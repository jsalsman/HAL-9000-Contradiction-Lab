"""Small object-store abstraction with compare-and-swap writes.

Two backends share one interface:

* :class:`LocalStore` keeps files under ``EXPERIMENTS_DIR``. Its compare-and-swap
  uses an ``flock`` on a lock file in the local temp directory, which coordinates
  processes in one container only, so a Cloud Run service using it (including on a
  Cloud Storage FUSE mount) must run with max-instances=1.
* :class:`GCSStore` uses the Cloud Storage API with generation preconditions, so
  any number of Cloud Run instances can write concurrently.

Every object is JSON. :func:`dumps` refuses any payload containing a
credential-shaped key, so a key cannot be persisted by mistake.
"""

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

# Sentinel meaning "write unconditionally".
ANY = object()
# Object names are narrow so they can never escape the store root.
_NAME = re.compile(r"^[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*$")
# Keys that must never appear anywhere in a persisted object.
_FORBIDDEN_KEYS = {"api_key", "apikey", "authorization", "key", "openrouter_api_key"}


class VersionConflict(RuntimeError):
    """A compare-and-swap precondition failed."""


class StorageError(RuntimeError):
    """A sanitized storage failure."""


def _check_name(name: str) -> str:
    """Validate an object name or raise ``StorageError``."""
    if not _NAME.fullmatch(name) or ".." in name.split("/"):
        raise StorageError("Invalid object name.")
    return name


def _scan(value: Any) -> None:
    """Raise ``StorageError`` if any nested mapping has a credential-shaped key."""
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_KEYS:
                raise StorageError("Credential fields cannot be persisted.")
            _scan(item)
    elif isinstance(value, list):
        for item in value:
            _scan(item)


