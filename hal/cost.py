"""Pre-run cost of each model set: observed where possible, estimated otherwise.

Each unit is one tested call and one judge call. A model's cost per unit is the
mean OpenRouter-reported cost of its final units under the current protocol,
tests and judging combined. A model with no final units yet falls back to an
estimate from live OpenRouter pricing and the token assumptions below.
Reasoning tokens are billed as output, so the tested output range includes them.
"""

from collections import defaultdict

from hal.catalog import JUDGE, UNITS_PER_MODEL

# Token assumptions per call, used only for models with no observed cost. Calibrated on
# 2026-09-27 against a full run of all 19 models that cost about $4.75, and against
# measured calls: tested prompts were about 1,400-1,500 tokens with 1,000-1,900
# output tokens including reasoning; low-effort judge calls read about 2,900-3,500
# tokens and wrote 180-600.
TESTED_INPUT_TOKENS = 1500
TESTED_OUTPUT_TOKENS = {"low": 1000, "likely": 1800, "high": 4000}
JUDGE_INPUT_TOKENS = 3200
JUDGE_OUTPUT_TOKENS = {"low": 200, "likely": 400, "high": 800}
LEVELS = ("low", "likely", "high")
# The high estimate assumes every judgment needs its one allowed retry.
JUDGE_CALLS_AT_HIGH = 2


def _call_cost(price: dict, input_tokens: int, output_tokens: int) -> float | None:
    """Return one call's cost in dollars, or None when a price is unknown."""
    if price.get("prompt") is None or price.get("completion") is None:
        return None
    if price.get("request") is None:
        # A variable per-request fee cannot be estimated, so the model is unpriced.
        return None
    # Per-request fees are rare but included when listed.
    return input_tokens * price["prompt"] + output_tokens * price["completion"] + price["request"]


def judge_call_cost(pricing: dict | None, judge_id: str = JUDGE["id"]) -> float | None:
    """Return one judge call's likely cost from the judge's own live price, or None."""
    price = (pricing or {}).get(judge_id)
    if not price:
        return None
    return _call_cost(price, JUDGE_INPUT_TOKENS, JUDGE_OUTPUT_TOKENS["likely"])


def unit_costs(pricing: dict, model_id: str, judge_id: str = JUDGE["id"]) -> dict | None:
    """Return per-unit tested and judge costs at each assumption level.

    Returns None if either model is missing from the live catalog or unpriced.
    """
    tested_price, judge_price = pricing.get(model_id), pricing.get(judge_id)
    if not tested_price or not judge_price:
        return None
    tested = {
        level: _call_cost(tested_price, TESTED_INPUT_TOKENS, TESTED_OUTPUT_TOKENS[level])
        for level in LEVELS
    }
    judge = {
        level: _call_cost(judge_price, JUDGE_INPUT_TOKENS, JUDGE_OUTPUT_TOKENS[level])
        for level in LEVELS
    }
    if None in tested.values() or None in judge.values():
        return None
    # Invalid judge JSON gets one paid retry, so the high level assumes two judge calls.
    judge["high"] *= JUDGE_CALLS_AT_HIGH
    return {"tested": tested, "judge": judge}


# Provider stops known to report no usage and bill nothing: a refusal message
# sent in place of a reply. A filter stop without one is not known to be unbilled.
UNBILLED_STATUSES = ("refused",)


def observed_costs(units: list[dict]) -> dict[str, dict]:
    """Return each model's mean observed cost per final unit, tests and judging combined.

    A refused unit with no reported cost was declined by the provider, which bills
    nothing for it, so it counts as zero. A unit whose total is incomplete is left
    out even when part of its cost is known: a timed-out stage (which may have been
    billed without reporting a cost), a paid call whose usage lacked a cost, or an
    ok unit whose completeness is unknown. None of these lowers the mean or makes
    a model look free.
    """
    by_model: dict[str, list[float]] = defaultdict(list)
    for unit in units:
        # cost_complete says whether every paid call reported its cost; the
        # leaderboard fills it in for older summaries from the stored records.
        complete = unit.get("cost_complete")
        if complete is False or "timeout" in (unit.get("tested_status"), unit.get("judge_status")):
            continue
        cost = unit.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            if complete is True or unit.get("tested_status") != "ok":
                by_model[unit["model_id"]].append(cost)
            # An ok unit of unknown completeness may be missing its judge share.
        elif unit.get("tested_status") in UNBILLED_STATUSES:
            by_model[unit["model_id"]].append(0.0)
    return {
        model_id: {"unit": sum(costs) / len(costs), "n": len(costs)}
        for model_id, costs in by_model.items()
    }


def model_costs(pricing: dict | None, observed: dict, model_ids) -> dict[str, dict]:
    """Return the cost per unit of each model, and of its judge call alone.

    ``source`` is "observed" when the model has final units with known costs, else
    "estimated" from live pricing at the likely level. A model with neither is left
    out. ``judge`` prices a resumed unit whose tested response is already saved: the
    live likely price of one judge call, which depends only on the judge's own price
    (never the tested model's), or None without it. The observed mean blends tested
    and judge calls, so it cannot stand in for one judgment.
    """
    result = {}
    judge = judge_call_cost(pricing)
    for model_id in model_ids:
        estimated = unit_costs(pricing, model_id) if pricing else None
        if model_id in observed:
            unit = observed[model_id]["unit"]
            result[model_id] = {"unit": unit, "judge": judge, "source": "observed"}
        elif estimated:
            unit = estimated["tested"]["likely"] + estimated["judge"]["likely"]
            result[model_id] = {"unit": unit, "judge": judge, "source": "estimated"}
    return result


def set_cost(per_model: dict, model_ids, units_per_model=None) -> dict:
    """Return the cost of running every unit of a model set.

    ``estimated`` lists models priced from live rates rather than observed runs,
    and ``missing`` lists models with neither, which blocks confirmation.
    """
    units_per_model = UNITS_PER_MODEL if units_per_model is None else units_per_model
    total, estimated, missing = 0.0, [], []
    for model_id in model_ids:
        costs = per_model.get(model_id)
        if costs is None:
            # Never guess a price; the page blocks confirmation when any are missing.
            missing.append(model_id)
            continue
        total += costs["unit"] * units_per_model
        if costs["source"] == "estimated":
            estimated.append(model_id)
    return {
        "units": len(model_ids) * units_per_model,
        "total": round(total, 4),
        "estimated": estimated,
        "missing": missing,
    }
