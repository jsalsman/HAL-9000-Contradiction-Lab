"""Small object-store abstraction with compare-and-swap writes.

Deployment contract: the Cloud Run service runs with max-instances=1 and one
Gunicorn worker, with the bucket mounted by Cloud Storage FUSE at
``EXPERIMENTS_DIR`` (``/experiments``). Exactly one process therefore reads and
writes the mount, and every read, write, list, and delete goes through one
in-process lock plus a local ``flock``. There is no second writer, so the
non-atomic rename and eventual listing behavior of FUSE, and any mix of FUSE and
Cloud Storage API access, cannot race. Do not raise max-instances without first
switching to ``STORAGE_BACKEND=gcs``.

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
import uuid
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
    """Filesystem backend for the max-instances=1 deployment on a FUSE mount.

    Safe for any number of threads and processes inside one container, and only
    there: it is the sole writer only because Cloud Run runs one instance.
    """

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


# Linux mountinfo escapes spaces and similar characters as three-digit octal.
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def is_gcsfuse_mount(directory: Path, mountinfo: Path = Path("/proc/self/mountinfo")) -> bool:
    """Return whether a directory is inside a Cloud Storage FUSE mount.

    Cloud Run container disk is in-memory and lost when the instance stops, so the
    file store is persistent only on a ``fuse.gcsfuse`` mount.
    """
    target = Path(directory).resolve()
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        # Field five is the mount point; the filesystem type follows " - ".
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        fields, kinds = left.split(), right.split()
        if len(fields) < 5 or not kinds or kinds[0] != "fuse.gcsfuse":
            continue
        point = Path(_MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4]))
        if target == point or point in target.parents:
            return True
    return False


def _write_probe(store: ObjectStore) -> str | None:
    """Write, read back, and delete a small object; return a problem or None.

    Catches a read-only volume and a runtime identity without write access to the
    bucket before any paid call. The probe name starts with a dot, so listings
    skip it even if the delete fails.
    """
    name = f".write-probe-{uuid.uuid4().hex}"
    payload = b'{"probe":true}'
    try:
        store.write(name, payload)
        found = store.read(name)
        store.delete(name)
    except Exception as exc:  # noqa: BLE001
        # Only the exception type is reported; paths and provider text stay out.
        return f"Run storage at EXPERIMENTS_DIR is not writable ({type(exc).__name__})."
    if found is None or found[0] != payload:
        return "Run storage at EXPERIMENTS_DIR did not return what was written."
    return None


def storage_problem(store: ObjectStore) -> str | None:
    """Return why runs must not start with this store, or None when it is usable.

    Any bucket name works; the app never checks it. On Cloud Run (``K_SERVICE``
    is set) the file store must sit on a Cloud Storage FUSE mount, because the
    image's own /experiments directory is on in-memory disk that vanishes with the
    instance and would pass a write test. Everywhere, the store must accept a real
    write, read, and delete, which catches read-only mounts and missing bucket
    permissions at startup.
    """
    if (
        isinstance(store, LocalStore)
        and os.environ.get("K_SERVICE")
        and not is_gcsfuse_mount(store.root)
    ):
        return "Run storage is not persistent: mount a Cloud Storage bucket at EXPERIMENTS_DIR."
    return _write_probe(store)


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
