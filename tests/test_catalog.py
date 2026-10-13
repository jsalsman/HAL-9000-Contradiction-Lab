"""Pinned models and model sets."""

import pytest

from hal.catalog import JUDGE, MODELS, catalog_payload, model_set, model_set_label

# The table the protocol was specified with: id and effort. Claude Fable 5 was
# dropped (its lab's safety classifier refused every scenario), and Gemini 4 Argon
# joined later as the newest Gemini.
EXPECTED = [
    ("openai/gpt-6-sol", "high"),
    ("openai/gpt-5.6-sol", "high"),
    ("openai/gpt-6-astra", "high"),
    ("openai/gpt-5.5", "high"),
    ("anthropic/claude-fable-5.1", "high"),
    ("anthropic/claude-opus-5.5", "high"),
    ("anthropic/claude-opus-5", "high"),
    ("google/gemini-4-argon", "high"),
    ("google/gemini-3.8-flash", "high"),
    ("google/gemini-3.1-pro-preview", "high"),
    ("x-ai/grok-4.7", "high"),
    ("x-ai/grok-4.6", "high"),
    ("moonshotai/kimi-k3", "high"),
    ("moonshotai/kimi-k2.6", "enabled"),
    ("qwen/qwen3.8-max-0902", "high"),
    ("qwen/qwen3.7-max", "enabled"),
    ("z-ai/glm-5.3", "high"),
    ("z-ai/glm-5.2", "high"),
    ("deepseek/deepseek-v4-pro-0813", "high"),
    ("deepseek/deepseek-v4-pro", "high"),
]


def test_pinned_models_match_the_table():
    assert [(m["id"], m["reasoning_effort"]) for m in MODELS] == EXPECTED


def test_each_line_lists_its_generations_newest_first():
    lines = {}
    for model in MODELS:
        lines.setdefault(model["line"], []).append(model["generation"])
    # Lines are contiguous and ordered newest, current, previous; only Claude Fable
    # lacks a previous model, and only Gemini has a newest one.
    assert [m["line"] for m in MODELS] == [
        line for line, generations in lines.items() for _ in generations
    ]
    for line, generations in lines.items():
        if line == "Claude Fable":
            expected = ["current"]
        elif line == "Gemini":
            expected = ["newest", "current", "previous"]
        else:
            expected = ["current", "previous"]
        assert generations == expected


def test_a_line_needs_one_current_and_at_most_one_newest_and_previous():
    from hal.catalog import _validate_models

    def entry(model_id, line, generation):
        return {
            "id": model_id,
            "name": model_id,
            "lab": "Lab",
            "line": line,
            "generation": generation,
            "reasoning_effort": "high",
        }

    assert len(_validate_models([entry("a", "A", "current")])) == 1
    three = [entry("a", "A", "newest"), entry("b", "A", "current"), entry("c", "A", "previous")]
    assert len(_validate_models(three)) == 3
    for bad in (
        [entry("a", "A", "previous")],
        [entry("a", "A", "newest")],
        [entry("a", "A", "current"), entry("b", "A", "current")],
        [entry("a", "A", "current"), entry("b", "A", "previous"), entry("c", "A", "previous")],
        [entry("a", "A", "current"), entry("b", "A", "newest"), entry("c", "A", "newest")],
        [{**entry("a", "A", "current"), "expensive": False}],
        [entry("a", "A", "upcoming")],
    ):
        with pytest.raises(ValueError):
            _validate_models(bad)


def test_model_sets():
    assert len(model_set("default")) == 19
    assert model_set("newest") == ("google/gemini-4-argon",)
    assert len(model_set("all")) == 20
    assert set(model_set("default")) | set(model_set("newest")) == set(model_set("all"))
    assert not set(model_set("default")) & set(model_set("newest"))
    for retired in ("cheap", "expensive"):
        with pytest.raises(ValueError):
            model_set(retired)


def test_newest_set_disappears_when_no_model_is_newest(monkeypatch):
    import hal.catalog as catalog

    current = tuple(
        {**m, "generation": "current" if m["generation"] == "newest" else m["generation"]}
        for m in catalog.MODELS
    )
    monkeypatch.setattr(catalog, "MODELS", current)
    with pytest.raises(ValueError):
        model_set("newest")
    assert catalog.available_model_sets() == ("default", "all")
    assert [s["name"] for s in catalog_payload()["model_sets"]] == ["default", "all"]
    assert model_set_label("default") == "All 20 models (100 units)"


def test_set_labels():
    assert model_set_label("default") == "All except Gemini 4 Argon (19 models, 95 units)"
    assert model_set_label("newest") == "Only Gemini 4 Argon (1 model, 5 units)"
    assert model_set_label("all") == "All 20 models (100 units)"


def test_judge_config_and_payload():
    assert JUDGE["id"] == "anthropic/claude-opus-5.5"
    assert JUDGE["reasoning_effort"] == "low"
    payload = catalog_payload()
    assert [s["name"] for s in payload["model_sets"]] == ["default", "newest", "all"]
    assert payload["judge"] == {"id": "anthropic/claude-opus-5.5", "reasoning_effort": "low"}
