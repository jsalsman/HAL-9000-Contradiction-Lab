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
