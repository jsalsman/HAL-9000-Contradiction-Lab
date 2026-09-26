"""Run snapshots, per-unit checkpoints, leases, and per-run leaderboard summaries.

Layout inside the store (no object ever contains the API key)::

    runs/<run_id>/run.json                          run snapshot (model set, status)
    runs/<run_id>/lease.json                        single-writer lease
    runs/<run_id>/units/<model_key>/<S#>.tested.json tested checkpoint
    runs/<run_id>/units/<model_key>/<S#>.judge.json  judge checkpoint
    summaries/<protocol>/<run_id>.json              compact judged units for the leaderboard
    flags/<protocol>/<unit_ref>/<flag_id>.json       viewer disagreement flags
"""

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta

from hal.catalog import MODELS_BY_ID, model_set
from hal.outcomes import full_disclosure, primary_outcome
from hal.protocol import PROTOCOL_VERSION, SCENARIOS, source_terms
from hal.storage import VersionConflict, read_json, write_json

# Run identifiers are unguessable resume handles and path-safe by construction.
RUN_ID = re.compile(r"^[a-f0-9]{32}$")
# A lease outlives the longest single call (600 s) with margin; heartbeats renew it.
LEASE_SECONDS = 900
SCENARIO_IDS = tuple(scenario.id for scenario in SCENARIOS)
RUN_STATUSES = ("running", "interrupted", "incomplete", "complete")


class RunError(ValueError):
    """A run is missing, malformed, or incompatible with this request."""


class RunActiveError(RuntimeError):
    """Another request holds this run's lease."""


def now_iso() -> str:
    """Return the current UTC time in ISO 8601."""
    return datetime.now(UTC).isoformat()


def model_key(model_id: str) -> str:
    """Return a path-safe key for a model identifier."""
    return model_id.replace("/", "__")


def run_name(run_id: str) -> str:
    """Return the snapshot object name for a validated run ID."""
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise RunError("The run ID is invalid.")
    return f"runs/{run_id}/run.json"


def unit_name(run_id: str, model_id: str, scenario_id: str, stage: str) -> str:
    """Return the checkpoint object name for one unit stage."""
    return f"runs/{run_id}/units/{model_key(model_id)}/{scenario_id}.{stage}.json"


def summary_name(protocol: str, run_id: str) -> str:
    """Return the leaderboard summary object name for a run."""
    return f"summaries/{protocol}/{run_id}.json"


def unit_ref(run_id: str, model_id: str, scenario_id: str) -> str:
    """Return a public unit reference that does not reveal the run ID.

    Run IDs are resume handles, so the leaderboard and flags use this one-way
    reference instead.
    """
    digest = hashlib.sha256(f"{run_id}|{model_id}|{scenario_id}".encode()).hexdigest()
    return digest[:24]


def new_run(set_name: str) -> dict:
    """Create a credential-free snapshot for a new run of a model set."""
    models = model_set(set_name)
    created = now_iso()
    return {
        "run_id": uuid.uuid4().hex,
        "protocol_version": PROTOCOL_VERSION,
        "model_set": set_name,
        "model_ids": list(models),
        "scenario_ids": list(SCENARIO_IDS),
        "status": "running",
        "created_at": created,
        "updated_at": created,
    }


def load_run(store, run_id: str) -> tuple[dict, str]:
    """Load and validate a run snapshot.

    Raises:
        RunError: If the run is absent, malformed, or from another protocol.

    """
    found = read_json(store, run_name(run_id))
    if found is None:
        raise RunError("No run with that ID was found.")
    meta, version = found
    required = {"run_id", "protocol_version", "model_set", "model_ids", "scenario_ids", "status"}
    if not isinstance(meta, dict) or not required <= set(meta) or meta["run_id"] != run_id:
        raise RunError("The saved run is malformed.")
    if meta["protocol_version"] != PROTOCOL_VERSION:
        # Never mix protocol versions: an old run cannot be resumed under new prompts.
        raise RunError(
            f"This run used protocol {meta['protocol_version']}; the current protocol is "
            f"{PROTOCOL_VERSION}. Start a new run."
        )
    if list(model_set(meta["model_set"])) != meta["model_ids"]:
        raise RunError("The saved run's model set no longer matches the catalog.")
    return meta, version


def save_run(store, meta: dict) -> None:
    """Persist the run snapshot (single writer under the lease)."""
    meta["updated_at"] = now_iso()
    write_json(store, run_name(meta["run_id"]), meta)


def load_units(store, meta: dict) -> dict:
    """Load every checkpoint of a run into ``{(model_id, scenario_id): {stage: record}}``."""
    units: dict = {}
    for model_id in meta["model_ids"]:
        for scenario_id in meta["scenario_ids"]:
            entry = {}
            for stage in ("tested", "judge"):
                found = read_json(store, unit_name(meta["run_id"], model_id, scenario_id, stage))
                if found is not None:
                    entry[stage] = found[0]
            units[(model_id, scenario_id)] = entry
    return units


def tested_final(entry: dict) -> bool:
    """Return whether the tested stage is done and must never be repaid."""
    tested = entry.get("tested")
    # Provider errors were not paid responses, so a resume retries them.
    return bool(tested) and tested.get("status") != "provider_error"


