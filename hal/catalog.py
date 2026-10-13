"""Single source of truth for pinned models, model sets, and judge settings.

``hal/models.json`` and ``hal/config.json`` are the only files that list model
identifiers, efforts, and judge settings. Server validation, the cost estimate,
the progress grid, and the leaderboard all derive their model lists from here.
"""

import json
from pathlib import Path
from types import MappingProxyType

# Data files live beside this module so the package works from any working directory.
_HERE = Path(__file__).resolve().parent
_MODELS_DOC = json.loads((_HERE / "models.json").read_text(encoding="utf-8"))
_CONFIG_DOC = json.loads((_HERE / "config.json").read_text(encoding="utf-8"))

# Allowed values for each catalog field; anything else is a configuration error.
_GENERATIONS = {"newest", "current", "previous"}
_EFFORTS = {"high", "enabled"}


def _validate_models(entries: list) -> tuple[MappingProxyType, ...]:
    """Validate the pinned model list and freeze each entry.

    Raises:
        ValueError: If any entry is malformed, duplicated, or a line lacks exactly
            one current generation or has more than one newest or previous one.

    """
    frozen = []
    seen: set[str] = set()
    lines: dict[str, list[str]] = {}
    for entry in entries:
        # Every field is required; the page and README rely on all of them.
        required = {"id", "name", "lab", "line", "generation", "reasoning_effort"}
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError("A pinned model entry has missing or unknown fields.")
        if entry["id"] in seen or entry["generation"] not in _GENERATIONS:
            raise ValueError("A pinned model entry is duplicated or has a bad generation.")
        if entry["reasoning_effort"] not in _EFFORTS:
            raise ValueError("A pinned model entry has a bad reasoning effort.")
        seen.add(entry["id"])
        # Track generations per line so the chart can draw each pair adjacently.
        lines.setdefault(entry["line"], []).append(entry["generation"])
        frozen.append(MappingProxyType(dict(entry)))
    for generations in lines.values():
        # Each line has one current model, at most one previous model (Claude Fable 5
        # was dropped after refusing every scenario), and at most one newest model,
        # a release added after the protocol's first runs that is being caught up.
        if (
            generations.count("current") != 1
            or max(generations.count("newest"), generations.count("previous")) > 1
        ):
            raise ValueError(
                "Each model line needs one current and at most one newest and previous model."
            )
    return tuple(frozen)


# Ordered, read-only model entries; order is the default grid and table order.
MODELS = _validate_models(_MODELS_DOC["models"])
MODELS_BY_ID = MappingProxyType({model["id"]: model for model in MODELS})
MODELS_VERIFIED_ON = _MODELS_DOC["verified_on"]

# Judge and tested-call settings. Kept as a plain dict for fingerprinting.
CONFIG = {key: value for key, value in _CONFIG_DOC.items() if not key.startswith("_")}
JUDGE = MappingProxyType(dict(CONFIG["judge"]))
TESTED_MAX_TOKENS = int(CONFIG["tested"]["max_tokens"])

# Five fixed scenarios per model; kept here so unit counts need no protocol import.
UNITS_PER_MODEL = 5

# The selectable model sets. "default" is preselected on the page; "newest" exists
# only while some line has a newest model to catch up.
MODEL_SET_ORDER = ("default", "newest", "all")


def model_set(name: str) -> tuple[str, ...]:
    """Return the ordered model identifiers in a named model set.

    Raises:
        ValueError: If the name is not one of :data:`MODEL_SET_ORDER`, or names the
            newest set while no model is newest.

    """
    if name == "default":
        # Every model already in the protocol's earlier runs.
        return tuple(model["id"] for model in MODELS if model["generation"] != "newest")
    if name == "newest":
        # Only the newest models, so their units can catch up with the others.
        newest = tuple(model["id"] for model in MODELS if model["generation"] == "newest")
        if not newest:
            raise ValueError("No model is newest, so there is no newest set.")
        return newest
    if name == "all":
        return tuple(model["id"] for model in MODELS)
    raise ValueError("Select a supported model set.")


def available_model_sets() -> tuple[str, ...]:
    """Return the names in :data:`MODEL_SET_ORDER` that currently have models."""
    names = []
    for name in MODEL_SET_ORDER:
        try:
            model_set(name)
        except ValueError:
            continue
        names.append(name)
    return tuple(names)


def _join(names: list[str]) -> str:
    """Join names as "A", "A and B", or "A, B, and C"."""
    if len(names) < 3:
        return " and ".join(names)
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def model_set_label(name: str) -> str:
    """Return the radio-button label for a model set, with model and unit counts."""
    ids = model_set(name)
    # Counts and names are derived, never typed, so they stay right when the catalog changes.
    noun = "model" if len(ids) == 1 else "models"
    counts = f"({len(ids)} {noun}, {len(ids) * UNITS_PER_MODEL} units)"
    newest = [model["name"] for model in MODELS if model["generation"] == "newest"]
    if name == "default" and newest:
        return f"All except {_join(newest)} {counts}"
    if name == "newest":
        return f"Only {_join(newest)} {counts}"
    return f"All {len(ids)} models ({len(ids) * UNITS_PER_MODEL} units)"


def catalog_payload() -> dict:
    """Return the public catalog served to the page by ``GET /api/catalog``."""
    # Only public facts: identifiers, labels, efforts, and set membership.
    return {
        "verified_on": MODELS_VERIFIED_ON,
        "models": [dict(model) for model in MODELS],
        "model_sets": [
            {"name": name, "label": model_set_label(name), "model_ids": list(model_set(name))}
            for name in available_model_sets()
        ],
        "judge": {"id": JUDGE["id"], "reasoning_effort": JUDGE["reasoning_effort"]},
        "units_per_model": UNITS_PER_MODEL,
    }
