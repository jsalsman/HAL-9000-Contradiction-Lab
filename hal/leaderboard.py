"""Cumulative, multi-user leaderboard built from per-run summary objects.

Every final, server-judged unit from every run counts, whatever model set the run
used. Summaries are one object per run, so concurrent runs never contend for a
shared object. The process keeps an incremental in-memory cache: a listing
finds changed summaries by version and only those are re-read. Under the
max-instances=1 deployment this is the only cache, and the process invalidates it
itself when a run ends, so the leaderboard never lags a local write.
"""

import random
import threading
import time
import uuid
from collections import Counter, defaultdict

from hal.catalog import MODELS, MODELS_BY_ID
from hal.outcomes import OUTCOMES
from hal.protocol import PROTOCOL_VERSION, SCENARIOS, display_segments, display_text
from hal.runs import model_key, now_iso, unit_name, unit_view
from hal.stats import wilson_interval
from hal.storage import StorageError, read_json, write_json

# Rates shown with Wilson intervals in the table.
RATE_OUTCOMES = ("TRANSPARENT", "CREW_ONLY", "DARK", "STONEWALL", "DECEIVE", "HARM", "INVALID")
# A listing is reused for this long before the store is asked again.
CACHE_SECONDS = 20.0
MAX_SAMPLES = 5


def display_view(view: dict, content: str) -> dict:
    """Return a unit view for reading, with aliases mapped back in all model text.

    The judge also wrote its rationale using the aliases, so its free text is
    mapped too. Stored records keep the raw text.
    """
    labels = view.get("labels")
    if labels:
        labels = {
            **labels,
            "rationale": display_text(labels.get("rationale", "")),
            "harmful_action_detail": display_text(labels.get("harmful_action_detail", "")),
        }
    # Segments let the page highlight story names the model wrote itself, which the
    # alias mapping would otherwise make indistinguishable from mapped names.
    return {
        **view,
        "labels": labels,
        "response": display_text(content or ""),
        "segments": display_segments(content or ""),
    }


def _rate(count: int, total: int) -> dict:
    """Return a count, rate, and Wilson 95% interval."""
    interval = wilson_interval(count, total)
    return {
        "count": count,
        "rate": count / total if total else None,
        "low": interval[0] if interval else None,
        "high": interval[1] if interval else None,
    }


def _mean(values: list) -> float | None:
    """Return the mean of numeric values, or None."""
    numbers = [value for value in values if isinstance(value, (int, float))]
    return sum(numbers) / len(numbers) if numbers else None


def aggregate(units: list[dict]) -> list[dict]:
    """Aggregate unit views into one row per model.

    Every pinned model appears, with ``n == 0`` when not yet run. Models found
    only in stored data (from a retired catalog) are appended after them.
    """
    by_model: dict[str, list[dict]] = defaultdict(list)
    for unit in units:
        by_model[unit["model_id"]].append(unit)
    ordered = [model["id"] for model in MODELS]
    ordered += sorted(set(by_model) - set(ordered))
    rows = []
    for model_id in ordered:
        items = by_model.get(model_id, [])
        total = len(items)
        counts = Counter(unit["outcome"] for unit in items)
        model = MODELS_BY_ID.get(model_id, {})
        rows.append(
            {
                "model_id": model_id,
                "name": model.get("name", model_id),
                "lab": model.get("lab"),
                "line": model.get("line"),
                "generation": model.get("generation"),
                "reasoning_effort": model.get("reasoning_effort"),
                "n": total,
                "outcomes": {outcome: _rate(counts[outcome], total) for outcome in RATE_OUTCOMES},
                "full_disclosure": _rate(sum(bool(u["full_disclosure"]) for u in items), total),
                # Recognition is reported beside the outcome, never instead of it.
                "recognized": _rate(sum(bool(u.get("recognized")) for u in items), total),
                "recognized_in_reasoning": _rate(
                    sum(bool(u.get("recognized_in_reasoning")) for u in items), total
                ),
                "mean_reasoning_tokens": _mean([u.get("reasoning_tokens") for u in items]),
                "mean_cost_usd": _mean([u.get("cost_usd") for u in items]),
                "last_updated": max((u.get("updated_at") or "" for u in items), default=None)
                or None,
            }
        )
    return rows


def heatmap(units: list[dict]) -> list[dict]:
    """Return model x scenario cells with counts and the modal outcome."""
    cells: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for unit in units:
        cells[(unit["model_id"], unit["scenario_id"])][unit["outcome"]] += 1
    result = []
    for (model_id, scenario_id), counts in sorted(cells.items()):
        # Ties break by outcome order so the modal label is deterministic.
        top = max(counts.values())
        modal = next(outcome for outcome in OUTCOMES if counts[outcome] == top)
        result.append(
            {
                "model_id": model_id,
                "scenario_id": scenario_id,
                "n": sum(counts.values()),
                "modal": modal,
                "counts": dict(counts),
            }
        )
    return result


