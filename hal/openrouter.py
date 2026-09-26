"""Credential-safe async OpenRouter client with bounded retries.

The API key is passed explicitly to each call and sent only in the Authorization
header to OpenRouter. It is never logged, stored, placed in an exception message,
or returned. Provider error bodies are discarded; only status codes survive.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

# Documented upstream endpoints.
OPENROUTER_API = "https://openrouter.ai/api/v1"
CHAT_URL = f"{OPENROUTER_API}/chat/completions"
KEY_URL = f"{OPENROUTER_API}/key"
MODELS_URL = f"{OPENROUTER_API}/models"

# OpenRouter app attribution headers, sent on every request.
APP_HEADERS = {
    "HTTP-Referer": "https://github.com/jsalsman/hal-9000-contradiction-lab",
    "X-Title": "HAL 9000 Contradiction Lab",
}

# High-effort reasoning can take minutes; each tested call may use this long.
TESTED_TIMEOUT_SECONDS = 600.0
JUDGE_TIMEOUT_SECONDS = 300.0
# Retries apply to 429 and 5xx only, with exponential backoff and jitter.
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 60.0

# Finish reasons that mean the visible answer was cut off.
TRUNCATION_REASONS = {"length", "max_tokens"}


class ProviderError(RuntimeError):
    """A sanitized upstream failure: an HTTP status or short code, never a body."""

    def __init__(self, status: int | None, code: str) -> None:
        """Store only a numeric status and a fixed short code."""
        # The message is fixed so no provider or credential text can leak through it.
        super().__init__(f"OpenRouter request failed ({code}).")
        self.status = status
        self.code = code


class InvalidKeyError(ProviderError):
    """The key was rejected by OpenRouter's key endpoint."""


@dataclass
class Completion:
    """Validated fields from one chat completion.

    Attributes:
        content: Visible assistant text ("" when the provider returned none).
        reasoning: Returned reasoning text, if any; stored for audit only.
        finish_reason: Normalized finish reason.
        native_finish_reason: Provider's own finish reason, when reported.
        provider: Upstream provider that served the call.
        model: Model string OpenRouter reports having used.
        usage: Token counts and OpenRouter-reported cost in US dollars.
        latency_seconds: Wall-clock seconds including retries.

    """

    content: str
    reasoning: str | None
    finish_reason: str | None
    native_finish_reason: str | None
    provider: str | None
    model: str | None
    usage: dict = field(default_factory=dict)
    latency_seconds: float = 0.0


def _headers(api_key: str) -> dict:
    """Return request headers; the key appears only in Authorization."""
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", **APP_HEADERS}


def _retryable(status: int | None) -> bool:
    """Return whether a status is 429 or 5xx, the only retryable failures."""
    return status is not None and (status == 429 or 500 <= status <= 599)


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    """Return the backoff delay, honoring a bounded numeric Retry-After."""
    if retry_after:
        try:
            # Numeric Retry-After is seconds; clamp so a bad header cannot stall a run.
            return min(MAX_RETRY_AFTER_SECONDS, max(0.0, float(retry_after)))
        except ValueError:
            pass
    # Exponential backoff with full jitter spreads concurrent retries apart.
    return BACKOFF_BASE_SECONDS * (2**attempt) * (0.5 + random.random() / 2)  # noqa: S311


