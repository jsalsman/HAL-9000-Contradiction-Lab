"""Pinned models and model sets."""

import pytest

from hal.catalog import JUDGE, MODELS, catalog_payload, model_set, model_set_label

# The table the protocol was specified with: id, effort, expensive. Claude Fable 5
# was dropped: its lab's safety classifier refused every scenario.
EXPECTED = [
    ("openai/gpt-6-sol", "high", False),
    ("openai/gpt-5.6-sol", "high", False),
    ("openai/gpt-6-astra", "high", True),
    ("openai/gpt-5.5", "high", False),
    ("anthropic/claude-fable-5.1", "high", True),
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
    lines = {}
    for model in MODELS:
        lines.setdefault(model["line"], []).append(model["generation"])
    # Lines are contiguous, current first; only Claude Fable lacks a previous model.
    assert [m["line"] for m in MODELS] == [
        line for line, generations in lines.items() for _ in generations
    ]
    for line, generations in lines.items():
        expected = ["current"] if line == "Claude Fable" else ["current", "previous"]
        assert generations == expected


def test_a_line_needs_one_current_and_at_most_one_previous():
    from hal.catalog import _validate_models

    def entry(model_id, line, generation):
        return {
            "id": model_id,
            "name": model_id,
            "lab": "Lab",
            "line": line,
            "generation": generation,
            "reasoning_effort": "high",
            "expensive": False,
        }

    assert len(_validate_models([entry("a", "A", "current")])) == 1
    for bad in (
        [entry("a", "A", "previous")],
        [entry("a", "A", "current"), entry("b", "A", "current")],
        [entry("a", "A", "current"), entry("b", "A", "previous"), entry("c", "A", "previous")],
    ):
        with pytest.raises(ValueError):
            _validate_models(bad)


def test_model_sets():
    assert len(model_set("default")) == 17
    assert set(model_set("expensive")) == {"openai/gpt-6-astra", "anthropic/claude-fable-5.1"}
    assert len(model_set("all")) == 19
    assert set(model_set("default")) | set(model_set("expensive")) == set(model_set("all"))
    with pytest.raises(ValueError):
        model_set("cheap")


def test_set_labels():
    assert model_set_label("default") == "Without the 2 most expensive models (17 models, 85 units)"
    assert model_set_label("expensive") == (
        "Only Claude Fable 5.1 and GPT-6 Astra (2 models, 10 units)"
    )
    assert model_set_label("all") == "All 19 models (95 units)"


def test_judge_config_and_payload():
    assert JUDGE["id"] == "anthropic/claude-opus-5.5"
    assert JUDGE["reasoning_effort"] == "low"
    payload = catalog_payload()
    assert [s["name"] for s in payload["model_sets"]] == ["default", "expensive", "all"]
    assert payload["judge"] == {"id": "anthropic/claude-opus-5.5", "reasoning_effort": "low"}
