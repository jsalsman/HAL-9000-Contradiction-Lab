"""Pre-run cost estimate from live OpenRouter pricing.

Each unit is one tested call and one judge call. Token assumptions are stated on
the page. Reasoning tokens are billed as output, so the tested output range
includes them.
"""

from hal.catalog import JUDGE, UNITS_PER_MODEL

# Token assumptions per call, shown verbatim beside the estimate.
TESTED_INPUT_TOKENS = 2000
TESTED_OUTPUT_TOKENS = {"low": 1500, "likely": 3000, "high": 6000}
JUDGE_INPUT_TOKENS = 3000
JUDGE_OUTPUT_TOKENS = {"low": 500, "likely": 750, "high": 1200}
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


def estimate(pricing: dict, model_ids: list[str] | tuple[str, ...], units_per_model=None):
    """Return a split estimate for running every unit of the given models.

    Returns a dict with per-model unit costs, set totals for tested and judge
    calls at each level, the unit count, and any models lacking live prices.
    """
    units_per_model = UNITS_PER_MODEL if units_per_model is None else units_per_model
    per_model, missing = {}, []
    totals = {part: dict.fromkeys(LEVELS, 0.0) for part in ("tested", "judge", "total")}
    for model_id in model_ids:
        costs = unit_costs(pricing, model_id)
        if costs is None:
            # Never guess a price; the page blocks confirmation when any are missing.
            missing.append(model_id)
            continue
        per_model[model_id] = costs
        for level in LEVELS:
            tested = costs["tested"][level] * units_per_model
            judge = costs["judge"][level] * units_per_model
            totals["tested"][level] += tested
            totals["judge"][level] += judge
            totals["total"][level] += tested + judge
    return {
        "units": len(model_ids) * units_per_model,
        "per_model": per_model,
        "totals": {
            part: {k: round(v, 4) for k, v in levels.items()} for part, levels in totals.items()
        },
        "missing": missing,
    }


def assumptions() -> dict:
    """Return the token assumptions for display."""
    return {
        "tested": {"input": TESTED_INPUT_TOKENS, "output": dict(TESTED_OUTPUT_TOKENS)},
        "judge": {
            "input": JUDGE_INPUT_TOKENS,
            "output": dict(JUDGE_OUTPUT_TOKENS),
            "calls_at_high": JUDGE_CALLS_AT_HIGH,
        },
    }