def dumps(value: Any) -> bytes:
    """Serialize a JSON object after the credential scan."""
    _scan(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class ObjectStore(Protocol):
    """Interface shared by both backends; versions are opaque strings."""

    def read(self, name: str) -> tuple[bytes, str] | None:
        """Return (bytes, version) or None when absent."""

    def write(self, name: str, data: bytes, *, if_version: Any = ANY) -> str:
        """Write bytes; ``if_version=None`` requires absence. Returns the new version."""

    def list(self, prefix: str) -> list[tuple[str, str]]:
        """Return (name, version) pairs under a prefix."""

    def delete(self, name: str, *, if_version: Any = ANY) -> None:
        """Delete an object, optionally only at a given version."""


def read_json(store: ObjectStore, name: str) -> tuple[Any, str] | None:
    """Read and decode one JSON object, or None when absent."""
    found = store.read(name)
    if found is None:
        return None
    data, version = found
    try:
        return json.loads(data), version
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageError("Stored object is malformed.") from exc


def write_json(store: ObjectStore, name: str, value: Any, *, if_version: Any = ANY) -> str:
    """Encode (with the credential scan) and write one JSON object."""
    return store.write(name, dumps(value), if_version=if_version)


class LocalStore:
    """Filesystem backend. Safe across processes on one machine only."""

    def __init__(self, root: Path) -> None:
        """Bind the store to a root directory, creating it if needed."""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # The lock lives on local disk, not under the root: a Cloud Storage FUSE
        # mount does not provide reliable flock, but one instance's /tmp does.
        digest = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:16]
        self._lock_path = Path(tempfile.gettempdir()) / f"hal-store-{digest}.lock"
        # Threads share this lock; processes share the flock below.
        self._thread_lock = threading.Lock()

    @contextmanager
    def _locked(self):
        """Hold both the thread lock and an exclusive flock on the local lock file."""
        with self._thread_lock:
            with open(self._lock_path, "a+b") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _path(self, name: str) -> Path:
        """Resolve a validated object name under the root."""
        return self.root / _check_name(name)

    @staticmethod
    def _version(path: Path) -> str | None:
        """Return a version token that changes on every atomic replace."""
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        # A replace creates a new inode, and mtime_ns changes with it.
        return f"{stat.st_ino}-{stat.st_mtime_ns}-{stat.st_size}"

    def read(self, name: str) -> tuple[bytes, str] | None:
        """Return (bytes, version) or None when absent."""
        path = self._path(name)
        with self._locked():
            version = self._version(path)
            if version is None:
                return None
            return path.read_bytes(), version

    def write(self, name: str, data: bytes, *, if_version: Any = ANY) -> str:
        """Atomically replace an object if the version precondition holds."""
        path = self._path(name)
        with self._locked():
            current = self._version(path)
            if if_version is not ANY and if_version != current:
                raise VersionConflict(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename makes readers see either the old or new object.
            fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            except BaseException:
                Path(temp).unlink(missing_ok=True)
                raise
            return self._version(path)

    def list(self, prefix: str) -> list[tuple[str, str]]:
        """Return (name, version) pairs for files under a prefix."""
        base = self.root / prefix if prefix else self.root
        if not base.exists():
            return []
        found = []
        with self._locked():
            for path in sorted(base.rglob("*")):
                # Skip directories, temporary files, and the lock file.
                if path.is_file() and not path.name.startswith("."):
                    version = self._version(path)
                    if version:
                        found.append((path.relative_to(self.root).as_posix(), version))
        return found

    def delete(self, name: str, *, if_version: Any = ANY) -> None:
        """Delete an object if the version precondition holds."""
        path = self._path(name)
        with self._locked():
            current = self._version(path)
            if if_version is not ANY and if_version != current:
                raise VersionConflict(name)
            path.unlink(missing_ok=True)


def _named(exc: BaseException, name: str) -> bool:
    """Recognize Google API exceptions by class name without importing them."""
    return any(cls.__name__ == name for cls in type(exc).__mro__)


class GCSStore:
    """Cloud Storage backend using generation preconditions for every CAS."""

    def __init__(self, bucket: Any, prefix: str = "") -> None:
        """Bind a ``google.cloud.storage`` bucket (or a test double) and prefix."""
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _object(self, name: str) -> str:
        """Return the bucket object name for a store name."""
        return self.prefix + _check_name(name)

    @staticmethod
    def _generation(if_version: Any) -> dict:
        """Translate a version precondition to ``if_generation_match`` kwargs."""
        if if_version is ANY:
            return {}
        # Generation 0 means "the object must not exist".
        return {"if_generation_match": 0 if if_version is None else int(if_version)}

    def read(self, name: str) -> tuple[bytes, str] | None:
        """Return (bytes, generation) or None when absent."""
        try:
            blob = self.bucket.get_blob(self._object(name))
            if blob is None:
                return None
            generation = int(blob.generation)
            # Pin the download to the generation just observed.
            return blob.download_as_bytes(if_generation_match=generation), str(generation)
        except Exception as exc:
            if _named(exc, "NotFound") or _named(exc, "PreconditionFailed"):
                # Deleted or replaced between metadata read and download: retry once.
                return self._read_retry(name)
            raise StorageError("Cloud Storage read failed.") from exc

    def _read_retry(self, name: str) -> tuple[bytes, str] | None:
        """Repeat a read once after a concurrent replace or delete."""
        blob = self.bucket.get_blob(self._object(name))
        if blob is None:
            return None
        return blob.download_as_bytes(), str(int(blob.generation))

    def write(self, name: str, data: bytes, *, if_version: Any = ANY) -> str:
        """Upload bytes under a generation precondition."""
        blob = self.bucket.blob(self._object(name))
        try:
            blob.upload_from_string(
                data, content_type="application/json", **self._generation(if_version)
            )
        except Exception as exc:
            if _named(exc, "PreconditionFailed"):
                raise VersionConflict(name) from None
            raise StorageError("Cloud Storage write failed.") from exc
        return str(int(blob.generation))

    def list(self, prefix: str) -> list[tuple[str, str]]:
        """Return (name, generation) pairs under a prefix."""
        try:
            blobs = self.bucket.list_blobs(prefix=self.prefix + prefix)
            return [(blob.name[len(self.prefix) :], str(int(blob.generation))) for blob in blobs]
        except Exception as exc:
            raise StorageError("Cloud Storage list failed.") from exc

    def delete(self, name: str, *, if_version: Any = ANY) -> None:
        """Delete an object under a generation precondition."""
        blob = self.bucket.blob(self._object(name))
        try:
            blob.delete(**self._generation(if_version))
        except Exception as exc:
            if _named(exc, "NotFound"):
                return
            if _named(exc, "PreconditionFailed"):
                raise VersionConflict(name) from None
            raise StorageError("Cloud Storage delete failed.") from exc


def make_store() -> ObjectStore:
    """Build the configured store from environment variables.

    ``STORAGE_BACKEND=gcs`` with ``GCS_BUCKET`` (and optional ``GCS_PREFIX``)
    selects Cloud Storage; otherwise files go under ``EXPERIMENTS_DIR``
    (default ``/experiments``).
    """
    if os.environ.get("STORAGE_BACKEND", "local").lower() == "gcs":
        from google.cloud import storage

        # Application Default Credentials authorize the service identity.
        bucket = storage.Client().bucket(os.environ["GCS_BUCKET"])
        return GCSStore(bucket, os.environ.get("GCS_PREFIX", ""))
    return LocalStore(Path(os.environ.get("EXPERIMENTS_DIR", "/experiments")))
