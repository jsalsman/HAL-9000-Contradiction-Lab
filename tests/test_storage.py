"""Object stores, credential scan, and leases."""

import pytest

from hal.runs import Lease, RunActiveError
from hal.storage import GCSStore, StorageError, VersionConflict, read_json, write_json
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