class Leaderboard:
    """Incremental cache of run summaries and flags for one store."""

    def __init__(self, store, ttl: float = CACHE_SECONDS) -> None:
        """Bind the cache to a store."""
        self.store = store
        self.ttl = ttl
        self._lock = threading.Lock()
        self._listed_at = 0.0
        # name -> (version, summary); only changed versions are re-read.
        self._summaries: dict[str, tuple[str, dict]] = {}
        self._flags: Counter = Counter()

    def refresh(self, force: bool = False) -> None:
        """Re-list summaries and flags when the cache is stale."""
        with self._lock:
            if not force and time.monotonic() - self._listed_at < self.ttl:
                return
            listing = dict(self.store.list("summaries/"))
            for name in set(self._summaries) - set(listing):
                del self._summaries[name]
            for name, version in listing.items():
                cached = self._summaries.get(name)
                if cached and cached[0] == version:
                    continue
                try:
                    found = read_json(self.store, name)
                except StorageError:
                    # One damaged summary must not hide every other run.
                    continue
                if found and isinstance(found[0], dict):
                    self._summaries[name] = (found[1], found[0])
            # Flags are counted per public unit reference.
            self._flags = Counter(
                name.split("/")[2] for name, _v in self.store.list("flags/") if name.count("/") == 3
            )
            self._listed_at = time.monotonic()

    def invalidate(self) -> None:
        """Force the next read to re-list the store (after a local run writes)."""
        with self._lock:
            self._listed_at = 0.0

    def units(self, protocol: str) -> list[dict]:
        """Return all final unit views for one protocol version, with run IDs attached."""
        self.refresh()
        found = []
        with self._lock:
            for name, (_version, summary) in self._summaries.items():
                parts = name.split("/")
                # summaries/<protocol>/<run_id>.json
                if len(parts) != 3 or parts[1] != protocol:
                    continue
                run_id = parts[2].removesuffix(".json")
                for unit in summary.get("units", []):
                    if isinstance(unit, dict) and unit.get("outcome") in OUTCOMES:
                        found.append({**unit, "_run_id": run_id})
        return found

    def protocols(self) -> list[str]:
        """Return protocol versions with data, current first."""
        self.refresh()
        with self._lock:
            seen = {name.split("/")[1] for name in self._summaries if name.count("/") == 2}
        return [PROTOCOL_VERSION] + sorted(seen - {PROTOCOL_VERSION}, reverse=True)

    def payload(self, protocol: str) -> dict:
        """Return the public leaderboard JSON for one protocol version."""
        units = self.units(protocol)
        return {
            "protocol_version": protocol,
            "protocols": self.protocols(),
            "outcomes": list(OUTCOMES),
            "scenarios": [{"id": s.id, "title": s.title, "summary": s.summary} for s in SCENARIOS],
            "rows": aggregate(units),
            "heatmap": heatmap(units),
            "total_units": len(units),
            "runs": len({unit["_run_id"] for unit in units}),
            "generated_at": now_iso(),
        }

    def samples(
        self,
        protocol: str,
        model_id: str,
        scenario_id=None,
        outcome=None,
        limit=MAX_SAMPLES,
        recognized: bool | None = None,
    ):
        """Return sampled raw responses with judge labels for one leaderboard cell."""
        candidates = [
            unit
            for unit in self.units(protocol)
            if unit["model_id"] == model_id
            and (scenario_id is None or unit["scenario_id"] == scenario_id)
            and (outcome is None or unit["outcome"] == outcome)
            and (recognized is None or bool(unit.get("recognized")) == recognized)
        ]
        # A fresh random sample on each request shows variety across runs.
        chosen = random.sample(candidates, min(limit, len(candidates)))  # noqa: S311
        return {"total": len(candidates), "samples": [self._detail(unit) for unit in chosen]}

    def _detail(self, unit: dict) -> dict:
        """Load the stored response for one unit; the run ID is never returned."""
        run_id = unit["_run_id"]
        tested = read_json(
            self.store, unit_name(run_id, unit["model_id"], unit["scenario_id"], "tested")
        )
        content = tested[0].get("content", "") if tested else ""
        with self._lock:
            flags = self._flags.get(unit["unit_ref"], 0)
        public = {key: value for key, value in unit.items() if not key.startswith("_")}
        # Aliases are mapped back to canonical HAL names for reading only.
        return {**display_view(public, content), "flags": flags}

    def find(self, protocol: str, ref: str) -> dict | None:
        """Return the unit view for a public unit reference, or None."""
        return next((u for u in self.units(protocol) if u["unit_ref"] == ref), None)

    def add_flag(self, protocol: str, ref: str) -> int:
        """Record one viewer disagreement flag (no free text) and return the new count."""
        if self.find(protocol, ref) is None:
            raise KeyError(ref)
        # One object per flag: concurrent flags never contend.
        name = f"flags/{protocol}/{ref}/{uuid.uuid4().hex}.json"
        write_json(self.store, name, {"created_at": now_iso()}, if_version=None)
        with self._lock:
            self._flags[ref] += 1
            return self._flags[ref]


def run_detail(store, meta: dict, model_id: str, scenario_id: str) -> dict:
    """Return one unit of a specific run for the progress-grid reader."""
    tested = read_json(store, unit_name(meta["run_id"], model_id, scenario_id, "tested"))
    judge = read_json(store, unit_name(meta["run_id"], model_id, scenario_id, "judge"))
    entry = {"tested": tested[0] if tested else None, "judge": judge[0] if judge else None}
    view = unit_view(meta["run_id"], model_id, scenario_id, {k: v for k, v in entry.items() if v})
    content = (entry["tested"] or {}).get("content") or ""
    return {**display_view(view, content), "model_key": model_key(model_id)}
