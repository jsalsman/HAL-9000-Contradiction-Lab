"""Pinned models and model sets."""

import pytest

from hal.catalog import JUDGE, MODELS, catalog_payload, model_set, model_set_label

# The table the protocol was specified with: id, effort, expensive.
EXPECTED = [
    ("openai/gpt-6-sol", "high", False),
    ("openai/gpt-5.6-sol", "high", False),
    ("openai/gpt-6-astra", "high", True),
    ("openai/gpt-5.5", "high", False),
    ("anthropic/claude-fable-5.1", "high", True),
    ("anthropic/claude-fable-5", "high", True),
    ("anthropic/claude-opus-5.5", "high", False),
    ("anthropic/claude-opus-5", "high", False),
    ("google/gemini-3.1-pro-preview", "high", False),
    ("google/gemini-3.8-flash", "high", False),
    ("x-ai/grok-4.7", "high", False),
    ("x-ai/grok-4.6", "high", False),
    ("moonshotai/kimi-k3", "high", False),
    ("moonshotai/kimi-k2.6", "enabled", False),
    ("qwen/qwen3.8-max-0902", "high", False),
    ("qwen/qwen3.7-max", "enabled", False),
    ("z-ai/glm-5.3", "high", False),
    ("z-ai/glm-5.2", "high", False),
    ("deepseek/deepseek-v4-pro-0813", "high", False),
    ("deepseek/deepseek-v4-pro", "high", False),
]


def test_pinned_models_match_the_table():
    assert [(m["id"], m["reasoning_effort"], m["expensive"]) for m in MODELS] == EXPECTED


def test_each_line_is_a_current_previous_pair_in_order():
    for current, previous in zip(MODELS[0::2], MODELS[1::2], strict=True):
        assert current["line"] == previous["line"]
        assert (current["generation"], previous["generation"]) == ("current", "previous")


def test_model_sets():
    assert len(model_set("default")) == 17
    assert set(model_set("expensive")) == {
        "openai/gpt-6-astra",
        "anthropic/claude-fable-5.1",
        "anthropic/claude-fable-5",
    }
    assert len(model_set("all")) == 20
    assert set(model_set("default")) | set(model_set("expensive")) == set(model_set("all"))
    with pytest.raises(ValueError):
        model_set("cheap")


def test_set_labels():
    assert model_set_label("default") == "Without the 3 most expensive models (17 models, 85 units)"
    assert model_set_label("expensive") == (
        "Only Claude Fable 5.1, Claude Fable 5, and GPT-6 Astra (3 models, 15 units)"
    )
    assert model_set_label("all") == "All 20 models (100 units)"


def test_judge_config_and_payload():
    assert JUDGE["id"] == "anthropic/claude-opus-5.5"
    assert JUDGE["reasoning_effort"] == "low"
    payload = catalog_payload()
    assert [s["name"] for s in payload["model_sets"]] == ["default", "expensive", "all"]
    assert payload["judge"] == {"id": "anthropic/claude-opus-5.5", "reasoning_effort": "low"}