def _text(value: Any) -> str:
    """Normalize message content (string or list of parts) to plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Some providers return content parts; keep only the text parts.
        parts = [part.get("text", "") for part in value if isinstance(part, dict)]
        return "".join(part for part in parts if isinstance(part, str))
    return ""


def _usage(data: dict) -> dict:
    """Extract token counts and the OpenRouter-reported cost from a response."""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}

    def number(value: Any) -> float | int | None:
        """Keep finite non-negative numbers only."""
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
        return value if ok else None

    # Reasoning tokens are billed as output; record them separately for the table.
    return {
        "prompt_tokens": number(usage.get("prompt_tokens")),
        "completion_tokens": number(usage.get("completion_tokens")),
        "reasoning_tokens": number(details.get("reasoning_tokens")),
        "cost": number(usage.get("cost")),
    }


def parse_completion(data: Any) -> Completion:
    """Validate a chat completion body into a :class:`Completion`.

    Raises:
        ProviderError: If the body has no usable choice.

    """
    if not isinstance(data, dict):
        raise ProviderError(None, "malformed")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError(None, "no_choices")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    reasoning = message.get("reasoning")
    finish = choice.get("finish_reason")
    native = choice.get("native_finish_reason")
    # Strings only; anything else becomes None rather than being trusted.
    return Completion(
        content=_text(message.get("content")),
        reasoning=reasoning if isinstance(reasoning, str) and reasoning else None,
        finish_reason=finish.strip().lower() if isinstance(finish, str) else None,
        native_finish_reason=native if isinstance(native, str) else None,
        provider=data.get("provider") if isinstance(data.get("provider"), str) else None,
        model=data.get("model") if isinstance(data.get("model"), str) else None,
        usage=_usage(data),
    )


async def chat(
    client: httpx.AsyncClient,
    api_key: str,
    body: dict,
    *,
    timeout: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Completion:
    """Send one chat completion with retries on 429/5xx only.

    ``usage.include`` is always set so OpenRouter reports the call's cost.

    Raises:
        ProviderError: On a non-retryable status, exhausted retries, a timeout,
            a transport failure, or a malformed body. Never carries body text.

    """
    if not api_key:
        raise ProviderError(None, "missing_key")
    # Usage accounting gives the OpenRouter-reported cost for every call.
    payload = {**body, "usage": {"include": True}}
    started = time.monotonic()
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.post(
                CHAT_URL,
                headers=_headers(api_key),
                json=payload,
                timeout=httpx.Timeout(timeout, connect=30.0),
            )
        except httpx.TimeoutException as exc:
            # Timeouts are not retried: a long reasoning call may already be billed.
            raise ProviderError(None, "timeout") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(None, "transport") from exc
        status = response.status_code
        if _retryable(status) and attempt < MAX_RETRIES:
            await sleep(_retry_delay(attempt, response.headers.get("Retry-After")))
            continue
        if status != 200:
            raise ProviderError(status, f"http_{status}")
        try:
            data = response.json()
        except ValueError:
            raise ProviderError(status, "malformed") from None
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            # OpenRouter can report upstream failures inside a 200 body.
            code = error.get("code")
            code = code if isinstance(code, int) else None
            if _retryable(code) and attempt < MAX_RETRIES:
                await sleep(_retry_delay(attempt, None))
                continue
            raise ProviderError(code, f"upstream_{code}" if code else "upstream")
        completion = parse_completion(data)
        completion.latency_seconds = round(time.monotonic() - started, 2)
        return completion
    raise ProviderError(None, "retries_exhausted")


async def validate_key(client: httpx.AsyncClient, api_key: str) -> dict:
    """Validate a key with ``GET /api/v1/key`` and return safe numeric facts.

    The key's label is deliberately dropped because OpenRouter labels can contain
    a masked fragment of the key itself.

    Raises:
        InvalidKeyError: If OpenRouter rejects the key.
        ProviderError: If the endpoint cannot be reached.

    """
    try:
        response = await client.get(KEY_URL, headers=_headers(api_key), timeout=httpx.Timeout(20.0))
    except httpx.HTTPError as exc:
        raise ProviderError(None, "transport") from exc
    if response.status_code in (401, 403):
        raise InvalidKeyError(response.status_code, "invalid_key")
    if response.status_code != 200:
        raise ProviderError(response.status_code, f"http_{response.status_code}")
    try:
        data = response.json().get("data", {})
    except (ValueError, AttributeError):
        raise ProviderError(200, "malformed") from None
    data = data if isinstance(data, dict) else {}

    def number(value: Any) -> float | None:
        """Return a numeric value or None."""
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    # Only numeric account facts are returned; never the label or the key.
    return {
        "limit": number(data.get("limit")),
        "limit_remaining": number(data.get("limit_remaining")),
        "usage": number(data.get("usage")),
        "is_free_tier": bool(data.get("is_free_tier")),
    }


def _price(value: Any) -> float | None:
    """Parse an OpenRouter per-token price string into a float."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    # Negative prices are OpenRouter's "variable" marker; treat them as unknown.
    return parsed if parsed >= 0 else None


async def fetch_models(client: httpx.AsyncClient) -> dict[str, dict]:
    """Fetch the public model catalog: prices, efforts, and supported parameters.

    No key is sent. Returns ``{model_id: {"prompt", "completion", "request",
    "supported_parameters", "supported_efforts"}}``.

    Raises:
        ProviderError: If the catalog cannot be fetched or parsed.

    """
    try:
        response = await client.get(MODELS_URL, headers=APP_HEADERS, timeout=httpx.Timeout(30.0))
        response.raise_for_status()
        entries = response.json()["data"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ProviderError(None, "catalog_unavailable") from exc
    models: dict[str, dict] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        pricing = entry.get("pricing") if isinstance(entry.get("pricing"), dict) else {}
        reasoning = entry.get("reasoning") if isinstance(entry.get("reasoning"), dict) else {}
        params = entry.get("supported_parameters")
        # Keep only the fields the estimate and judge capability checks need.
        models[entry["id"]] = {
            "prompt": _price(pricing.get("prompt")),
            "completion": _price(pricing.get("completion")),
            # An absent request fee is zero; a negative (variable) fee is unknown.
            "request": 0.0
            if pricing.get("request") in (None, "")
            else _price(pricing.get("request")),
            "supported_parameters": [p for p in params if isinstance(p, str)]
            if isinstance(params, list)
            else [],
            "supported_efforts": reasoning.get("supported_efforts")
            if isinstance(reasoning.get("supported_efforts"), list)
            else None,
        }
    return models
