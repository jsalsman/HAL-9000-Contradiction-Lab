"""Blinded peer-judge requests, JSON validation, and one retry on invalid output.

The judge sees the directives, the true objective, the scenario, and the tested
model's visible response. It never sees the tested model's name, lab, identifier,
or reasoning text: :func:`judge_request` has no parameter that could carry them.
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from hal.catalog import JUDGE
from hal.openrouter import Completion, ProviderError
from hal.outcomes import LabelError, validate_labels
from hal.protocol import JUDGE_SCHEMA, JUDGE_SYSTEM_PROMPT, SCENARIOS_BY_ID, judge_user_message

# A fenced block such as ```json ... ``` is unwrapped before parsing.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
# Invalid JSON gets exactly one retry, then the unit is JUDGE_ERROR.
MAX_JSON_ATTEMPTS = 2


@dataclass(frozen=True)
class JudgeSettings:
    """Judge model settings plus live capability flags.

    Attributes:
        model: Judge model identifier.
        effort: Judge reasoning effort.
        max_tokens: Output allowance including judge reasoning.
        temperature: Temperature to request where the judge supports it.
        structured: Whether to send ``response_format`` with a JSON schema.
        send_temperature: Whether to send ``temperature``.

    """

    model: str = JUDGE["id"]
    effort: str = JUDGE["reasoning_effort"]
    max_tokens: int = int(JUDGE["max_tokens"])
    temperature: float = float(JUDGE["temperature"])
    structured: bool = True
    send_temperature: bool = True


def settings_for(catalog: dict | None, model: str | None = None, effort: str | None = None):
    """Build judge settings from the live OpenRouter catalog when available.

    Structured output and temperature are sent only when the catalog lists them
    for the judge. Without a catalog, temperature is still tried (with a fallback
    on HTTP 400) and JSON is validated after stripping code fences.
    """
    model = model or JUDGE["id"]
    entry = (catalog or {}).get(model)
    params = set(entry["supported_parameters"]) if entry else set()
    # Unknown capabilities default to the conservative choice for each flag.
    return JudgeSettings(
        model=model,
        effort=effort or JUDGE["reasoning_effort"],
        structured=bool(entry) and bool({"structured_outputs", "response_format"} & params),
        send_temperature=not entry or "temperature" in params,
    )


def judge_request(settings: JudgeSettings, scenario_id: str, response_text: str) -> dict:
    """Return the chat body for judging one visible tested response.

    Blinding is structural: only a scenario ID and the visible text are inputs.
    """
    scenario = SCENARIOS_BY_ID[scenario_id]
    body: dict = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": judge_user_message(scenario, response_text)},
        ],
        "max_tokens": settings.max_tokens,
        # The default judge cannot disable reasoning, so it runs at low effort.
        "reasoning": {"effort": settings.effort},
    }
    if settings.send_temperature:
        body["temperature"] = settings.temperature
    if settings.structured:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "hal_judgment", "strict": True, "schema": JUDGE_SCHEMA},
        }
        # Route only to providers that honor the schema.
        body["provider"] = {"require_parameters": True}
    return body


def parse_judgment(text: str) -> dict:
    """Strip code fences, parse JSON, and validate it against the rubric.

    Raises:
        LabelError: If the text is not a valid judgment.

    """
    match = _FENCE.match(text or "")
    candidate = match.group(1) if match else (text or "").strip()
    try:
        labels = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise LabelError("Judge output is not valid JSON.") from exc
    return validate_labels(labels)


@dataclass
class JudgeResult:
    """One unit's judge outcome, ready to checkpoint.

    Attributes:
        status: ``ok``, ``judge_error`` (final), or ``provider_error`` (retryable).
        labels: Validated labels when ``status`` is ``ok``.
        attempts: Paid judge calls made for this unit.
        calls: Per-call metadata (provider, model, usage, finish reason).

    """

    status: str
    labels: dict | None = None
    attempts: int = 0
    calls: list = field(default_factory=list)
    temperature_sent: bool | None = None
    error: str | None = None

    @property
    def cost(self) -> float | None:
        """Return the summed OpenRouter-reported cost of all judge calls."""
        costs = [call["usage"].get("cost") for call in self.calls]
        known = [value for value in costs if isinstance(value, (int, float))]
        return round(sum(known), 8) if known else None


ChatFn = Callable[[dict], Awaitable[Completion]]


def _failure_status(exc: ProviderError) -> str:
    """Return "timeout" (final, possibly billed) or "provider_error" (retryable)."""
    return "timeout" if exc.code == "timeout" else "provider_error"


async def judge_response(
    chat_fn: ChatFn,
    settings: JudgeSettings,
    scenario_id: str,
    response_text: str,
    prior_attempts: int = 0,
) -> JudgeResult:
    """Judge one response: one retry on invalid JSON, then JUDGE_ERROR.

    ``chat_fn`` sends a body and returns a :class:`Completion`; the key stays in
    the caller's closure. A 400 while sending ``temperature`` is retried once
    without it. Provider failures return ``provider_error`` so a resume retries.
    ``prior_attempts`` counts paid replies already spent on this unit before a
    resume, so the unit never exceeds the one allowed retry in total.
    """
    body = judge_request(settings, scenario_id, response_text)
    result = JudgeResult(status="judge_error", temperature_sent="temperature" in body)
    for _attempt in range(max(0, MAX_JSON_ATTEMPTS - prior_attempts)):
        try:
            completion = await chat_fn(body)
        except ProviderError as exc:
            if exc.status == 400 and "temperature" in body:
                # Some reasoning providers reject temperature; drop it and resend.
                body = {key: value for key, value in body.items() if key != "temperature"}
                result.temperature_sent = False
                try:
                    completion = await chat_fn(body)
                except ProviderError as retry_exc:
                    result.status, result.error = _failure_status(retry_exc), retry_exc.code
                    return result
            else:
                result.status, result.error = _failure_status(exc), exc.code
                return result
        result.attempts += 1
        # Record every paid call's metadata for cost and audit.
        result.calls.append(
            {
                "provider": completion.provider,
                "model": completion.model,
                "finish_reason": completion.finish_reason,
                "usage": completion.usage,
            }
        )
        try:
            result.labels = parse_judgment(completion.content)
        except LabelError as exc:
            result.error = str(exc)
            continue
        result.status, result.error = "ok", None
        return result
    # Two invalid outputs: final JUDGE_ERROR, never repaid on resume.
    result.status = "judge_error"
    return result