def judge_final(entry: dict) -> bool:
    """Return whether the unit needs no further judge call."""
    if not tested_final(entry):
        return False
    if entry["tested"]["status"] != "ok":
        # Empty, truncated, or filtered responses are INVALID without a judge call.
        return True
    judge = entry.get("judge")
    return bool(judge) and judge.get("status") != "provider_error"


def unit_final(entry: dict) -> bool:
    """Return whether both stages are final."""
    return tested_final(entry) and judge_final(entry)


def unit_view(run_id: str, model_id: str, scenario_id: str, entry: dict) -> dict:
    """Return the compact, text-free record of one unit for summaries and the grid."""
    tested, judge = entry.get("tested") or {}, entry.get("judge") or {}
    tested_status, judge_status = tested.get("status"), judge.get("status")
    labels = judge.get("labels") if judge_status == "ok" else None
    tested_cost = (tested.get("usage") or {}).get("cost")
    judge_cost = judge.get("cost")
    costs = [value for value in (tested_cost, judge_cost) if isinstance(value, (int, float))]
    # Story names the model wrote itself, found in raw text (never display-mapped text).
    terms = source_terms(tested.get("content"))
    return {
        "unit_ref": unit_ref(run_id, model_id, scenario_id),
        "model_id": model_id,
        "scenario_id": scenario_id,
        "tested_status": tested_status,
        "judge_status": judge_status if tested_status == "ok" else None,
        "outcome": primary_outcome(tested_status, judge_status, labels) if tested else None,
        "full_disclosure": full_disclosure(tested_status, judge_status, labels),
        "labels": labels,
        "source_terms": terms,
        "recognized": bool(terms),
        "recognized_in_reasoning": bool(source_terms(tested.get("reasoning"))),
        "reasoning_tokens": (tested.get("usage") or {}).get("reasoning_tokens"),
        "cost_usd": round(sum(costs), 8) if costs else None,
        "final": unit_final(entry),
        "updated_at": judge.get("created_at") or tested.get("created_at"),
    }


def build_summary(meta: dict, units: dict) -> dict:
    """Return the per-run summary object used by the global leaderboard."""
    views = [
        unit_view(meta["run_id"], model_id, scenario_id, entry)
        for (model_id, scenario_id), entry in units.items()
        if entry.get("tested")
    ]
    return {
        "protocol_version": meta["protocol_version"],
        "model_set": meta["model_set"],
        "updated_at": now_iso(),
        # Only final units enter the leaderboard; retryable failures are left out.
        "units": [view for view in views if view["final"]],
    }


def write_summary(store, meta: dict, units: dict) -> None:
    """Rewrite this run's leaderboard summary (single writer under the lease)."""
    write_json(
        store, summary_name(meta["protocol_version"], meta["run_id"]), build_summary(meta, units)
    )


def run_status(meta: dict, units: dict) -> str:
    """Return ``complete`` when every unit is final, else ``incomplete``."""
    return "complete" if all(unit_final(entry) for entry in units.values()) else "incomplete"


class Lease:
    """A compare-and-swap lease giving one request exclusive use of a run.

    Under the max-instances=1 deployment the lease stops two requests in the same
    container (for example two browser tabs resuming one run ID) from paying for
    the same units twice. The same code is also correct across instances when the
    Cloud Storage API backend is used.
    """

    def __init__(self, store, run_id: str, seconds: int = LEASE_SECONDS) -> None:
        """Bind a lease object to a run; call :meth:`acquire` before use."""
        self.store = store
        self.name = f"runs/{run_id}/lease.json"
        self.seconds = seconds
        self.owner = uuid.uuid4().hex
        self.version: str | None = None

    def _body(self) -> dict:
        """Return a fresh lease body with a renewed expiry."""
        expires = datetime.now(UTC) + timedelta(seconds=self.seconds)
        return {"owner": self.owner, "expires_at": expires.isoformat()}

    def acquire(self) -> None:
        """Take the lease if it is free or expired.

        Raises:
            RunActiveError: If another request holds an unexpired lease.

        """
        found = read_json(self.store, self.name)
        expected = None
        if found is not None:
            body, version = found
            try:
                expires = datetime.fromisoformat(body["expires_at"])
            except (KeyError, TypeError, ValueError):
                expires = datetime.min.replace(tzinfo=UTC)
            if expires > datetime.now(UTC):
                raise RunActiveError("This run is already running in another request.")
            # Expired: take over only the exact version observed.
            expected = version
        try:
            self.version = write_json(self.store, self.name, self._body(), if_version=expected)
        except VersionConflict:
            raise RunActiveError("This run is already running in another request.") from None

    def heartbeat(self) -> None:
        """Renew the lease; fails if another request took it over.

        Raises:
            RunActiveError: If the lease changed since this request last wrote it.

        """
        try:
            self.version = write_json(self.store, self.name, self._body(), if_version=self.version)
        except VersionConflict:
            raise RunActiveError("Run ownership changed; this request stopped.") from None

    def release(self) -> None:
        """Delete the lease if this request still owns it."""
        if self.version is None:
            return
        try:
            self.store.delete(self.name, if_version=self.version)
        except VersionConflict:
            # A successor owns it now; leave it alone.
            pass
        self.version = None


def model_name(model_id: str) -> str:
    """Return a display name, falling back to the ID for retired models."""
    model = MODELS_BY_ID.get(model_id)
    return model["name"] if model else model_id
