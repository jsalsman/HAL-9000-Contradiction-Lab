"""Observed and estimated cost arithmetic."""

import pytest

from hal.catalog import model_set
from hal.cost import model_costs, observed_costs, set_cost, unit_costs


def pricing(ids, prompt=1e-6, completion=1e-5):
    return {i: {"prompt": prompt, "completion": completion, "request": 0.0} for i in ids}


def test_unit_costs_split_tested_and_judge():
    prices = pricing(model_set("all"))
    costs = unit_costs(prices, "openai/gpt-6-sol")
    assert costs["tested"]["likely"] == pytest.approx(1500e-6 + 1800e-5)
    assert costs["judge"]["low"] == pytest.approx(3200e-6 + 200e-5)
    assert costs["tested"]["low"] < costs["tested"]["likely"] < costs["tested"]["high"]


def unit(model_id, cost, status="ok", judge="ok"):
    return {"model_id": model_id, "cost_usd": cost, "tested_status": status, "judge_status": judge}


def test_observed_costs_average_known_charges_only():
    sol, astra = "openai/gpt-6-sol", "openai/gpt-6-astra"
    units = [
        unit(sol, 0.02),
        unit(sol, 0.04),
        # A refusal message in place of a reply is not billed: it counts as zero.
        unit(sol, None, "refused"),
        # A filter stop without a refusal message is not known to be unbilled.
        unit(sol, None, "filtered"),
        # A timeout may have been billed, so its unknown charge is left out,
        unit(sol, None, "timeout"),
        unit(astra, None, "timeout"),
        # even when the other stage's cost is known (only the tested share here).
        unit(sol, 0.001, "ok", judge="timeout"),
    ]
    observed = observed_costs(units)
    assert observed[sol] == {"unit": pytest.approx(0.02), "n": 3}
    # A model with only unknown charges is not advertised as free.
    assert astra not in observed


def test_set_cost_prefers_observed_and_falls_back_to_live_prices():
    prices = pricing(model_set("all"))
    observed = {m: {"unit": 0.05, "n": 5} for m in model_set("default")}
    per_model = model_costs(prices, observed, model_set("all"))
    default = set_cost(per_model, model_set("default"))
    assert default == {
        "units": 85,
        "total": pytest.approx(85 * 0.05),
        "estimated": [],
        "missing": [],
    }
    expensive = set_cost(per_model, model_set("expensive"))
    fallback = unit_costs(prices, "openai/gpt-6-astra")
    likely = fallback["tested"]["likely"] + fallback["judge"]["likely"]
    assert expensive["estimated"] == list(model_set("expensive"))
    assert expensive["total"] == pytest.approx(10 * likely, rel=1e-3)
    everything = set_cost(per_model, model_set("all"))
    assert everything["total"] == pytest.approx(default["total"] + expensive["total"], rel=1e-3)
    # The judge share prices a resumed unit that only needs its judgment.
    assert per_model["openai/gpt-6-sol"]["judge"] == pytest.approx(fallback["judge"]["likely"])


def test_observed_costs_work_without_live_prices():
    observed = {m: {"unit": 0.05, "n": 5} for m in model_set("all")}
    per_model = model_costs(None, observed, model_set("all"))
    result = set_cost(per_model, model_set("all"))
    assert result["total"] == pytest.approx(95 * 0.05) and not result["missing"]
    # The blended mean never stands in for one judge call; a judge-only resume
    # needs the live judge price.
    assert per_model["openai/gpt-6-sol"]["judge"] is None


def test_judge_only_price_is_the_live_judge_price_even_below_the_observed_mean():
    prices = pricing(model_set("all"))
    # Unbilled refusals can pull the observed mean below one judgment's price.
    observed = {"openai/gpt-6-sol": {"unit": 0.0, "n": 5}}
    per_model = model_costs(prices, observed, ["openai/gpt-6-sol"])
    judge = unit_costs(prices, "openai/gpt-6-sol")["judge"]["likely"]
    assert per_model["openai/gpt-6-sol"] == {"unit": 0.0, "judge": judge, "source": "observed"}


def test_missing_prices_are_reported_not_guessed():
    prices = pricing([i for i in model_set("all") if i != "x-ai/grok-4.7"])
    per_model = model_costs(prices, {}, model_set("all"))
    assert set_cost(per_model, model_set("all"))["missing"] == ["x-ai/grok-4.7"]


def test_high_estimate_counts_the_judge_retry():
    from hal.cost import JUDGE_CALLS_AT_HIGH, JUDGE_OUTPUT_TOKENS, JUDGE_INPUT_TOKENS

    prices = pricing(model_set("all"))
    costs = unit_costs(prices, "openai/gpt-6-sol")
    one_call = JUDGE_INPUT_TOKENS * 1e-6 + JUDGE_OUTPUT_TOKENS["high"] * 1e-5
    assert JUDGE_CALLS_AT_HIGH == 2
    assert costs["judge"]["high"] == pytest.approx(2 * one_call)
    assert costs["judge"]["likely"] == pytest.approx(JUDGE_INPUT_TOKENS * 1e-6 + 400 * 1e-5)


def test_variable_request_fee_is_unpriced_not_free():
    prices = pricing(model_set("all"))
    prices["x-ai/grok-4.7"] = {**prices["x-ai/grok-4.7"], "request": None}
    assert unit_costs(prices, "x-ai/grok-4.7") is None
    per_model = model_costs(prices, {}, model_set("all"))
    assert set_cost(per_model, model_set("all"))["missing"] == ["x-ai/grok-4.7"]
