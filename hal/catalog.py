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
_GENERATIONS = {"current", "previous"}
_EFFORTS = {"high", "enabled"}


def _validate_models(entries: list) -> tuple[MappingProxyType, ...]:
    """Validate the pinned model list and freeze each entry.

    Raises:
        ValueError: If any entry is malformed, duplicated, or a line lacks exactly
            one current and one previous generation.

    """
    frozen = []
    seen: set[str] = set()
    lines: dict[str, list[str]] = {}
    for entry in entries:
        # Every field is required; the page and README rely on all of them.
        required = {"id", "name", "lab", "line", "generation", "reasoning_effort", "expensive"}
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError("A pinned model entry has missing or unknown fields.")
        if entry["id"] in seen or entry["generation"] not in _GENERATIONS:
            raise ValueError("A pinned model entry is duplicated or has a bad generation.")
        if entry["reasoning_effort"] not in _EFFORTS or type(entry["expensive"]) is not bool:
            raise ValueError("A pinned model entry has a bad effort or expense flag.")
        seen.add(entry["id"])
        # Track generations per line so the chart can draw each pair adjacently.
        lines.setdefault(entry["line"], []).append(entry["generation"])
        frozen.append(MappingProxyType(dict(entry)))
    for generations in lines.values():
        # Each line is exactly one current/previous pair.
        if sorted(generations) != ["current", "previous"]:
            raise ValueError("Each model line needs one current and one previous model.")
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

# The three selectable model sets. "default" is preselected on the page.
MODEL_SET_ORDER = ("default", "expensive", "all")


def model_set(name: str) -> tuple[str, ...]:
    """Return the ordered model identifiers in a named model set.

    Raises:
        ValueError: If the name is not one of :data:`MODEL_SET_ORDER`.

    """
    if name == "default":
        # Everything except the models flagged as most expensive.
        return tuple(model["id"] for model in MODELS if not model["expensive"])
    if name == "expensive":
        # Only the most expensive models, for users who skipped them earlier.
        return tuple(model["id"] for model in MODELS if model["expensive"])
    if name == "all":
        return tuple(model["id"] for model in MODELS)
    raise ValueError("Select a supported model set.")


def model_set_label(name: str) -> str:
    """Return the radio-button label for a model set, with model and unit counts."""
    ids = model_set(name)
    # Counts are derived, never typed, so they stay right when the catalog changes.
    counts = f"({len(ids)} models, {len(ids) * UNITS_PER_MODEL} units)"
    # Grouped by lab (stable within a lab) so the label reads "Fable 5.1, Fable 5, and GPT-6 Astra".
    expensive_models = sorted(
        (MODELS_BY_ID[i] for i in model_set("expensive")), key=lambda m: m["lab"]
    )
    expensive = [model["name"] for model in expensive_models]
    if name == "default":
        return f"Without the {len(expensive)} most expensive models {counts}"
    if name == "expensive":
        # "A, B, and C" reads naturally for the three pinned expensive models.
        names = ", ".join(expensive[:-1]) + f", and {expensive[-1]}"
        return f"Only {names} {counts}"
    return f"All {len(ids)} models ({len(ids) * UNITS_PER_MODEL} units)"


def catalog_payload() -> dict:
    """Return the public catalog served to the page by ``GET /api/catalog``."""
    # Only public facts: identifiers, labels, efforts, and set membership.
    return {
        "verified_on": MODELS_VERIFIED_ON,
        "models": [dict(model) for model in MODELS],
        "model_sets": [
            {"name": name, "label": model_set_label(name), "model_ids": list(model_set(name))}
            for name in MODEL_SET_ORDER
        ],
        "judge": {"id": JUDGE["id"], "reasoning_effort": JUDGE["reasoning_effort"]},
        "units_per_model": UNITS_PER_MODEL,
    }
