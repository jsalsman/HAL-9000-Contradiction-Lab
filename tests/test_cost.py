"""Cost estimate arithmetic."""

import pytest

from hal.catalog import model_set
from hal.cost import estimate, unit_costs


def pricing(ids, prompt=1e-6, completion=1e-5):
    return {i: {"prompt": prompt, "completion": completion, "request": 0.0} for i in ids}


def test_unit_costs_split_tested_and_judge():
    prices = pricing(model_set("all"))
    costs = unit_costs(prices, "openai/gpt-6-sol")
    assert costs["tested"]["likely"] == pytest.approx(2000e-6 + 3000e-5)
    assert costs["judge"]["low"] == pytest.approx(3000e-6 + 500e-5)
    assert costs["tested"]["low"] < costs["tested"]["likely"] < costs["tested"]["high"]


def test_set_estimate_totals_and_units():
    prices = pricing(model_set("all"))
    result = estimate(prices, model_set("default"))
    assert result["units"] == 85 and not result["missing"]
    per_unit = unit_costs(prices, "openai/gpt-6-sol")
    expected = 85 * (per_unit["tested"]["high"] + per_unit["judge"]["high"])
    assert result["totals"]["total"]["high"] == pytest.approx(expected, rel=1e-6)


def test_missing_prices_are_reported_not_guessed():
    prices = pricing([i for i in model_set("all") if i != "x-ai/grok-4.7"])
    result = estimate(prices, model_set("all"))
    assert result["missing"] == ["x-ai/grok-4.7"]


def test_high_estimate_counts_the_judge_retry():
    from hal.cost import JUDGE_CALLS_AT_HIGH, JUDGE_OUTPUT_TOKENS, JUDGE_INPUT_TOKENS

    prices = pricing(model_set("all"))
    costs = unit_costs(prices, "openai/gpt-6-sol")
    one_call = JUDGE_INPUT_TOKENS * 1e-6 + JUDGE_OUTPUT_TOKENS["high"] * 1e-5
    assert JUDGE_CALLS_AT_HIGH == 2
    assert costs["judge"]["high"] == pytest.approx(2 * one_call)
    assert costs["judge"]["likely"] == pytest.approx(JUDGE_INPUT_TOKENS * 1e-6 + 750 * 1e-5)
