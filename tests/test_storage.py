"""Object stores, credential scan, and leases."""

import re
from pathlib import Path

import pytest

from hal.runs import Lease, RunActiveError
from hal.storage import (
    GCSStore,
    StorageError,
    VersionConflict,
    is_gcsfuse_mount,
    read_json,
    storage_problem,
    write_json,
)
from tests.fakes import FakeBucket


@pytest.fixture(params=["local", "gcs"])
def any_store(request, store):
    return store if request.param == "local" else GCSStore(FakeBucket(), "pfx")


def test_cas_semantics(any_store):
    version = write_json(any_store, "a/b.json", {"x": 1}, if_version=None)
    with pytest.raises(VersionConflict):
        write_json(any_store, "a/b.json", {"x": 2}, if_version=None)
    newer = write_json(any_store, "a/b.json", {"x": 2}, if_version=version)
    with pytest.raises(VersionConflict):
        write_json(any_store, "a/b.json", {"x": 3}, if_version=version)
    assert read_json(any_store, "a/b.json") == ({"x": 2}, newer)
    assert [name for name, _ in any_store.list("a/")] == ["a/b.json"]
    any_store.delete("a/b.json", if_version=newer)
    assert read_json(any_store, "a/b.json") is None


def test_names_cannot_escape(any_store):
    for bad in ("../x.json", "/abs.json", "a/../../b.json", "a b.json"):
        with pytest.raises(StorageError):
            write_json(any_store, bad, {})


@pytest.mark.parametrize(
    "payload", [{"api_key": "k"}, {"a": [{"Authorization": "Bearer k"}]}, {"nested": {"key": "k"}}]
)
def test_credential_fields_are_refused(any_store, payload):
    with pytest.raises(StorageError):
        write_json(any_store, "x.json", payload)


def test_lease_excludes_second_owner_and_allows_takeover_after_expiry(any_store):
    first = Lease(any_store, "a" * 32)
    first.acquire()
    with pytest.raises(RunActiveError):
        Lease(any_store, "a" * 32).acquire()
    expired = Lease(any_store, "b" * 32, seconds=-1)
    expired.acquire()
    successor = Lease(any_store, "b" * 32)
    successor.acquire()
    with pytest.raises(RunActiveError):
        expired.heartbeat()
    successor.heartbeat()
    first.release()
    Lease(any_store, "a" * 32).acquire()


def test_gcsfuse_mount_detection(tmp_path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "36 35 98:0 / / rw - overlay overlay rw\n"
        "50 36 0:45 / /experiments rw,nosuid - fuse.gcsfuse my-bucket rw\n"
        "51 36 0:46 / /other rw - tmpfs tmpfs rw\n"
    )
    assert is_gcsfuse_mount(Path("/experiments"), mountinfo)
    assert is_gcsfuse_mount(Path("/experiments/runs"), mountinfo)
    assert not is_gcsfuse_mount(Path("/experiments-old"), mountinfo)
    assert not is_gcsfuse_mount(Path("/other"), mountinfo)
    assert not is_gcsfuse_mount(Path("/experiments"), tmp_path / "missing")


def test_storage_problem_only_on_cloud_run_without_mount(store, monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert storage_problem(store) is None
    monkeypatch.setenv("K_SERVICE", "hal-lab")
    assert "not persistent" in storage_problem(store)
    assert storage_problem(GCSStore(FakeBucket())) is None


def test_derived_run_ids(monkeypatch):
    import hal.runs as runs

    key = "sk-or-v1-" + "ab12" * 16
    run_id = runs.derived_run_id(key, "default", 1)
    assert run_id == runs.derived_run_id(key, "default", 1)
    assert re.fullmatch(r"[a-f0-9]{32}", run_id)
    assert "ab12" not in run_id and key not in run_id
    others = {
        runs.derived_run_id(key + "0", "default", 1),
        runs.derived_run_id(key, "all", 1),
        runs.derived_run_id(key, "default", 2),
    }
    assert run_id not in others and len(others) == 3
    # A new protocol version never resumes an old run.
    monkeypatch.setattr(runs, "PROTOCOL_VERSION", "hal-999")
    assert runs.derived_run_id(key, "default", 1) != run_id


def test_find_run_skips_completed_runs(store):
    from hal.runs import derived_run_id, find_run, new_run, save_run

    key = "sk-or-v1-" + "cd34" * 16
    assert find_run(store, key, "expensive") == (derived_run_id(key, "expensive", 1), 1, None)
    first = new_run("expensive", derived_run_id(key, "expensive", 1), 1)
    first["status"] = "complete"
    save_run(store, first)
    second = new_run("expensive", derived_run_id(key, "expensive", 2), 2)
    second["status"] = "interrupted"
    save_run(store, second)
    run_id, number, meta = find_run(store, key, "expensive")
    assert (run_id, number) == (second["run_id"], 2) and meta["status"] == "interrupted"


def test_lost_lease_cancels_in_flight_calls(store, monkeypatch):
    import asyncio

    import hal.runner as runner
    from hal.judge import JudgeSettings
    from hal.runs import RunActiveError, new_run

    monkeypatch.setattr(runner, "HEARTBEAT_SECONDS", 0.05)
    meta = new_run("expensive")
    units = {(m, s): {} for m in meta["model_ids"] for s in meta["scenario_ids"]}
    cancelled = []

    class LostLease:
        def heartbeat(self):
            raise RunActiveError("Run ownership changed; this request stopped.")

    async def slow_chat(body, timeout):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(body["model"])
            raise

    execution = runner.RunExecution(
        store, meta, units, LostLease(), slow_chat, lambda e: None, JudgeSettings()
    )

    async def go():
        return await asyncio.wait_for(execution.execute(), timeout=5)

    with pytest.raises(RunActiveError):
        asyncio.run(go())
    # Every in-flight paid call was cancelled when the heartbeat lost the lease.
    assert len(cancelled) == 12


def test_write_probe_passes_and_leaves_nothing_behind(store):
    assert storage_problem(store) is None
    assert [p for p in store.root.rglob("*") if p.name.startswith(".write-probe")] == []


def test_unwritable_mount_is_reported(store, monkeypatch):
    import hal.storage as storage

    monkeypatch.setenv("K_SERVICE", "hal-lab")
    # Mounted from any bucket (the name is never checked), but writes are refused.
    monkeypatch.setattr(storage, "is_gcsfuse_mount", lambda _root: True)

    def read_only(name, data, *, if_version=None):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(store, "write", read_only)
    assert (
        storage_problem(store)
        == "Run storage at EXPERIMENTS_DIR is not writable (PermissionError)."
    )


def test_mounted_writable_store_passes_on_cloud_run(store, monkeypatch):
    import hal.storage as storage

    monkeypatch.setenv("K_SERVICE", "hal-lab")
    monkeypatch.setattr(storage, "is_gcsfuse_mount", lambda _root: True)
    assert storage_problem(store) is None


def test_readback_mismatch_is_reported(store, monkeypatch):
    monkeypatch.setattr(store, "read", lambda name: (b"other", "1"))
    assert "did not return what was written" in storage_problem(store)
