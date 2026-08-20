"""Sovereign Memory v1 event store and rebuildable SQLite projection.

The append-only JSONL files are authoritative. SQLite is deliberately treated as
a disposable projection: every row can be recreated by :meth:`MemoryStore.replay`.
This module contains no model, network, vector, or background-job integration.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from agent_continuity.compaction import (
    TASK_STATE_SCHEMA as TASK_STATE_SCHEMA,
)
from agent_continuity.compaction import (
    TaskStateError,
    task_state_sha256,
    validate_task_state,
)
from agent_continuity.retention import (
    BLOB_REHYDRATION_SCHEMA,
    BULK_ARCHIVE_SECONDS,
    BULK_HOT_SECONDS,
    BULK_REFUSAL_BASIS_POINTS,
    EVIDENCE_HOT_SECONDS,
    EXCERPT_CHARACTERS,
    RETENTION_RESULT_SCHEMA,
    RETENTION_TOMBSTONE_SCHEMA,
    SCRATCH_HOT_SECONDS,
    RetentionContractError,
    RetentionDecision,
    RetentionPlan,
)
from agent_continuity.retention import (
    canonical_bytes as retention_canonical_bytes,
)
from agent_continuity.retention import (
    sha256 as retention_sha256,
)

EVENT_SCHEMA = "coding-intelligence-memory-event/v1"
BLOB_THRESHOLD_BYTES = 16 * 1024
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
DEFAULT_HOT_BLOB_BUDGET_BYTES = 16 * 1024 * 1024 * 1024
PROJECTION_SCHEMA_VERSION = "2"
_LOCK_POLL_SECONDS = 0.025

_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EVENT_FIELDS = {
    "schema",
    "event_id",
    "host_id",
    "session_id",
    "task_id",
    "parent_event_id",
    "seq",
    "occurred_at",
    "type",
    "actor",
    "scope",
    "payload",
    "payload_sha256",
    "previous_record_sha256",
    "record_sha256",
}
_MEMORY_KINDS = {
    "constraint",
    "preference",
    "decision",
    "fact",
    "lesson",
    "episode",
    "procedure_ref",
}
_MEMORY_STATUSES = {"active", "superseded", "retired", "disputed"}
_VERIFICATION_STATUSES = {
    "user_authority",
    "observed",
    "tested",
    "source_verified",
    "inferred",
}
_EVIDENCE_KINDS = {
    "user",
    "event",
    "file",
    "symbol",
    "command",
    "test",
    "web",
    "artifact",
    "model_inference",
}
_ACTOR_KINDS = {"user", "agent", "tool", "system"}
_RETENTION_CLASSES = {"core", "evidence", "bulk", "scratch"}


class MemoryStoreError(RuntimeError):
    """Base error for an invalid store operation or durable record."""


class CapabilityError(MemoryStoreError):
    """The local SQLite runtime cannot provide the required retrieval channels."""


class ChainValidationError(MemoryStoreError):
    """A canonical event, hash chain, or content-addressed blob is invalid."""


class ProjectionError(MemoryStoreError):
    """An event cannot be projected without violating the Memory v1 contract."""


class StoreLockTimeout(MemoryStoreError):
    """Another live process held the store lock beyond the bounded wait."""


class RetentionPressureError(MemoryStoreError):
    """An unpinned bulk payload would meet or exceed the refusal threshold."""


class BlobEvictedError(ChainValidationError):
    """A non-authoritative payload was deliberately evicted with a valid tombstone."""


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


@dataclass(frozen=True)
class MemoryScope:
    """An immutable, exact retrieval scope.

    ``None`` is a value, not a wildcard. Callers express the documented default
    union (owner-global + workspace + task) by supplying all desired scopes.
    """

    owner_id: str
    workspace_id: str | None = None
    task_id: str | None = None
    agent_role: str | None = None
    session_id: str | None = None

    @classmethod
    def from_value(cls, value: MemoryScope | Mapping[str, Any]) -> MemoryScope:
        if isinstance(value, cls):
            scope = value
        elif isinstance(value, Mapping):
            unknown = set(value) - {
                "owner_id",
                "workspace_id",
                "task_id",
                "agent_role",
                "session_id",
            }
            if unknown:
                raise MemoryStoreError(f"unknown scope fields: {sorted(unknown)}")
            try:
                scope = cls(**value)
            except TypeError as error:
                raise MemoryStoreError(f"invalid scope: {error}") from error
        else:
            raise MemoryStoreError("scope must be MemoryScope or a mapping")
        if not isinstance(scope.owner_id, str) or not scope.owner_id.strip():
            raise MemoryStoreError("scope owner_id must be a non-empty string")
        for name in ("workspace_id", "task_id", "agent_role", "session_id"):
            item = getattr(scope, name)
            if item is not None and (not isinstance(item, str) or not item):
                raise MemoryStoreError(f"scope {name} must be null or a non-empty string")
        return scope

    @property
    def key(self) -> str:
        # Canonical JSON is unambiguous and escapes delimiter-like user text.
        return _canonical_text(
            [self.owner_id, self.workspace_id, self.task_id, self.agent_role, self.session_id]
        )


@dataclass(frozen=True)
class VerificationReport:
    segment_count: int
    event_count: int
    blob_count: int
    last_hashes: dict[str, str]


@dataclass(frozen=True)
class ReplayReport:
    verified_events: int
    applied_events: int
    rebuilt: bool


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise MemoryStoreError(f"value is not canonical JSON: {error}") from error


def _canonical_text(value: Any) -> str:
    return _canonical_bytes(value).decode("utf-8")


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _uuid_text(value: str, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise MemoryStoreError(f"{field} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise MemoryStoreError(f"{field} must be a UUID string") from error
    if str(parsed) != value.lower():
        raise MemoryStoreError(f"{field} must use canonical UUID spelling")
    return str(parsed)


def _utc_text(value: str | datetime | None = None) -> str:
    if value is None:
        moment = datetime.now(UTC)
    elif isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise MemoryStoreError("timestamp must be RFC3339") from error
    else:
        raise MemoryStoreError("timestamp must be RFC3339 text or datetime")
    if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(moment):
        raise MemoryStoreError("timestamp must be in UTC")
    moment = moment.astimezone(UTC)
    timespec = "microseconds" if moment.microsecond else "seconds"
    return moment.isoformat(timespec=timespec).replace("+00:00", "Z")


class MemoryStore:
    """Own canonical Memory v1 events beneath ``root`` and their projection."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        blob_threshold: int = BLOB_THRESHOLD_BYTES,
        rebuild_on_open: bool = False,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
        hot_blob_budget_bytes: int = DEFAULT_HOT_BLOB_BUDGET_BYTES,
    ):
        if type(rebuild_on_open) is not bool:
            raise MemoryStoreError("rebuild_on_open must be an exact boolean")
        configured_root = Path(os.path.abspath(os.fspath(root)))
        resolved_root = configured_root.resolve()
        if os.path.normcase(str(configured_root)) != os.path.normcase(str(resolved_root)):
            raise MemoryStoreError("memory root cannot be a symlink or junction path")
        self.root = resolved_root
        if blob_threshold < 256:
            raise MemoryStoreError("blob_threshold must be at least 256 bytes")
        if (
            isinstance(lock_timeout, bool)
            or not isinstance(lock_timeout, (int, float))
            or not math.isfinite(lock_timeout)
            or lock_timeout > threading.TIMEOUT_MAX
        ):
            raise MemoryStoreError("lock_timeout must be a finite positive number")
        if lock_timeout <= 0:
            raise MemoryStoreError("lock_timeout must be a finite positive number")
        if (
            not isinstance(hot_blob_budget_bytes, int)
            or isinstance(hot_blob_budget_bytes, bool)
            or hot_blob_budget_bytes < 1
        ):
            raise MemoryStoreError("hot_blob_budget_bytes must be a positive integer")
        self.blob_threshold = blob_threshold
        self.lock_timeout = float(lock_timeout)
        self.hot_blob_budget_bytes = hot_blob_budget_bytes
        self.events_dir = self.root / "events"
        self.blobs_dir = self.root / "blobs" / "sha256"
        self.archive_dir = self.root / "archive"
        self.tombstones_dir = self.root / "retention" / "tombstones" / "sha256"
        self.rehydration_dir = self.root / "retention" / "rehydration"
        self.database_path = self.root / "memory.sqlite3"
        self.lock_path = self.root / ".memory-v1.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        self._assert_internal_path(self.root, "memory root")
        self._ensure_retention_directory(self.events_dir, "event root")
        self._ensure_retention_directory(self.blobs_dir, "blob root")
        self._ensure_retention_directory(self.archive_dir, "archive root")
        self._ensure_retention_directory(self.tombstones_dir, "tombstone root")
        self._ensure_retention_directory(self.rehydration_dir, "rehydration root")
        self._assert_internal_path(self.database_path, "SQLite projection")
        self._assert_internal_path(self.lock_path, "lock rendezvous")
        self._lock = _process_lock(self.lock_path)
        projection_upgrade_required = False
        with self._exclusive_store_lock():
            with closing(self._connect()) as connection:
                self._probe_capabilities(connection)
                projection_upgrade_required = self._create_schema(connection)
            self._recover_incomplete_retention_artifacts_locked()
        self.replay(rebuild=rebuild_on_open or projection_upgrade_required)
        self._mark_projection_schema_current()
        self.reconcile_retention()

    @contextmanager
    def _exclusive_store_lock(self) -> Iterator[None]:
        """Hold the process-local and kernel-released store mutation locks."""

        deadline = time.monotonic() + self.lock_timeout
        acquired = self._lock.acquire(timeout=self.lock_timeout)
        if not acquired:
            raise StoreLockTimeout(
                f"timed out after {self.lock_timeout:.3f}s acquiring store lock "
                f"{self.lock_path}"
            )
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StoreLockTimeout(
                    f"timed out after {self.lock_timeout:.3f}s acquiring store lock "
                    f"{self.lock_path}"
                )
            with self._os_file_lock(remaining):
                yield
        finally:
            self._lock.release()

    @contextmanager
    def _os_file_lock(self, timeout: float) -> Iterator[None]:
        """Acquire a bounded advisory lock; the OS releases it on process death."""

        # The empty file is a permanent rendezvous inode. Never unlink or replace
        # it: doing so could let two processes lock different inodes. Ownership
        # lives only in the kernel lock, so there is no stale PID to diagnose.
        flags = os.O_RDWR | os.O_CREAT
        self._assert_internal_path(self.lock_path, "lock rendezvous")
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as error:
            raise MemoryStoreError(f"cannot open store lock {self.lock_path}: {error}") from error
        try:
            stream = os.fdopen(descriptor, "r+b", buffering=0)
        except Exception:
            os.close(descriptor)
            raise
        locked = False
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    if os.name == "nt":
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise MemoryStoreError(
                            f"cannot acquire store lock {self.lock_path}: {error}"
                        ) from error
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StoreLockTimeout(
                            f"timed out after {self.lock_timeout:.3f}s acquiring store lock "
                            f"{self.lock_path}"
                        ) from error
                    time.sleep(min(_LOCK_POLL_SECONDS, remaining))
            yield
        finally:
            try:
                if locked:
                    if os.name == "nt":
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    @staticmethod
    def _sync_file(descriptor: int) -> None:
        os.fsync(descriptor)
        if sys.platform == "darwin" and hasattr(fcntl, "F_FULLFSYNC"):
            fcntl.fcntl(descriptor, fcntl.F_FULLFSYNC)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        # Windows has no directory descriptor equivalent; _commit on the file
        # is the strongest portable stdlib guarantee there. POSIX also flushes
        # the directory entry after a newly created/replaced durable file.
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            try:
                os.fsync(descriptor)
            except OSError as error:
                if error.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
                    raise
        finally:
            os.close(descriptor)

    def _assert_internal_path(self, path: Path, label: str) -> Path:
        """Reject every existing symlink, junction, reparse point, or hard link."""

        candidate = Path(os.path.abspath(os.fspath(path)))
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise MemoryStoreError(f"{label} escapes the configured memory root") from error
        current = self.root
        paths = [self.root]
        for part in relative.parts:
            current = current / part
            paths.append(current)
        for component in paths:
            if not os.path.lexists(component):
                break
            try:
                metadata = os.lstat(component)
            except OSError as error:
                raise MemoryStoreError(f"cannot inspect {label}: {component}") from error
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if stat.S_ISLNK(metadata.st_mode) or attributes & int(
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise MemoryStoreError(f"{label} escapes via a symlink or junction")
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                raise MemoryStoreError(f"{label} contains a hard-linked internal file")
        try:
            resolved = candidate.resolve(strict=os.path.lexists(candidate))
            resolved.relative_to(self.root)
        except (OSError, ValueError) as error:
            raise MemoryStoreError(f"{label} escapes the configured memory root") from error
        return candidate

    def _ensure_retention_directory(self, path: Path, label: str) -> None:
        """Create an internal retention directory without following an escape."""

        path = self._assert_internal_path(path, label)
        nearest = path
        while not os.path.lexists(nearest):
            if nearest == nearest.parent:  # pragma: no cover - root always exists here
                raise MemoryStoreError(f"cannot locate an existing parent for {label}")
            nearest = nearest.parent
        try:
            resolved_parent = nearest.resolve(strict=True)
            resolved_parent.relative_to(self.root)
        except (OSError, ValueError) as error:
            raise MemoryStoreError(f"{label} escapes the configured memory root") from error
        path.mkdir(parents=True, exist_ok=True)
        self._assert_internal_path(path, label)
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as error:
            raise MemoryStoreError(f"{label} escapes the configured memory root") from error

    def _retention_path(
        self,
        path: Path,
        base: Path,
        label: str,
        *,
        must_exist: bool = False,
    ) -> Path:
        """Resolve once and require a blob/archive/tombstone path below its root.

        Operations use the returned resolved path rather than the lexical path,
        preventing an already-present symlink or Windows junction from redirecting
        the mutation. Portable ``Path`` APIs cannot eliminate a hostile concurrent
        reparse-point swap after this check; the store lock covers cooperating
        Memory v1 processes, not an external filesystem attacker.
        """

        self._assert_internal_path(base, f"{label} base")
        path = self._assert_internal_path(path, label)
        try:
            resolved_base = base.resolve(strict=True)
            resolved_base.relative_to(self.root)
            if os.path.lexists(path):
                resolved = path.resolve(strict=True)
            else:
                if must_exist:
                    raise FileNotFoundError(path)
                resolved = path.resolve(strict=False)
            resolved.relative_to(resolved_base)
        except (OSError, ValueError) as error:
            raise MemoryStoreError(
                f"{label} escapes its configured retention root"
            ) from error
        return resolved

    def _recover_incomplete_retention_artifacts_locked(self) -> dict[str, Any]:
        """Remove only pre-event GC artifacts left by a dead process.

        A tombstone or archive is derived state until its deterministic action
        event is present in a canonical JSONL segment. If the event exists but
        SQLite projection is missing, the artifact is retained for replay.
        """

        records_by_id: dict[str, dict[str, Any]] = {}
        for path in sorted(self.events_dir.glob("*/*.jsonl"), key=lambda item: item.as_posix()):
            for _, record in self._read_segment(path):
                records_by_id[str(record["event_id"])] = record

        removed_tombstones: list[str] = []
        referenced_archives: set[str] = set()
        for lexical_path in sorted(self.tombstones_dir.glob("*/*/*.json")):
            safe_path = self._retention_path(
                lexical_path,
                self.tombstones_dir,
                "tombstone recovery read",
                must_exist=True,
            )
            digest = "sha256:" + lexical_path.parent.name
            receipt = self._read_tombstone(digest, lexical_path.stem)
            action = records_by_id.get(str(receipt["action_event_id"]))
            if action is None:
                if receipt.get("archive_locator"):
                    self._unlink_archive(receipt)
                safe_path.unlink()
                self._sync_directory(safe_path.parent)
                removed_tombstones.append(
                    str(safe_path.relative_to(self.root)).replace("\\", "/")
                )
                continue
            locator = receipt.get("archive_locator")
            if isinstance(locator, str) and locator:
                archive_path = self._retention_path(
                    self.root / locator,
                    self.archive_dir,
                    "tombstone archive locator",
                    must_exist=True,
                )
                referenced_archives.add(
                    str(archive_path.relative_to(self.root)).replace("\\", "/")
                )

        removed_archives: list[str] = []
        archive_root = self._retention_path(
            self.archive_dir,
            self.archive_dir,
            "archive recovery root",
            must_exist=True,
        )
        for lexical_path in sorted(archive_root.glob("*/*.json.zst")):
            safe_path = self._retention_path(
                lexical_path,
                self.archive_dir,
                "archive recovery read",
                must_exist=True,
            )
            locator = str(safe_path.relative_to(self.root)).replace("\\", "/")
            if locator in referenced_archives:
                continue
            safe_path.unlink()
            self._sync_directory(safe_path.parent)
            removed_archives.append(locator)
        rehydration = self._recover_rehydration_stages_locked(records_by_id)
        return {
            "status": "recovered_pre_event_retention_artifacts",
            "removed_tombstone_locators": removed_tombstones,
            "removed_archive_locators": removed_archives,
            **rehydration,
        }

    def _recover_rehydration_stages_locked(
        self, records_by_id: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Reconcile only exact rehydration staging files after process death.

        A stage without its canonical ``blob/rehydrated`` event is an orphan and
        is removed. A stage with that event is retained for replay unless the
        exact hot payload already exists, in which case the redundant stage is
        removed. No other file is enumerated or deleted.
        """

        removed_orphans: list[str] = []
        removed_redundant: list[str] = []
        retained_for_replay: list[str] = []
        root = self._retention_path(
            self.rehydration_dir,
            self.rehydration_dir,
            "rehydration recovery root",
            must_exist=True,
        )
        for lexical_path in sorted(root.glob("*.tmp")):
            safe_path = self._retention_path(
                lexical_path,
                self.rehydration_dir,
                "rehydration temporary recovery",
                must_exist=True,
            )
            safe_path.unlink()
            removed_orphans.append(str(safe_path.relative_to(self.root)).replace("\\", "/"))
        for lexical_path in sorted(root.glob("*.blob")):
            safe_path = self._retention_path(
                lexical_path,
                self.rehydration_dir,
                "rehydration stage recovery",
                must_exist=True,
            )
            event_id = safe_path.stem
            record = records_by_id.get(event_id)
            if record is None or record.get("type") != "blob/rehydrated":
                safe_path.unlink()
                removed_orphans.append(
                    str(safe_path.relative_to(self.root)).replace("\\", "/")
                )
                continue
            payload = self._resolve_payload(record)
            expected_locator = str(safe_path.relative_to(self.root)).replace("\\", "/")
            if payload.get("staged_locator") != expected_locator:
                raise ChainValidationError(
                    f"rehydration stage locator differs from canonical event: {event_id}"
                )
            content = safe_path.read_bytes()
            digest = str(payload.get("blob_sha256"))
            size_bytes = payload.get("size_bytes")
            if (
                not isinstance(size_bytes, int)
                or len(content) != size_bytes
                or _sha256(content) != digest
            ):
                raise ChainValidationError(f"rehydration stage integrity failure: {event_id}")
            hot_path = self._retention_path(
                self._blob_path(digest), self.blobs_dir, "rehydrated hot recovery"
            )
            if hot_path.exists():
                hot_content = hot_path.read_bytes()
                if hot_content != content:
                    raise ChainValidationError(
                        f"rehydration stage conflicts with restored hot blob: {digest}"
                    )
                safe_path.unlink()
                removed_redundant.append(expected_locator)
            else:
                retained_for_replay.append(expected_locator)
        if removed_orphans or removed_redundant:
            self._sync_directory(root)
        return {
            "removed_orphan_rehydration_locators": removed_orphans,
            "removed_redundant_rehydration_locators": removed_redundant,
            "retained_rehydration_locators": retained_for_replay,
        }

    def append(
        self,
        *,
        host_id: str,
        session_id: str,
        event_type: str,
        actor: Mapping[str, Any],
        scope: MemoryScope | Mapping[str, Any],
        payload: Mapping[str, Any],
        task_id: str | None = None,
        parent_event_id: str | None = None,
        event_id: str | None = None,
        occurred_at: str | datetime | None = None,
        retention_class: str = "core",
        _lock_already_held: bool = False,
    ) -> dict[str, Any]:
        """Append, fsync, then transactionally project one canonical event.

        Known reducer payloads:

        * ``memory/committed``: memory fields plus a non-empty ``evidence`` list.
        * ``memory/superseded|retired|disputed``: ``memory_id`` and optional
          ``valid_to``.
        * ``state/committed``: ``revision`` and ``state``.

        Other event families remain queryable through ``event_index``.
        """

        if type(_lock_already_held) is not bool:
            raise MemoryStoreError("_lock_already_held must be an exact boolean")
        lock_context = nullcontext() if _lock_already_held else self._exclusive_store_lock()
        with lock_context:
            host_id = self._validate_host_id(host_id)
            session_id = _uuid_text(session_id, "session_id")  # type: ignore[assignment]
            task_id = _uuid_text(task_id, "task_id", nullable=True)
            parent_event_id = _uuid_text(parent_event_id, "parent_event_id", nullable=True)
            event_id = _uuid_text(event_id or str(uuid.uuid4()), "event_id")  # type: ignore[assignment]
            occurred = _utc_text(occurred_at)
            if not isinstance(event_type, str) or "/" not in event_type:
                raise MemoryStoreError("event_type must be a non-empty namespaced string")
            actor_value = self._validate_actor(actor)
            scope_value = MemoryScope.from_value(scope)
            # A durable event always belongs to ``session_id`` above. Its memory
            # scope may deliberately be broader (null task/session), but it may
            # never claim a different task or session.
            if scope_value.session_id not in {None, session_id}:
                raise MemoryStoreError("scope session_id must be null or equal event session_id")
            if scope_value.task_id not in {None, task_id}:
                raise MemoryStoreError("scope task_id must be null or equal event task_id")
            if retention_class not in _RETENTION_CLASSES:
                raise MemoryStoreError(f"invalid retention_class: {retention_class}")
            if not isinstance(payload, Mapping):
                raise MemoryStoreError("payload must be a mapping")
            payload_value = self._normalize_payload(
                event_type, dict(payload), event_id, task_id, occurred
            )

            path = self._segment_path(host_id, session_id)
            existing = self._read_segment(path)
            if existing and not self._record_hash_indexed(existing[-1][1]["record_sha256"]):
                raise ProjectionError(
                    "segment has durable unprojected events; call replay() before appending"
                )
            seq = len(existing) + 1
            previous = existing[-1][1]["record_sha256"] if existing else None
            if self._event_id_exists(event_id):
                raise MemoryStoreError(f"event_id already exists: {event_id}")

            payload_bytes = _canonical_bytes(payload_value)
            payload_sha256 = _sha256(payload_bytes)
            if len(payload_bytes) > self.blob_threshold:
                self._reject_evicted_blob_reuse_locked(payload_sha256)
                self._enforce_bulk_admission_locked(
                    event_type=event_type,
                    retention_class=retention_class,
                    payload_sha256=payload_sha256,
                    payload_size=len(payload_bytes),
                )
            stored_payload: Mapping[str, Any]
            if len(payload_bytes) > self.blob_threshold:
                stored_payload = {
                    "$blob": {
                        "sha256": payload_sha256,
                        "size_bytes": len(payload_bytes),
                        "media_type": "application/json",
                        "retention_class": retention_class,
                    }
                }
            else:
                stored_payload = payload_value

            record: dict[str, Any] = {
                "schema": EVENT_SCHEMA,
                "event_id": event_id,
                "host_id": host_id,
                "session_id": session_id,
                "task_id": task_id,
                "parent_event_id": parent_event_id,
                "seq": seq,
                "occurred_at": occurred,
                "type": event_type,
                "actor": actor_value,
                "scope": asdict(scope_value),
                "payload": stored_payload,
                "payload_sha256": payload_sha256,
                "previous_record_sha256": previous,
            }
            record["record_sha256"] = _sha256(_canonical_bytes(record))
            encoded = _canonical_bytes(record) + b"\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path = self._retention_path(
                path, self.events_dir, "event segment append"
            )
            segment_was_new = not path.exists()
            offset = 0 if segment_was_new else path.stat().st_size
            self._preflight_event(record, path, offset, payload_value)
            if len(payload_bytes) > self.blob_threshold:
                self._write_blob(payload_sha256, payload_bytes)
            # The kernel lock makes this one write non-interleaving across
            # cooperating processes. A successful return is file-synced below;
            # an interrupted/torn line is detected and never projected. This is
            # not a claim that arbitrary filesystems make power-loss writes atomic.
            with path.open("ab", buffering=0) as stream:
                written = stream.write(encoded)
                if written != len(encoded):
                    raise MemoryStoreError("short event-log write")
                stream.flush()
                self._sync_file(stream.fileno())
            if segment_was_new:
                self._sync_directory(path.parent)
                self._sync_directory(path.parent.parent)
                self._sync_directory(self.root)

            # The raw record is already durable. A projection failure is repaired
            # automatically by replay on the next open.
            try:
                with closing(self._connect()) as connection:
                    with connection:
                        self._apply_event(connection, record, path, offset)
            except Exception as error:
                raise ProjectionError(
                    "event is durable but projection failed; reopen the store to replay it"
                ) from error
            return record

    append_event = append

    def verify(self) -> VerificationReport:
        """Verify canonical encoding, every chain, global IDs, and referenced blobs."""

        with self._exclusive_store_lock():
            return self._verify_locked()

    def _verify_locked(self) -> VerificationReport:
        seen_event_ids: set[str] = set()
        seen_record_hashes: set[str] = set()
        last_hashes: dict[str, str] = {}
        event_count = 0
        blobs: set[str] = set()
        records_by_id: dict[str, dict[str, Any]] = {}
        evicted_receipts: dict[str, dict[str, Any]] = {}
        paths = sorted(self.events_dir.glob("*/*.jsonl"), key=lambda item: item.as_posix())
        for path in paths:
            rows = self._read_segment(path)
            for _, record in rows:
                event_id = record["event_id"]
                record_hash = record["record_sha256"]
                if event_id in seen_event_ids:
                    raise ChainValidationError(f"duplicate event_id: {event_id}")
                if record_hash in seen_record_hashes:
                    raise ChainValidationError(f"duplicate record hash: {record_hash}")
                seen_event_ids.add(event_id)
                seen_record_hashes.add(record_hash)
                records_by_id[event_id] = record
                blob = self._blob_reference(record["payload"])
                if blob is not None:
                    try:
                        self._read_blob(blob["sha256"], blob["size_bytes"])
                    except BlobEvictedError:
                        evicted_receipts[blob["sha256"]] = self._read_tombstone(
                            blob["sha256"]
                        )
                    blobs.add(blob["sha256"])
                event_count += 1
            if rows:
                last_hashes[f"{path.parent.name}/{path.stem}"] = rows[-1][1][
                    "record_sha256"
                ]
        receipt_chains: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(self.tombstones_dir.glob("*/*/*.json")):
            digest = "sha256:" + path.parent.name
            receipt_chains.setdefault(digest, []).append(
                self._read_tombstone(digest, path.stem)
            )
        for digest, receipts in sorted(receipt_chains.items()):
            roots = [item for item in receipts if item["previous_tombstone_sha256"] is None]
            if len(roots) != 1:
                raise ChainValidationError(f"blob tombstone history is broken: {digest}")
            ordered: list[dict[str, Any]] = []
            current = roots[0]
            while True:
                ordered.append(current)
                children = [
                    item
                    for item in receipts
                    if item["previous_tombstone_sha256"] == current["tombstone_sha256"]
                ]
                if not children:
                    break
                if len(children) != 1 or children[0] in ordered:
                    raise ChainValidationError(f"blob tombstone history is broken: {digest}")
                current = children[0]
            if len(ordered) != len(receipts):
                raise ChainValidationError(f"blob tombstone history is broken: {digest}")
            for receipt in ordered:
                action = records_by_id.get(receipt["action_event_id"])
                expected_type = (
                    "blob/archived"
                    if receipt["availability"] == "archived"
                    else "blob/evicted"
                )
                if action is None or action["type"] != expected_type:
                    raise ChainValidationError(
                        f"blob tombstone lacks its canonical action event: {digest}"
                    )
                action_payload = self._resolve_payload(action)
                if (
                    action_payload.get("blob_sha256") != digest
                    or action_payload.get("tombstone_sha256")
                    != receipt["tombstone_sha256"]
                    or sorted(action_payload.get("source_event_ids", []))
                    != sorted(receipt["source_event_ids"])
                ):
                    raise ChainValidationError(
                        f"blob tombstone canonical event mismatch: {digest}"
                    )
        for record in sorted(
            (
                item
                for item in records_by_id.values()
                if item["type"] == "blob/rehydrated"
            ),
            key=lambda item: (item["occurred_at"], item["event_id"]),
        ):
            payload = self._resolve_payload(record)
            digest = str(payload["blob_sha256"])
            receipt = self._read_tombstone_by_sha256(
                digest, str(payload["expected_tombstone_sha256"])
            )
            if (
                receipt["availability"] != "evicted"
                or payload["size_bytes"] != receipt["size_bytes"]
                or payload["retention_class"] != receipt["retention_class"]
                or payload["source_event_ids"] != receipt["source_event_ids"]
                or payload["scope_keys"] != receipt["scope_keys"]
            ):
                raise ChainValidationError(
                    f"blob rehydration does not match eviction history: {digest}"
                )
            if not self._has_later_blob_lifecycle_event(
                digest,
                str(record["occurred_at"]),
                str(record["event_id"]),
                expected_tombstone_sha256=str(payload["expected_tombstone_sha256"]),
            ):
                hot = self._retention_path(
                    self._blob_path(digest), self.blobs_dir, "rehydration verification"
                )
                try:
                    content = hot.read_bytes()
                except FileNotFoundError as error:
                    stage = self._retention_path(
                        self._rehydration_stage_path(str(record["event_id"])),
                        self.rehydration_dir,
                        "rehydration verification stage",
                    )
                    try:
                        content = stage.read_bytes()
                    except FileNotFoundError:
                        raise ChainValidationError(
                            f"latest rehydration has neither hot nor staged payload: {digest}"
                        ) from error
                if len(content) != payload["size_bytes"] or _sha256(content) != digest:
                    raise ChainValidationError(
                        f"latest rehydration hot payload mismatch: {digest}"
                    )
        for digest, receipt in sorted(evicted_receipts.items()):
            action = records_by_id.get(receipt["action_event_id"])
            if action is None or action["type"] != "blob/evicted":
                raise ChainValidationError(
                    f"evicted blob tombstone lacks its canonical action event: {digest}"
                )
            payload = self._resolve_payload(action)
            if (
                payload.get("blob_sha256") != digest
                or payload.get("tombstone_sha256") != receipt["tombstone_sha256"]
                or sorted(payload.get("source_event_ids", []))
                != sorted(receipt["source_event_ids"])
            ):
                raise ChainValidationError(
                    f"evicted blob tombstone provenance mismatch: {digest}"
                )
        return VerificationReport(len(paths), event_count, len(blobs), last_hashes)

    def replay(self, *, rebuild: bool = False) -> ReplayReport:
        """Verify raw truth and apply missing events, or rebuild every projection row."""

        if type(rebuild) is not bool:
            raise MemoryStoreError("rebuild must be an exact boolean")
        with self._exclusive_store_lock():
            report = self._verify_locked()
            events: list[tuple[Path, int, dict[str, Any]]] = []
            for path in sorted(self.events_dir.glob("*/*.jsonl"), key=lambda item: item.as_posix()):
                for offset, record in self._read_segment(path):
                    events.append((path, offset, record))

            applied = 0
            with closing(self._connect()) as connection:
                if rebuild:
                    with connection:
                        self._clear_projection(connection)
                indexed = {
                    row[0]: row[1]
                    for row in connection.execute(
                        "SELECT event_id, record_sha256 FROM event_index"
                    ).fetchall()
                }
                raw_ids = {record["event_id"] for _, _, record in events}
                extra = set(indexed) - raw_ids
                if extra:
                    raise ProjectionError(
                        "projection contains events absent from canonical logs; "
                        "use replay(rebuild=True)"
                    )
                missing: list[tuple[Path, int, dict[str, Any]]] = []
                for path, offset, record in events:
                    known = indexed.get(record["event_id"])
                    if known is not None:
                        if known != record["record_sha256"]:
                            raise ProjectionError(
                                f"projection hash differs for event {record['event_id']}"
                            )
                        continue
                    missing.append((path, offset, record))
                if missing:
                    with connection:
                        # Index the complete durable range first so evidence may
                        # cite an event imported from another host/session.
                        for path, offset, record in missing:
                            self._index_event(connection, record, path, offset)
                        for path, offset, record in self._projection_order(missing):
                            self._apply_projection(connection, record)
                    applied = len(missing)
            return ReplayReport(report.event_count, applied, rebuild)

    def _enforce_bulk_admission_locked(
        self,
        *,
        event_type: str,
        retention_class: str,
        payload_sha256: str,
        payload_size: int,
    ) -> None:
        if retention_class != "bulk" or event_type in {
            "memory/committed",
            "state/committed",
            "verification/result",
            "artifact/observed",
            "production/claim",
        }:
            return
        with closing(self._connect()) as connection:
            hot_bytes = int(
                connection.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) FROM blob_catalog "
                    "WHERE availability = 'hot'"
                ).fetchone()[0]
            )
            existing = connection.execute(
                "SELECT availability FROM blob_catalog WHERE blob_sha256 = ?",
                (payload_sha256,),
            ).fetchone()
        additional = (
            0 if existing is not None and existing["availability"] == "hot" else payload_size
        )
        projected = hot_bytes + additional
        if projected * 10_000 >= self.hot_blob_budget_bytes * BULK_REFUSAL_BASIS_POINTS:
            raise RetentionPressureError(
                "unpinned bulk admission refused before durable write at or above "
                f"{BULK_REFUSAL_BASIS_POINTS / 100:.0f}% hot-blob pressure "
                f"({projected}/{self.hot_blob_budget_bytes} bytes)"
            )

    def _reject_evicted_blob_reuse_locked(self, payload_sha256: str) -> None:
        """Reject silent resurrection until a canonical rehydration event exists."""

        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT availability FROM blob_catalog WHERE blob_sha256 = ?",
                (payload_sha256,),
            ).fetchone()
        if existing is not None and existing["availability"] == "evicted":
            raise BlobEvictedError(
                "payload digest was previously evicted; canonical blob rehydration "
                "is required, so admission was refused before any write"
            )

    def rehydrate_blob(
        self,
        content: bytes,
        *,
        expected_blob_sha256: str,
        expected_size_bytes: int,
        expected_tombstone_sha256: str,
        expected_scopes: Sequence[MemoryScope | Mapping[str, Any]],
        expected_source_event_ids: Sequence[str],
        provenance: Mapping[str, Any],
        occurred_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        """Restore one evicted payload through an exact canonical lifecycle event.

        The caller must name every identity boundary observed before obtaining
        the bytes. The method stages and fsyncs the exact content, appends the
        canonical ``blob/rehydrated`` event, then atomically promotes the stage
        before the SQLite projection may mark the blob hot. Tombstone history is
        preserved and remains independently verifiable.
        """

        if not isinstance(content, bytes):
            raise MemoryStoreError("rehydration content must be exact bytes")
        if not isinstance(expected_size_bytes, int) or isinstance(
            expected_size_bytes, bool
        ) or expected_size_bytes < 0:
            raise MemoryStoreError("expected_size_bytes must be a non-negative integer")
        if not isinstance(expected_blob_sha256, str) or not _SHA256_RE.fullmatch(
            expected_blob_sha256
        ):
            raise MemoryStoreError("expected_blob_sha256 must be sha256:<hex>")
        if not isinstance(expected_tombstone_sha256, str) or not _SHA256_RE.fullmatch(
            expected_tombstone_sha256
        ):
            raise MemoryStoreError("expected_tombstone_sha256 must be sha256:<hex>")
        if len(content) != expected_size_bytes or _sha256(content) != expected_blob_sha256:
            raise ChainValidationError("rehydration bytes do not match expected digest and size")
        if not expected_scopes:
            raise MemoryStoreError("rehydration requires the exact non-empty scope set")
        scope_values = tuple(MemoryScope.from_value(item) for item in expected_scopes)
        scope_keys = tuple(sorted({item.key for item in scope_values}))
        if len(scope_keys) != len(expected_scopes):
            raise MemoryStoreError("rehydration expected scopes must be unique")
        if not expected_source_event_ids:
            raise MemoryStoreError("rehydration requires exact source event provenance")
        source_event_ids = tuple(
            sorted(
                {
                    _uuid_text(item, "source_event_id")  # type: ignore[arg-type]
                    for item in expected_source_event_ids
                }
            )
        )
        if len(source_event_ids) != len(expected_source_event_ids):
            raise MemoryStoreError("rehydration source event ids must be unique")
        provenance_value = self._normalize_rehydration_provenance(
            provenance, expected_blob_sha256
        )
        observed_at = _utc_text(occurred_at)

        with self._exclusive_store_lock():
            with closing(self._connect()) as connection:
                catalog = connection.execute(
                    "SELECT * FROM blob_catalog WHERE blob_sha256 = ?",
                    (expected_blob_sha256,),
                ).fetchone()
                references = connection.execute(
                    "SELECT event_id, task_id, scope_key FROM blob_reference "
                    "WHERE blob_sha256 = ? ORDER BY event_id",
                    (expected_blob_sha256,),
                ).fetchall()
            if catalog is None:
                raise ProjectionError("rehydration target is absent from the blob catalog")
            if catalog["availability"] != "evicted":
                raise ProjectionError("rehydration target is not currently evicted")
            if catalog["size_bytes"] != expected_size_bytes:
                raise ProjectionError("rehydration byte count differs from the blob catalog")
            receipt = self._read_tombstone(expected_blob_sha256)
            if receipt["availability"] != "evicted":
                raise ChainValidationError("latest tombstone is not an eviction")
            if receipt["tombstone_sha256"] != expected_tombstone_sha256:
                raise ProjectionError("rehydration tombstone identity is stale or mismatched")
            catalog_source_ids = tuple(sorted(str(item["event_id"]) for item in references))
            catalog_scope_keys = tuple(sorted({str(item["scope_key"]) for item in references}))
            if source_event_ids != catalog_source_ids or list(source_event_ids) != receipt[
                "source_event_ids"
            ]:
                raise ProjectionError("rehydration source-event provenance does not match exactly")
            if scope_keys != catalog_scope_keys or list(scope_keys) != receipt["scope_keys"]:
                raise ProjectionError("rehydration scope set does not match exactly")
            if catalog["retention_class"] != receipt["retention_class"]:
                raise ChainValidationError("rehydration retention class history is inconsistent")

            session_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"memory-rehydration:{expected_blob_sha256}:{expected_tombstone_sha256}",
                )
            )
            event_id = str(uuid.uuid5(uuid.UUID(session_id), "blob/rehydrated"))
            staged_locator = self._write_rehydration_stage(
                event_id, expected_blob_sha256, content
            )
            task_ids = {
                str(item["task_id"]) for item in references if item["task_id"] is not None
            }
            task_id = next(iter(task_ids)) if len(task_ids) == 1 else None
            first_scope = scope_values[0]
            event_scope = MemoryScope(
                owner_id=(
                    first_scope.owner_id
                    if all(item.owner_id == first_scope.owner_id for item in scope_values)
                    else "memory-retention"
                ),
                workspace_id=(
                    first_scope.workspace_id
                    if all(item.workspace_id == first_scope.workspace_id for item in scope_values)
                    else None
                ),
                task_id=(
                    task_id
                    if all(item.task_id == task_id for item in scope_values)
                    else None
                ),
                agent_role=(
                    first_scope.agent_role
                    if all(item.agent_role == first_scope.agent_role for item in scope_values)
                    else None
                ),
                session_id=None,
            )
            try:
                return self.append(
                    host_id="memory-rehydration",
                    session_id=session_id,
                    task_id=task_id,
                    event_id=event_id,
                    event_type="blob/rehydrated",
                    actor={"kind": "system", "id": "memory-v1-rehydration"},
                    scope=event_scope,
                    occurred_at=observed_at,
                    retention_class="core",
                    payload={
                        "schema": BLOB_REHYDRATION_SCHEMA,
                        "blob_sha256": expected_blob_sha256,
                        "size_bytes": expected_size_bytes,
                        "retention_class": str(catalog["retention_class"]),
                        "expected_tombstone_sha256": expected_tombstone_sha256,
                        "source_event_ids": list(source_event_ids),
                        "scope_keys": list(scope_keys),
                        "provenance": provenance_value,
                        "staged_locator": staged_locator,
                    },
                    _lock_already_held=True,
                )
            except Exception:
                segment = self._segment_path("memory-rehydration", session_id)
                durable = any(
                    record["event_id"] == event_id for _, record in self._read_segment(segment)
                )
                if not durable:
                    self._unlink_rehydration_stage(event_id)
                raise

    @staticmethod
    def _normalize_rehydration_provenance(
        provenance: Mapping[str, Any], expected_digest: str
    ) -> dict[str, str]:
        required = {"source_kind", "source_locator", "source_sha256", "authority"}
        if not isinstance(provenance, Mapping) or set(provenance) != required:
            raise MemoryStoreError("rehydration provenance fields do not match v1")
        value = {key: provenance[key] for key in sorted(required)}
        for field in required:
            if not isinstance(value[field], str) or not value[field]:
                raise MemoryStoreError(f"rehydration provenance {field} must be non-empty text")
            if len(value[field]) > 2_048:
                raise MemoryStoreError(f"rehydration provenance {field} is too long")
        for field in ("source_locator", "authority"):
            text = value[field]
            if not text.strip():
                raise MemoryStoreError(
                    f"rehydration provenance {field} must be semantically nonblank"
                )
            if unicodedata.normalize("NFC", text) != text:
                raise MemoryStoreError(
                    f"rehydration provenance {field} must use canonical Unicode"
                )
            if any(unicodedata.category(character) == "Cc" for character in text):
                raise MemoryStoreError(
                    f"rehydration provenance {field} cannot contain control characters"
                )
        if value["source_kind"] not in {
            "operator_supplied",
            "local_backup",
            "peer_replica",
        }:
            raise MemoryStoreError("rehydration provenance source_kind is invalid")
        if value["source_sha256"] != expected_digest:
            raise ChainValidationError("rehydration provenance digest does not match content")
        return value

    def plan_retention(
        self,
        *,
        now: str | datetime | None = None,
        scopes: Sequence[MemoryScope | Mapping[str, Any]] | None = None,
    ) -> RetentionPlan:
        """Return a deterministic, non-mutating GC dry run."""

        observed_at = _utc_text(now)
        exact_scope_keys = tuple(
            sorted({MemoryScope.from_value(scope).key for scope in scopes or ()})
        )
        with self._exclusive_store_lock():
            return self._plan_retention_locked(observed_at, exact_scope_keys)

    def _plan_retention_locked(
        self, observed_at: str, exact_scope_keys: tuple[str, ...]
    ) -> RetentionPlan:
        with closing(self._connect()) as connection:
            catalog = connection.execute(
                "SELECT * FROM blob_catalog ORDER BY blob_sha256"
            ).fetchall()
            hot_bytes = sum(
                int(row["size_bytes"]) for row in catalog if row["availability"] == "hot"
            )
            cited_event_ids = tuple(
                sorted(
                    {
                        str(row[0])
                        for row in connection.execute(
                            "SELECT source_event_id FROM blob_citation "
                            "WHERE citation_kind = 'ordinary'"
                        ).fetchall()
                    }
                )
            )
            decisions: list[RetentionDecision] = []
            for row in catalog:
                references = connection.execute(
                    "SELECT * FROM blob_reference WHERE blob_sha256 = ? ORDER BY event_id",
                    (row["blob_sha256"],),
                ).fetchall()
                scope_keys = tuple(sorted({str(item["scope_key"]) for item in references}))
                if exact_scope_keys and not set(scope_keys) & set(exact_scope_keys):
                    continue
                pin_reasons = self._blob_pin_reasons(
                    connection,
                    row,
                    references,
                    exact_scope_keys=exact_scope_keys,
                )
                cited = any(str(item["event_id"]) in cited_event_ids for item in references)
                action, reason = self._retention_action(
                    row,
                    observed_at=observed_at,
                    pin_reasons=pin_reasons,
                    cited=cited,
                )
                decisions.append(
                    RetentionDecision(
                        blob_sha256=str(row["blob_sha256"]),
                        retention_class=str(row["retention_class"]),
                        availability_before=str(row["availability"]),
                        action=action,
                        reason=reason,
                        size_bytes=int(row["size_bytes"]),
                        hot_locator=(
                            None if row["hot_locator"] is None else str(row["hot_locator"])
                        ),
                        created_at=str(row["created_at"]),
                        task_terminal_at=(
                            None
                            if row["task_terminal_at"] is None
                            else str(row["task_terminal_at"])
                        ),
                        expires_at=(
                            None if row["expires_at"] is None else str(row["expires_at"])
                        ),
                        source_event_ids=tuple(
                            sorted(str(item["event_id"]) for item in references)
                        ),
                        scope_keys=scope_keys,
                        pin_reasons=pin_reasons,
                        cited=cited,
                    )
                )
        decisions.sort(
            key=lambda item: (
                {"scratch": 0, "bulk": 1, "evidence": 2, "core": 3}[
                    item.retention_class
                ],
                item.expires_at or "9999-12-31T23:59:59Z",
                item.blob_sha256,
            )
        )
        pressure = (hot_bytes * 10_000) // self.hot_blob_budget_bytes
        return RetentionPlan.create(
            observed_at=observed_at,
            hot_budget_bytes=self.hot_blob_budget_bytes,
            hot_bytes=hot_bytes,
            pressure_basis_points=pressure,
            exact_scope_keys=exact_scope_keys,
            cited_event_ids=cited_event_ids,
            decisions=decisions,
        )

    def _blob_pin_reasons(
        self,
        connection: sqlite3.Connection,
        catalog: sqlite3.Row,
        references: Sequence[sqlite3.Row],
        *,
        exact_scope_keys: tuple[str, ...],
    ) -> tuple[str, ...]:
        digest = str(catalog["blob_sha256"])
        reasons: set[str] = set()
        if catalog["retention_class"] == "core":
            reasons.add("retention_class:core")
        reference_ids = tuple(str(item["event_id"]) for item in references)
        reference_scopes = {str(item["scope_key"]) for item in references}
        if exact_scope_keys and not reference_scopes.issubset(set(exact_scope_keys)):
            reasons.add("scope:outside_requested_exact_scope")
        for reference in references:
            task_id = reference["task_id"]
            if task_id is not None:
                terminal = connection.execute(
                    "SELECT 1 FROM task_terminal WHERE task_id = ?", (task_id,)
                ).fetchone()
                if terminal is None:
                    reasons.add(f"active_task:{task_id}")
            if reference["event_type"] in {
                "verification/result",
                "artifact/observed",
                "production/claim",
            }:
                reasons.add(f"authoritative_event:{reference['event_type']}")
        if reference_ids:
            placeholders = ",".join("?" for _ in reference_ids)
            memories = connection.execute(
                f"SELECT memory_id, status FROM memory_item WHERE source_event_id IN "
                f"({placeholders}) AND status IN ('active','disputed')",
                reference_ids,
            ).fetchall()
            reasons.update(f"memory:{row['memory_id']}:{row['status']}" for row in memories)
            evidence = connection.execute(
                f"SELECT evidence_id FROM evidence WHERE source_event_id IN ({placeholders}) "
                "OR source_sha256 = ?",
                (*reference_ids, digest),
            ).fetchall()
            reasons.update(f"evidence:{row['evidence_id']}" for row in evidence)
            states = connection.execute(
                f"SELECT task_id FROM task_state WHERE through_event_id IN ({placeholders})",
                reference_ids,
            ).fetchall()
            for row in states:
                terminal = connection.execute(
                    "SELECT 1 FROM task_terminal WHERE task_id = ?", (row["task_id"],)
                ).fetchone()
                if terminal is None:
                    reasons.add(f"active_task_state:{row['task_id']}")
            production = connection.execute(
                f"SELECT DISTINCT source_event_id FROM blob_citation "
                f"WHERE source_event_id IN ({placeholders}) AND citation_kind = 'production'",
                reference_ids,
            ).fetchall()
            reasons.update(f"production_claim:{row[0]}" for row in production)
        referenced_memories = connection.execute(
            "SELECT memory_id, status, object_json FROM memory_item "
            "WHERE status IN ('active','disputed')"
        ).fetchall()
        for memory in referenced_memories:
            if digest in str(memory["object_json"]):
                reasons.add(
                    f"memory_blob_reference:{memory['memory_id']}:{memory['status']}"
                )
        active_states = connection.execute(
            """
            SELECT s.task_id, s.state_json
            FROM task_state AS s
            LEFT JOIN task_terminal AS t ON t.task_id = s.task_id
            WHERE t.task_id IS NULL
            """
        ).fetchall()
        for state in active_states:
            state_json = str(state["state_json"])
            if digest in state_json or any(event_id in state_json for event_id in reference_ids):
                reasons.add(f"active_task_state_reference:{state['task_id']}")
        return tuple(sorted(reasons))

    @staticmethod
    def _retention_action(
        row: sqlite3.Row,
        *,
        observed_at: str,
        pin_reasons: tuple[str, ...],
        cited: bool,
    ) -> tuple[str, str]:
        availability = str(row["availability"])
        if availability == "evicted":
            return "keep", "already_evicted"
        if pin_reasons:
            return "keep", "protected_reference"
        if row["task_terminal_at"] is None or row["expires_at"] is None:
            return "keep", "task_not_terminal_or_no_ttl"
        expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed < expires:
            return (
                ("keep", "archive_ttl_not_expired")
                if availability == "archived"
                else ("keep", "hot_ttl_not_expired")
            )
        retention_class = str(row["retention_class"])
        if availability == "archived":
            if retention_class == "bulk":
                return "evict", "cited_bulk_archive_expired_after_90d"
            return "keep", "evidence_archive_retained"
        if retention_class == "scratch":
            return "evict", "terminal_unreferenced_scratch_after_24h"
        if retention_class == "bulk":
            return (
                ("archive", "cited_bulk_after_14d")
                if cited
                else ("evict", "uncited_bulk_after_14d")
            )
        if retention_class == "evidence":
            return "archive", "unprotected_evidence_after_180d"
        return "keep", "core_never_gc"

    def run_gc(
        self,
        plan: RetentionPlan | Mapping[str, Any],
        *,
        apply: bool = False,
        max_actions: int | None = None,
        max_bytes: int | None = None,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """Validate a dry-run plan; apply it only when explicitly requested."""

        if type(apply) is not bool:
            raise MemoryStoreError("apply must be an exact boolean")
        try:
            supplied = RetentionPlan.from_value(plan)
        except RetentionContractError as error:
            raise MemoryStoreError(f"invalid retention plan: {error}") from error
        if max_actions is not None and (
            not isinstance(max_actions, int)
            or isinstance(max_actions, bool)
            or max_actions < 1
        ):
            raise MemoryStoreError("max_actions must be a positive integer or null")
        if max_bytes is not None and (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes < 1
        ):
            raise MemoryStoreError("max_bytes must be a positive integer or null")
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
        ):
            raise MemoryStoreError("deadline_monotonic must be finite or null")
        if not callable(monotonic):
            raise MemoryStoreError("monotonic must be callable")
        if not apply:
            return {
                "schema": RETENTION_RESULT_SCHEMA,
                "status": "dry_run",
                "plan_sha256": supplied.plan_sha256,
                "actions_planned": len(supplied.actionable),
                "actions_applied": 0,
                "bytes_released": 0,
                "events": [],
            }
        with self._exclusive_store_lock():
            current = self._plan_retention_locked(
                supplied.observed_at, supplied.exact_scope_keys
            )
            if current.as_dict() != supplied.as_dict():
                raise ProjectionError(
                    "retention plan is stale; obtain and inspect a new dry run before apply"
                )
            events: list[dict[str, Any]] = []
            bytes_released = 0
            stop_reason = "all_eligible_actions_selected"
            for decision in supplied.actionable:
                if max_actions is not None and len(events) >= max_actions:
                    stop_reason = "max_actions"
                    break
                if max_bytes is not None and bytes_released + decision.size_bytes > max_bytes:
                    stop_reason = "max_bytes"
                    break
                if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
                    stop_reason = "wall_deadline"
                    break
                record = self._apply_retention_decision_locked(supplied, decision)
                events.append(
                    {
                        "event_id": record["event_id"],
                        "type": record["type"],
                        "blob_sha256": decision.blob_sha256,
                    }
                )
                bytes_released += decision.size_bytes
            self._reconcile_retention_files_locked()
            return {
                "schema": RETENTION_RESULT_SCHEMA,
                "status": "applied",
                "plan_sha256": supplied.plan_sha256,
                "actions_planned": len(events),
                "actions_eligible": len(supplied.actionable),
                "actions_applied": len(events),
                "bytes_released": bytes_released,
                "stop_reason": stop_reason,
                "events": events,
            }

    def _apply_retention_decision_locked(
        self, plan: RetentionPlan, decision: RetentionDecision
    ) -> dict[str, Any]:
        content = self._read_blob(decision.blob_sha256, decision.size_bytes)
        tombstone_directory = self._retention_path(
            self._tombstone_directory(decision.blob_sha256),
            self.tombstones_dir,
            "retention predecessor history",
        )
        previous_tombstone = (
            self._read_tombstone(decision.blob_sha256)
            if tombstone_directory.exists()
            and any(tombstone_directory.glob("*.json"))
            else None
        )
        event_provenance = self._blob_event_provenance(decision.blob_sha256)
        session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memory-retention:{plan.plan_sha256}"))
        event_id = str(
            uuid.uuid5(
                uuid.UUID(session_id), f"{decision.action}:{decision.blob_sha256}"
            )
        )
        archive_locator: str | None = None
        archive_sha256: str | None = None
        if decision.action == "archive":
            archive_locator, archive_sha256 = self._write_archive(
                decision.blob_sha256, content, plan.observed_at
            )
        excerpt_text = content.decode("utf-8", errors="replace")
        excerpt = {
            "head": excerpt_text[:EXCERPT_CHARACTERS],
            "tail": excerpt_text[-EXCERPT_CHARACTERS:],
            "truncated": len(excerpt_text) > EXCERPT_CHARACTERS * 2,
            "source_size_bytes": decision.size_bytes,
        }
        tombstone = self._write_tombstone(
            {
                "schema": RETENTION_TOMBSTONE_SCHEMA,
                "blob_sha256": decision.blob_sha256,
                "size_bytes": decision.size_bytes,
                "retention_class": decision.retention_class,
                "availability": "archived" if decision.action == "archive" else "evicted",
                "hot_locator_before": decision.hot_locator,
                "archive_locator": archive_locator,
                "archive_sha256": archive_sha256,
                "excerpt": excerpt,
                "event_provenance": event_provenance,
                "source_event_ids": list(decision.source_event_ids),
                "scope_keys": list(decision.scope_keys),
                "pin_reasons": list(decision.pin_reasons),
                "plan_sha256": plan.plan_sha256,
                "action_event_id": event_id,
                "decided_at": plan.observed_at,
                "previous_tombstone_sha256": (
                    None
                    if previous_tombstone is None
                    else previous_tombstone["tombstone_sha256"]
                ),
            }
        )
        task_ids = {item["task_id"] for item in event_provenance if item["task_id"] is not None}
        task_id = next(iter(task_ids)) if len(task_ids) == 1 else None
        scopes = [json.loads(item) for item in decision.scope_keys]
        if scopes and all(item == scopes[0] for item in scopes):
            source_scope = scopes[0]
            scope = MemoryScope(
                owner_id=source_scope[0],
                workspace_id=source_scope[1],
                task_id=task_id if source_scope[2] == task_id else None,
                agent_role=source_scope[3],
                session_id=None,
            )
        else:
            scope = MemoryScope("memory-retention")
        record = self.append(
            host_id="memory-retention",
            session_id=session_id,
            task_id=task_id,
            event_id=event_id,
            event_type=("blob/archived" if decision.action == "archive" else "blob/evicted"),
            actor={"kind": "system", "id": "memory-v1-retention"},
            scope=scope,
            occurred_at=plan.observed_at,
            retention_class="core",
            payload={
                "blob_sha256": decision.blob_sha256,
                "size_bytes": decision.size_bytes,
                "retention_class": decision.retention_class,
                "availability": (
                    "archived" if decision.action == "archive" else "evicted"
                ),
                "plan_sha256": plan.plan_sha256,
                "tombstone_sha256": tombstone["tombstone_sha256"],
                "source_event_ids": list(decision.source_event_ids),
                "archive_locator": archive_locator,
                "archive_sha256": archive_sha256,
            },
            _lock_already_held=True,
        )
        if decision.availability_before == "archived":
            if previous_tombstone is None:  # pragma: no cover - guarded above
                raise ChainValidationError("archived decision lacks prior tombstone")
            self._unlink_archive(previous_tombstone)
        else:
            self._unlink_hot_blob(decision.blob_sha256)
        return record

    def _blob_event_provenance(self, digest: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT r.event_id, r.event_type, r.task_id, r.scope_key,
                       r.record_sha256, r.payload_sha256, i.segment_path, i.byte_offset
                FROM blob_reference AS r
                JOIN event_index AS i ON i.event_id = r.event_id
                WHERE r.blob_sha256 = ? ORDER BY r.event_id
                """,
                (digest,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _unlink_hot_blob(self, digest: str) -> None:
        path = self._retention_path(
            self._blob_path(digest), self.blobs_dir, "blob unlink"
        )
        path.unlink(missing_ok=True)
        self._sync_directory(path.parent)

    def _unlink_archive(self, receipt: Mapping[str, Any]) -> None:
        locator = receipt.get("archive_locator")
        if not isinstance(locator, str) or not locator:
            return
        path = self._retention_path(
            self.root / locator, self.archive_dir, "archive unlink"
        )
        path.unlink(missing_ok=True)
        self._sync_directory(path.parent)

    def reconcile_retention(self) -> dict[str, Any]:
        """Finish only exact post-event hot-file removals left by an interrupted apply."""

        with self._exclusive_store_lock():
            return self._reconcile_retention_files_locked()

    def _reconcile_retention_files_locked(self) -> dict[str, Any]:
        removed: list[str] = []
        removed_archives: list[str] = []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT blob_sha256 FROM blob_catalog "
                "WHERE availability IN ('archived','evicted') ORDER BY blob_sha256"
            ).fetchall()
        for row in rows:
            digest = str(row["blob_sha256"])
            receipt = self._read_tombstone(digest)
            with closing(self._connect()) as connection:
                action = connection.execute(
                    "SELECT event_type FROM event_index WHERE event_id = ?",
                    (receipt["action_event_id"],),
                ).fetchone()
            expected = (
                "blob/archived" if receipt["availability"] == "archived" else "blob/evicted"
            )
            if action is None or action["event_type"] != expected:
                raise ChainValidationError(
                    f"retention receipt lacks canonical action event: {digest}"
                )
            hot_path = self._retention_path(
                self._blob_path(digest), self.blobs_dir, "blob reconciliation read"
            )
            if hot_path.exists():
                self._unlink_hot_blob(digest)
                removed.append(digest)
            if receipt["availability"] == "evicted":
                tombstone_directory = self._retention_path(
                    self._tombstone_directory(digest),
                    self.tombstones_dir,
                    "tombstone reconciliation directory",
                )
                for path in sorted(tombstone_directory.glob("*.json")):
                    prior = self._read_tombstone(digest, path.stem)
                    locator = prior.get("archive_locator")
                    if isinstance(locator, str) and locator:
                        archive_path = self._retention_path(
                            self.root / locator,
                            self.archive_dir,
                            "archive reconciliation read",
                        )
                        if archive_path.exists():
                            self._unlink_archive(prior)
                            removed_archives.append(locator)
        return {
            "status": "reconciled",
            "removed_hot_blob_sha256": removed,
            "removed_archive_locators": removed_archives,
        }

    def search(
        self,
        query: str,
        scopes: Sequence[MemoryScope | Mapping[str, Any]],
        *,
        as_of: str | datetime | None = None,
        kinds: Iterable[str] | None = None,
        top_k: int = 8,
    ) -> list[dict[str, Any]]:
        """Search exact scopes through independent exact, word, and trigram channels."""

        if not isinstance(query, str) or not query.strip():
            raise MemoryStoreError("query must be non-empty text")
        if not scopes:
            raise MemoryStoreError("at least one exact scope is required")
        if not 1 <= top_k <= 100:
            raise MemoryStoreError("top_k must be between 1 and 100")
        scope_values = [MemoryScope.from_value(scope) for scope in scopes]
        scope_keys = sorted({scope.key for scope in scope_values})
        kind_values = sorted(set(kinds or []))
        unknown_kinds = set(kind_values) - _MEMORY_KINDS
        if unknown_kinds:
            raise MemoryStoreError(f"invalid memory kinds: {sorted(unknown_kinds)}")
        as_of_value = _utc_text(as_of) if as_of is not None else None
        candidate_limit = max(top_k * 4, 20)

        with closing(self._connect()) as connection:
            common_sql, parameters = self._search_filter_sql(
                scope_keys, kind_values, as_of_value
            )
            channels: dict[str, list[str]] = {}

            exact_sql = (
                "SELECT m.memory_id FROM memory_item m WHERE "
                f"{common_sql} AND (m.memory_id = ? OR m.subject = ? COLLATE NOCASE "
                "OR m.predicate = ? COLLATE NOCASE OR m.searchable_text = ? COLLATE NOCASE) "
                "ORDER BY m.recorded_at DESC, m.memory_id ASC LIMIT ?"
            )
            channels["exact"] = [
                row[0]
                for row in connection.execute(
                    exact_sql, [*parameters, query, query, query, query, candidate_limit]
                ).fetchall()
            ]

            word_query = self._word_query(query)
            if word_query:
                word_sql = (
                    "SELECT m.memory_id FROM memory_fts_unicode "
                    "JOIN memory_item m ON m.rowid = memory_fts_unicode.rowid WHERE "
                    "memory_fts_unicode MATCH ? AND "
                    f"{common_sql} ORDER BY bm25(memory_fts_unicode), "
                    "m.recorded_at DESC, m.memory_id ASC LIMIT ?"
                )
                channels["word"] = [
                    row[0]
                    for row in connection.execute(
                        word_sql, [word_query, *parameters, candidate_limit]
                    ).fetchall()
                ]

            if len(query) >= 3:
                trigram_query = f'"{query.replace(chr(34), chr(34) * 2)}"'
                trigram_sql = (
                    "SELECT m.memory_id FROM memory_fts_trigram "
                    "JOIN memory_item m ON m.rowid = memory_fts_trigram.rowid WHERE "
                    "memory_fts_trigram MATCH ? AND "
                    f"{common_sql} ORDER BY bm25(memory_fts_trigram), "
                    "m.recorded_at DESC, m.memory_id ASC LIMIT ?"
                )
                channels["trigram"] = [
                    row[0]
                    for row in connection.execute(
                        trigram_sql, [trigram_query, *parameters, candidate_limit]
                    ).fetchall()
                ]

            ranks: dict[str, dict[str, int]] = {}
            scores: dict[str, Fraction] = {}
            for channel in ("exact", "word", "trigram"):
                for rank, memory_id in enumerate(channels.get(channel, []), start=1):
                    ranks.setdefault(memory_id, {})[channel] = rank
                    scores[memory_id] = scores.get(memory_id, Fraction()) + Fraction(1, 60 + rank)

            if not scores:
                return []
            placeholders = ",".join("?" for _ in scores)
            rows = connection.execute(
                f"SELECT * FROM memory_item WHERE memory_id IN ({placeholders})",
                sorted(scores),
            ).fetchall()
            by_id = {row["memory_id"]: row for row in rows}
            ordered = sorted(
                scores,
                key=lambda memory_id: (
                    -scores[memory_id],
                    -self._timestamp_sort_value(by_id[memory_id]["recorded_at"]),
                    memory_id,
                ),
            )[:top_k]
            return [
                self._search_result(
                    connection,
                    by_id[memory_id],
                    ranks[memory_id],
                    scores[memory_id],
                    query,
                )
                for memory_id in ordered
            ]

    memory_search = search

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """Return one projected memory and all provenance evidence."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM memory_item WHERE memory_id = ?", (memory_id,)
            ).fetchone()
            return None if row is None else self._memory_result(connection, row)

    memory_get = get

    def _connect(self) -> sqlite3.Connection:
        self._assert_internal_path(self.database_path, "SQLite projection")
        for suffix in ("-wal", "-shm", "-journal"):
            self._assert_internal_path(
                Path(str(self.database_path) + suffix),
                f"SQLite projection {suffix}",
            )
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        return connection

    @staticmethod
    def _probe_capabilities(connection: sqlite3.Connection) -> None:
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE temp.memory_probe_unicode USING "
                "fts5(value, tokenize='unicode61 remove_diacritics 2')"
            )
            connection.execute(
                "CREATE VIRTUAL TABLE temp.memory_probe_trigram USING "
                "fts5(value, tokenize='trigram')"
            )
            connection.execute("DROP TABLE temp.memory_probe_unicode")
            connection.execute("DROP TABLE temp.memory_probe_trigram")
        except sqlite3.DatabaseError as error:
            raise CapabilityError(
                "SQLite must support FTS5 unicode61 and trigram tokenizers"
            ) from error

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> bool:
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        event_count = (
            int(connection.execute("SELECT COUNT(*) FROM event_index").fetchone()[0])
            if "event_index" in existing_tables
            else 0
        )
        existing_blob_columns = (
            {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(blob_catalog)").fetchall()
            }
            if "blob_catalog" in existing_tables
            else set()
        )
        current_version: str | None = None
        if "projection_metadata" in existing_tables:
            current = connection.execute(
                "SELECT value FROM projection_metadata WHERE key = 'schema_version'"
            ).fetchone()
            current_version = None if current is None else str(current[0])
        required_tables = {"blob_reference", "blob_citation", "task_terminal"}
        required_blob_columns = {
            "tombstone_locator",
            "tombstone_sha256",
            "excerpt_json",
            "event_provenance_json",
        }
        layout_upgrade = not required_tables.issubset(existing_tables) or not (
            required_blob_columns.issubset(existing_blob_columns)
        )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projection_metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_index (
              event_id TEXT PRIMARY KEY,
              host_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              seq INTEGER NOT NULL,
              event_type TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              segment_path TEXT NOT NULL,
              byte_offset INTEGER NOT NULL,
              payload_sha256 TEXT NOT NULL,
              record_sha256 TEXT NOT NULL UNIQUE,
              UNIQUE(host_id, session_id, seq)
            );
            CREATE TABLE IF NOT EXISTS memory_item (
              memory_id TEXT PRIMARY KEY,
              kind TEXT NOT NULL CHECK(kind IN
                ('constraint','preference','decision','fact','lesson','episode','procedure_ref')),
              subject TEXT NOT NULL,
              predicate TEXT NOT NULL,
              object_json TEXT NOT NULL CHECK(json_valid(object_json)),
              searchable_text TEXT NOT NULL,
              scope_key TEXT NOT NULL,
              owner_id TEXT NOT NULL,
              workspace_id TEXT,
              task_id TEXT,
              agent_role TEXT,
              session_id TEXT,
              status TEXT NOT NULL CHECK(status IN
                ('active','superseded','retired','disputed')),
              valid_from TEXT,
              valid_to TEXT,
              observed_at TEXT,
              recorded_at TEXT NOT NULL,
              review_after TEXT,
              verification_status TEXT NOT NULL CHECK(verification_status IN
                ('user_authority','observed','tested','source_verified','inferred')),
              supersedes_id TEXT REFERENCES memory_item(memory_id),
              source_event_id TEXT NOT NULL REFERENCES event_index(event_id),
              content_sha256 TEXT NOT NULL,
              schema_version INTEGER NOT NULL DEFAULT 1,
              UNIQUE(scope_key, content_sha256)
            );
            CREATE TABLE IF NOT EXISTS evidence (
              evidence_id TEXT PRIMARY KEY,
              memory_id TEXT REFERENCES memory_item(memory_id),
              task_id TEXT,
              source_kind TEXT NOT NULL CHECK(source_kind IN
                ('user','event','file','symbol','command','test','web','artifact','model_inference')),
              source_locator TEXT NOT NULL,
              source_event_id TEXT REFERENCES event_index(event_id),
              source_sha256 TEXT,
              observed_at TEXT NOT NULL,
              authority TEXT NOT NULL,
              excerpt TEXT,
              CHECK(memory_id IS NOT NULL OR task_id IS NOT NULL)
            );
            CREATE TABLE IF NOT EXISTS task_state (
              task_id TEXT PRIMARY KEY,
              revision INTEGER NOT NULL,
              state_json TEXT NOT NULL CHECK(json_valid(state_json)),
              state_sha256 TEXT NOT NULL,
              through_event_id TEXT NOT NULL REFERENCES event_index(event_id),
              committed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blob_catalog (
              blob_sha256 TEXT PRIMARY KEY,
              size_bytes INTEGER NOT NULL,
              media_type TEXT,
              hot_locator TEXT,
              retention_class TEXT NOT NULL CHECK(retention_class IN
                ('core','evidence','bulk','scratch')),
              availability TEXT NOT NULL CHECK(availability IN ('hot','archived','evicted')),
              created_at TEXT NOT NULL,
              task_terminal_at TEXT,
              expires_at TEXT,
              archive_locator TEXT,
              archive_sha256 TEXT,
              reference_count INTEGER NOT NULL,
              pin_reasons_json TEXT NOT NULL CHECK(json_valid(pin_reasons_json)),
              tombstone_locator TEXT,
              tombstone_sha256 TEXT,
              excerpt_json TEXT CHECK(excerpt_json IS NULL OR json_valid(excerpt_json)),
              event_provenance_json TEXT CHECK(
                event_provenance_json IS NULL OR json_valid(event_provenance_json)
              )
            );
            CREATE TABLE IF NOT EXISTS blob_reference (
              blob_sha256 TEXT NOT NULL,
              event_id TEXT PRIMARY KEY REFERENCES event_index(event_id),
              event_type TEXT NOT NULL,
              task_id TEXT,
              scope_key TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              record_sha256 TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS blob_reference_digest_idx
              ON blob_reference(blob_sha256);
            CREATE INDEX IF NOT EXISTS blob_reference_task_idx
              ON blob_reference(task_id);
            CREATE INDEX IF NOT EXISTS blob_reference_scope_idx
              ON blob_reference(scope_key);
            CREATE TABLE IF NOT EXISTS task_terminal (
              task_id TEXT PRIMARY KEY,
              terminal_event_id TEXT NOT NULL REFERENCES event_index(event_id),
              terminal_at TEXT NOT NULL,
              terminal_type TEXT NOT NULL CHECK(terminal_type IN ('task/completed','task/failed'))
            );
            CREATE TABLE IF NOT EXISTS blob_citation (
              citation_event_id TEXT NOT NULL REFERENCES event_index(event_id),
              source_event_id TEXT NOT NULL REFERENCES event_index(event_id),
              scope_key TEXT NOT NULL,
              citation_kind TEXT NOT NULL CHECK(citation_kind IN ('ordinary','production')),
              PRIMARY KEY(citation_event_id, source_event_id)
            );
            CREATE INDEX IF NOT EXISTS memory_scope_idx ON memory_item(scope_key);
            CREATE INDEX IF NOT EXISTS memory_workspace_idx ON memory_item(workspace_id);
            CREATE INDEX IF NOT EXISTS memory_task_idx ON memory_item(task_id);
            CREATE INDEX IF NOT EXISTS memory_subject_idx ON memory_item(subject);
            CREATE INDEX IF NOT EXISTS memory_predicate_idx ON memory_item(predicate);
            CREATE INDEX IF NOT EXISTS memory_status_idx ON memory_item(status);
            CREATE INDEX IF NOT EXISTS memory_validity_idx ON memory_item(valid_from, valid_to);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_unicode USING fts5(
              memory_id UNINDEXED,
              searchable_text,
              content='memory_item',
              content_rowid='rowid',
              tokenize='unicode61 remove_diacritics 2'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_trigram USING fts5(
              memory_id UNINDEXED,
              searchable_text,
              content='memory_item',
              content_rowid='rowid',
              tokenize='trigram'
            );
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(blob_catalog)").fetchall()
        }
        additions = {
            "tombstone_locator": "TEXT",
            "tombstone_sha256": "TEXT",
            "excerpt_json": "TEXT",
            "event_provenance_json": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE blob_catalog ADD COLUMN {name} {declaration}")
        upgrade_required = event_count > 0 and (
            layout_upgrade or current_version != PROJECTION_SCHEMA_VERSION
        )
        if not upgrade_required:
            connection.execute(
                """
                INSERT INTO projection_metadata (key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (PROJECTION_SCHEMA_VERSION,),
            )
        return upgrade_required

    def _mark_projection_schema_current(self) -> None:
        with self._exclusive_store_lock():
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO projection_metadata (key, value)
                        VALUES ('schema_version', ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (PROJECTION_SCHEMA_VERSION,),
                    )

    def _clear_projection(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM evidence")
        connection.execute("DELETE FROM task_state")
        connection.execute("DELETE FROM memory_item")
        connection.execute("DELETE FROM blob_citation")
        connection.execute("DELETE FROM task_terminal")
        connection.execute("DELETE FROM blob_reference")
        connection.execute("DELETE FROM blob_catalog")
        connection.execute("DELETE FROM event_index")
        connection.execute(
            "INSERT INTO memory_fts_unicode(memory_fts_unicode) VALUES('rebuild')"
        )
        connection.execute(
            "INSERT INTO memory_fts_trigram(memory_fts_trigram) VALUES('rebuild')"
        )

    def _apply_event(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        path: Path,
        offset: int,
        resolved_payload: Mapping[str, Any] | None = None,
        *,
        preflight: bool = False,
    ) -> None:
        self._index_event(connection, record, path, offset)
        self._apply_projection(
            connection, record, resolved_payload, preflight=preflight
        )

    def _index_event(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        path: Path,
        offset: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO event_index (
              event_id, host_id, session_id, seq, event_type, occurred_at,
              segment_path, byte_offset, payload_sha256, record_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["event_id"],
                record["host_id"],
                record["session_id"],
                record["seq"],
                record["type"],
                record["occurred_at"],
                str(path.relative_to(self.root)).replace("\\", "/"),
                offset,
                record["payload_sha256"],
                record["record_sha256"],
            ),
        )

    def _apply_projection(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        resolved_payload: Mapping[str, Any] | None = None,
        *,
        preflight: bool = False,
    ) -> None:
        event_type = record["type"]
        reducer_types = {
            "memory/committed",
            "memory/superseded",
            "memory/retired",
            "memory/disputed",
            "state/committed",
            "task/completed",
            "task/failed",
            "retention/cited",
            "production/claim",
            "blob/archived",
            "blob/evicted",
            "blob/rehydrated",
        }
        if resolved_payload is not None:
            payload = resolved_payload
        else:
            try:
                payload = self._resolve_payload(record)
            except BlobEvictedError:
                if event_type in reducer_types:
                    raise ProjectionError(
                        f"authoritative reducer payload was evicted: {record['event_id']}"
                    )
                payload = {}
        if event_type == "memory/committed":
            self._apply_memory_commit(connection, record, payload)
        elif event_type in {"memory/superseded", "memory/retired", "memory/disputed"}:
            self._apply_memory_status(connection, record, payload)
        elif event_type == "state/committed":
            self._apply_task_state(connection, record, payload)
        elif event_type in {"task/completed", "task/failed"}:
            self._apply_task_terminal(connection, record)
        elif event_type in {"retention/cited", "production/claim"}:
            self._apply_blob_citations(connection, record, payload)
        elif event_type in {"blob/archived", "blob/evicted"}:
            self._apply_blob_retention(connection, record, payload)
        elif event_type == "blob/rehydrated":
            self._apply_blob_rehydration(
                connection, record, payload, preflight=preflight
            )
        blob = self._blob_reference(record["payload"])
        if blob is not None:
            locator = str(self._blob_path(blob["sha256"]).relative_to(self.root)).replace(
                "\\", "/"
            )
            connection.execute(
                """
                INSERT INTO blob_catalog (
                  blob_sha256, size_bytes, media_type, hot_locator, retention_class,
                  availability, created_at, reference_count, pin_reasons_json
                ) VALUES (?, ?, ?, ?, ?, 'hot', ?, 1, '[]')
                ON CONFLICT(blob_sha256) DO UPDATE SET
                  reference_count = reference_count + 1,
                  retention_class = CASE
                    WHEN blob_catalog.retention_class = 'core'
                      OR excluded.retention_class = 'core' THEN 'core'
                    WHEN blob_catalog.retention_class = 'evidence'
                      OR excluded.retention_class = 'evidence' THEN 'evidence'
                    WHEN blob_catalog.retention_class = 'bulk'
                      OR excluded.retention_class = 'bulk' THEN 'bulk'
                    ELSE 'scratch'
                  END
                """,
                (
                    blob["sha256"],
                    blob["size_bytes"],
                    blob["media_type"],
                    locator,
                    blob["retention_class"],
                    record["occurred_at"],
                ),
            )
            connection.execute(
                """
                INSERT INTO blob_reference (
                  blob_sha256, event_id, event_type, task_id, scope_key,
                  occurred_at, record_sha256, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    blob["sha256"],
                    record["event_id"],
                    event_type,
                    record["task_id"],
                    MemoryScope.from_value(record["scope"]).key,
                    record["occurred_at"],
                    record["record_sha256"],
                    record["payload_sha256"],
                ),
            )
            self._refresh_blob_deadline(connection, blob["sha256"])
            self._refresh_blob_pin_reasons(connection, blob["sha256"])

    def _projection_order(
        self, events: Sequence[tuple[Path, int, dict[str, Any]]]
    ) -> list[tuple[Path, int, dict[str, Any]]]:
        """Order cross-segment reducers without weakening per-segment chain truth."""

        neutral: list[tuple[Path, int, dict[str, Any]]] = []
        commits: dict[str, tuple[Path, int, dict[str, Any]]] = {}
        statuses: list[tuple[Path, int, dict[str, Any]]] = []
        states: list[tuple[Path, int, dict[str, Any]]] = []
        terminals: list[tuple[Path, int, dict[str, Any]]] = []
        retention_events: list[tuple[Path, int, dict[str, Any]]] = []
        for item in events:
            record = item[2]
            if record["type"] == "memory/committed":
                payload = self._resolve_payload(record)
                if payload["memory_id"] in commits:
                    raise ProjectionError(
                        f"duplicate memory_id in canonical events: {payload['memory_id']}"
                    )
                commits[payload["memory_id"]] = item
            elif record["type"] in {
                "memory/superseded",
                "memory/retired",
                "memory/disputed",
            }:
                statuses.append(item)
            elif record["type"] == "state/committed":
                states.append(item)
            elif record["type"] in {"task/completed", "task/failed"}:
                terminals.append(item)
            elif record["type"] in {
                "retention/cited",
                "production/claim",
                "blob/archived",
                "blob/evicted",
                "blob/rehydrated",
            }:
                retention_events.append(item)
            else:
                neutral.append(item)

        ordered_commits: list[tuple[Path, int, dict[str, Any]]] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(memory_id: str) -> None:
            if memory_id in visited:
                return
            if memory_id in visiting:
                raise ProjectionError("memory supersession cycle in canonical events")
            visiting.add(memory_id)
            item = commits[memory_id]
            dependency = self._resolve_payload(item[2]).get("supersedes_id")
            if dependency in commits:
                visit(dependency)
            visiting.remove(memory_id)
            visited.add(memory_id)
            ordered_commits.append(item)

        for memory_id in sorted(commits):
            visit(memory_id)
        statuses.sort(key=lambda item: (item[2]["occurred_at"], item[2]["event_id"]))
        states.sort(
            key=lambda item: (
                item[2]["task_id"] or "",
                self._resolve_payload(item[2])["revision"],
                item[2]["event_id"],
            )
        )
        terminals.sort(key=lambda item: (item[2]["occurred_at"], item[2]["event_id"]))
        retention_events = self._order_retention_events(retention_events)
        return [
            *neutral,
            *ordered_commits,
            *statuses,
            *states,
            *terminals,
            *retention_events,
        ]

    def _order_retention_events(
        self, events: Sequence[tuple[Path, int, dict[str, Any]]]
    ) -> list[tuple[Path, int, dict[str, Any]]]:
        citations = [
            item
            for item in events
            if item[2]["type"] in {"retention/cited", "production/claim"}
        ]
        lifecycle = [item for item in events if item not in citations]
        citations.sort(key=lambda item: (item[2]["occurred_at"], item[2]["event_id"]))
        by_id = {str(item[2]["event_id"]): item for item in lifecycle}
        tombstone_event_by_sha: dict[str, str] = {}
        rehydrate_event_by_tombstone: dict[str, str] = {}
        metadata: dict[str, tuple[str, str | None, str]] = {}
        for item in lifecycle:
            record = item[2]
            event_id = str(record["event_id"])
            payload = self._resolve_payload(record)
            digest = str(payload["blob_sha256"])
            if record["type"] == "blob/rehydrated":
                predecessor = str(payload["expected_tombstone_sha256"])
                if predecessor in rehydrate_event_by_tombstone:
                    raise ProjectionError(
                        "multiple canonical rehydrations name one eviction tombstone"
                    )
                rehydrate_event_by_tombstone[predecessor] = event_id
                metadata[event_id] = (digest, predecessor, "rehydrated")
            else:
                receipt = self._read_tombstone(digest, event_id)
                tombstone_sha = str(receipt["tombstone_sha256"])
                tombstone_event_by_sha[tombstone_sha] = event_id
                predecessor_value = receipt["previous_tombstone_sha256"]
                metadata[event_id] = (
                    digest,
                    None if predecessor_value is None else str(predecessor_value),
                    "tombstone",
                )
        dependencies: dict[str, set[str]] = {event_id: set() for event_id in by_id}
        global_rehydrate_by_tombstone = dict(rehydrate_event_by_tombstone)
        for path in sorted(self.events_dir.glob("*/*.jsonl"), key=lambda item: item.as_posix()):
            for _, record in self._read_segment(path):
                if record["type"] != "blob/rehydrated":
                    continue
                payload = self._resolve_payload(record)
                predecessor = str(payload["expected_tombstone_sha256"])
                known = global_rehydrate_by_tombstone.get(predecessor)
                if known is not None and known != record["event_id"]:
                    raise ProjectionError(
                        "multiple canonical rehydrations name one eviction tombstone"
                    )
                global_rehydrate_by_tombstone[predecessor] = str(record["event_id"])
        for event_id, (digest, predecessor, kind) in metadata.items():
            if predecessor is None:
                continue
            if kind == "rehydrated":
                dependency = tombstone_event_by_sha.get(predecessor)
                if dependency is None:
                    dependency = str(
                        self._read_tombstone_by_sha256(digest, predecessor)[
                            "action_event_id"
                        ]
                    )
            else:
                dependency = global_rehydrate_by_tombstone.get(
                    predecessor
                )
                if dependency is None:
                    dependency = tombstone_event_by_sha.get(predecessor)
                if dependency is None:
                    dependency = str(
                        self._read_tombstone_by_sha256(digest, predecessor)[
                            "action_event_id"
                        ]
                    )
            if dependency is None:
                raise ProjectionError(
                    f"blob lifecycle predecessor is absent for {digest}: {predecessor}"
                )
            if dependency in by_id:
                dependencies[event_id].add(dependency)
            elif not self._event_id_exists(dependency):
                raise ProjectionError(
                    f"blob lifecycle predecessor is not projected for {digest}: {dependency}"
                )
        ordered: list[tuple[Path, int, dict[str, Any]]] = []
        remaining = dict(dependencies)
        while remaining:
            ready = sorted(
                (event_id for event_id, needs in remaining.items() if not needs),
                key=lambda event_id: (
                    by_id[event_id][2]["occurred_at"],
                    event_id,
                ),
            )
            if not ready:
                raise ProjectionError("blob lifecycle dependency cycle")
            for event_id in ready:
                ordered.append(by_id[event_id])
                remaining.pop(event_id)
                for needs in remaining.values():
                    needs.discard(event_id)
        return [*citations, *ordered]

    def _preflight_event(
        self,
        record: Mapping[str, Any],
        path: Path,
        offset: int,
        resolved_payload: Mapping[str, Any],
    ) -> None:
        """Exercise the reducer in a rolled-back transaction before raw append."""

        with closing(self._connect()) as connection:
            connection.execute("SAVEPOINT memory_preflight")
            try:
                self._apply_event(
                    connection,
                    record,
                    path,
                    offset,
                    resolved_payload,
                    preflight=True,
                )
            except Exception as error:
                connection.execute("ROLLBACK TO memory_preflight")
                connection.execute("RELEASE memory_preflight")
                if isinstance(error, ProjectionError):
                    raise
                raise ProjectionError(f"event rejected before append: {error}") from error
            connection.execute("ROLLBACK TO memory_preflight")
            connection.execute("RELEASE memory_preflight")

    def _apply_memory_commit(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        scope = MemoryScope.from_value(record["scope"])
        supersedes = payload.get("supersedes_id")
        if supersedes is not None:
            previous = connection.execute(
                "SELECT scope_key, status, source_event_id FROM memory_item WHERE memory_id = ?",
                (supersedes,),
            ).fetchone()
            if previous is None:
                raise ProjectionError(f"superseded memory does not exist: {supersedes}")
            if previous["scope_key"] != scope.key:
                raise ProjectionError("a memory cannot supersede a different exact scope")
            if previous["status"] not in {"active", "disputed"}:
                raise ProjectionError("only active or disputed memory can be superseded")
            connection.execute(
                "UPDATE memory_item SET status = 'superseded', valid_to = ? WHERE memory_id = ?",
                (payload.get("valid_from") or record["occurred_at"], supersedes),
            )
            previous_blob = connection.execute(
                "SELECT blob_sha256 FROM blob_reference WHERE event_id = ?",
                (previous["source_event_id"],),
            ).fetchone()
            if previous_blob is not None:
                self._refresh_blob_pin_reasons(
                    connection, str(previous_blob["blob_sha256"])
                )

        cursor = connection.execute(
            """
            INSERT INTO memory_item (
              memory_id, kind, subject, predicate, object_json, searchable_text,
              scope_key, owner_id, workspace_id, task_id, agent_role, session_id,
              status, valid_from, valid_to, observed_at, recorded_at, review_after,
              verification_status, supersedes_id, source_event_id, content_sha256,
              schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                payload["memory_id"],
                payload["kind"],
                payload["subject"],
                payload["predicate"],
                _canonical_text(payload["object"]),
                payload["searchable_text"],
                scope.key,
                scope.owner_id,
                scope.workspace_id,
                scope.task_id,
                scope.agent_role,
                scope.session_id,
                payload["status"],
                payload.get("valid_from"),
                payload.get("valid_to"),
                payload.get("observed_at"),
                payload["recorded_at"],
                payload.get("review_after"),
                payload["verification_status"],
                supersedes,
                record["event_id"],
                payload["content_sha256"],
            ),
        )
        rowid = cursor.lastrowid
        connection.execute(
            "INSERT INTO memory_fts_unicode(rowid, memory_id, searchable_text) VALUES (?, ?, ?)",
            (rowid, payload["memory_id"], payload["searchable_text"]),
        )
        connection.execute(
            "INSERT INTO memory_fts_trigram(rowid, memory_id, searchable_text) VALUES (?, ?, ?)",
            (rowid, payload["memory_id"], payload["searchable_text"]),
        )
        for item in payload["evidence"]:
            connection.execute(
                """
                INSERT INTO evidence (
                  evidence_id, memory_id, task_id, source_kind, source_locator,
                  source_event_id, source_sha256, observed_at, authority, excerpt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["evidence_id"],
                    payload["memory_id"],
                    record["task_id"],
                    item["source_kind"],
                    item["source_locator"],
                    item.get("source_event_id"),
                    item.get("source_sha256"),
                    item["observed_at"],
                    item["authority"],
                    item.get("excerpt"),
                ),
            )

    def _apply_memory_status(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        status = record["type"].split("/", 1)[1]
        scope_key = MemoryScope.from_value(record["scope"]).key
        current = connection.execute(
            "SELECT scope_key, source_event_id FROM memory_item WHERE memory_id = ?",
            (payload["memory_id"],),
        ).fetchone()
        if current is None:
            raise ProjectionError(f"memory does not exist: {payload['memory_id']}")
        if current["scope_key"] != scope_key:
            raise ProjectionError("a status event cannot mutate a different exact scope")
        cursor = connection.execute(
            "UPDATE memory_item SET status = ?, valid_to = COALESCE(?, valid_to) "
            "WHERE memory_id = ? AND scope_key = ?",
            (status, payload.get("valid_to"), payload["memory_id"], scope_key),
        )
        if cursor.rowcount != 1:
            raise ProjectionError(f"memory does not exist: {payload['memory_id']}")
        memory_blob = connection.execute(
            "SELECT blob_sha256 FROM blob_reference WHERE event_id = ?",
            (current["source_event_id"],),
        ).fetchone()
        if memory_blob is not None:
            self._refresh_blob_pin_reasons(connection, str(memory_blob["blob_sha256"]))

    @staticmethod
    def _apply_task_state(
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        state_text = _canonical_text(payload["state"])
        state_hash = task_state_sha256(payload["state"])
        previous = connection.execute(
            "SELECT revision, state_sha256 FROM task_state WHERE task_id = ?",
            (record["task_id"],),
        ).fetchone()
        if previous is None:
            if payload["revision"] != 1:
                raise ProjectionError("first task-state revision must be 1")
            if payload["state"].get("previous_state_sha256") is not None:
                raise ProjectionError("first task-state revision must have no previous hash")
        else:
            if payload["revision"] != previous["revision"] + 1:
                raise ProjectionError("task-state revision must increase by exactly one")
            if payload["state"].get("previous_state_sha256") != previous["state_sha256"]:
                raise ProjectionError("task-state previous_state_sha256 mismatch")
        connection.execute(
            """
            INSERT INTO task_state (
              task_id, revision, state_json, state_sha256, through_event_id, committed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              revision=excluded.revision,
              state_json=excluded.state_json,
              state_sha256=excluded.state_sha256,
              through_event_id=excluded.through_event_id,
              committed_at=excluded.committed_at
            """,
            (
                record["task_id"],
                payload["revision"],
                state_text,
                state_hash,
                record["event_id"],
                record["occurred_at"],
            ),
        )

    def _apply_task_terminal(
        self, connection: sqlite3.Connection, record: Mapping[str, Any]
    ) -> None:
        task_id = record["task_id"]
        if task_id is None:
            raise ProjectionError(f"{record['type']} requires task_id")
        current = connection.execute(
            "SELECT terminal_event_id, terminal_at, terminal_type FROM task_terminal "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        expected = (record["event_id"], record["occurred_at"], record["type"])
        if current is not None and tuple(current) != expected:
            raise ProjectionError(f"task already has a different terminal event: {task_id}")
        if current is None:
            connection.execute(
                """
                INSERT INTO task_terminal (
                  task_id, terminal_event_id, terminal_at, terminal_type
                ) VALUES (?, ?, ?, ?)
                """,
                (task_id, record["event_id"], record["occurred_at"], record["type"]),
            )
        digests = [
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT blob_sha256 FROM blob_reference WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        ]
        for digest in sorted(digests):
            self._refresh_blob_deadline(connection, digest)
            self._refresh_blob_pin_reasons(connection, digest)

    def _apply_blob_citations(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        source_event_ids = payload.get("source_event_ids", [])
        if not isinstance(source_event_ids, list) or any(
            not isinstance(item, str) for item in source_event_ids
        ):
            raise ProjectionError("retention citation source_event_ids must be a string list")
        scope_key = MemoryScope.from_value(record["scope"]).key
        citation_kind = "production" if record["type"] == "production/claim" else "ordinary"
        for source_event_id in sorted(set(source_event_ids)):
            source = connection.execute(
                "SELECT scope_key FROM blob_reference WHERE event_id = ?", (source_event_id,)
            ).fetchone()
            if source is None:
                raise ProjectionError(f"citation source event has no blob: {source_event_id}")
            if source["scope_key"] != scope_key:
                raise ProjectionError("a citation cannot cross an exact memory scope")
            connection.execute(
                """
                INSERT INTO blob_citation (
                  citation_event_id, source_event_id, scope_key, citation_kind
                ) VALUES (?, ?, ?, ?)
                """,
                (record["event_id"], source_event_id, scope_key, citation_kind),
            )
            digest = connection.execute(
                "SELECT blob_sha256 FROM blob_reference WHERE event_id = ?",
                (source_event_id,),
            ).fetchone()
            if digest is not None:
                self._refresh_blob_pin_reasons(connection, str(digest["blob_sha256"]))

    def _apply_blob_retention(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        digest = payload.get("blob_sha256")
        action = record["type"].split("/", 1)[1]
        expected_availability = "archived" if action == "archived" else "evicted"
        row = connection.execute(
            "SELECT size_bytes, retention_class FROM blob_catalog WHERE blob_sha256 = ?",
            (digest,),
        ).fetchone()
        if row is None:
            raise ProjectionError(f"retention target blob does not exist: {digest}")
        if payload.get("size_bytes") != row["size_bytes"]:
            raise ProjectionError("retention byte count does not match blob catalog")
        if payload.get("retention_class") != row["retention_class"]:
            raise ProjectionError("retention class does not match blob catalog")
        receipt = self._read_tombstone(str(digest), str(record["event_id"]))
        if receipt["action_event_id"] != record["event_id"]:
            raise ProjectionError("retention tombstone event identity mismatch")
        if receipt["availability"] != expected_availability:
            raise ProjectionError("retention tombstone availability mismatch")
        if receipt["tombstone_sha256"] != payload.get("tombstone_sha256"):
            raise ProjectionError("retention tombstone digest mismatch")
        expires_at = None
        if expected_availability == "archived" and row["retention_class"] == "bulk":
            expires_at = _utc_text(
                datetime.fromisoformat(record["occurred_at"].replace("Z", "+00:00"))
                + timedelta(seconds=BULK_ARCHIVE_SECONDS)
            )
        connection.execute(
            """
            UPDATE blob_catalog SET
              availability = ?, hot_locator = NULL, expires_at = ?,
              archive_locator = ?, archive_sha256 = ?,
              tombstone_locator = ?, tombstone_sha256 = ?,
              excerpt_json = ?, event_provenance_json = ?, pin_reasons_json = ?
            WHERE blob_sha256 = ?
            """,
            (
                expected_availability,
                expires_at,
                receipt.get("archive_locator"),
                receipt.get("archive_sha256"),
                self._tombstone_locator(str(digest), str(record["event_id"])),
                receipt["tombstone_sha256"],
                _canonical_text(receipt["excerpt"]),
                _canonical_text(receipt["event_provenance"]),
                _canonical_text(receipt["pin_reasons"]),
                digest,
            ),
        )

    def _apply_blob_rehydration(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        preflight: bool,
    ) -> None:
        if (
            record.get("host_id") != "memory-rehydration"
            or record.get("actor")
            != {"kind": "system", "id": "memory-v1-rehydration", "model": None}
        ):
            raise ProjectionError("blob rehydration requires the canonical lifecycle owner")
        digest = str(payload.get("blob_sha256"))
        row = connection.execute(
            "SELECT * FROM blob_catalog WHERE blob_sha256 = ?", (digest,)
        ).fetchone()
        if row is None:
            raise ProjectionError(f"rehydration target blob does not exist: {digest}")
        if row["availability"] != "evicted":
            raise ProjectionError("rehydration target is not evicted at this lifecycle point")
        if payload.get("size_bytes") != row["size_bytes"]:
            raise ProjectionError("rehydration byte count does not match blob catalog")
        if payload.get("retention_class") != row["retention_class"]:
            raise ProjectionError("rehydration retention class does not match blob catalog")
        expected_tombstone = str(payload.get("expected_tombstone_sha256"))
        receipt = self._read_tombstone_by_sha256(digest, expected_tombstone)
        if receipt["availability"] != "evicted":
            raise ProjectionError("rehydration predecessor is not an eviction tombstone")
        references = connection.execute(
            "SELECT event_id, scope_key FROM blob_reference "
            "WHERE blob_sha256 = ? ORDER BY event_id",
            (digest,),
        ).fetchall()
        source_event_ids = sorted(str(item["event_id"]) for item in references)
        scope_keys = sorted({str(item["scope_key"]) for item in references})
        if payload.get("source_event_ids") != source_event_ids or source_event_ids != receipt[
            "source_event_ids"
        ]:
            raise ProjectionError("rehydration source-event provenance mismatch")
        if payload.get("scope_keys") != scope_keys or scope_keys != receipt["scope_keys"]:
            raise ProjectionError("rehydration exact scope mismatch")
        provenance = payload.get("provenance")
        if not isinstance(provenance, Mapping) or provenance.get("source_sha256") != digest:
            raise ProjectionError("rehydration byte provenance mismatch")
        expected_locator = str(
            self._rehydration_stage_path(str(record["event_id"])).relative_to(self.root)
        ).replace("\\", "/")
        if payload.get("staged_locator") != expected_locator:
            raise ProjectionError("rehydration staged locator is not event-owned")

        later_lifecycle = self._has_later_blob_lifecycle_event(
            digest,
            str(record["occurred_at"]),
            str(record["event_id"]),
            expected_tombstone_sha256=expected_tombstone,
        )
        hot_locator = str(self._blob_path(digest).relative_to(self.root)).replace("\\", "/")
        if not preflight and not later_lifecycle:
            hot_locator = self._promote_rehydration_stage(
                str(record["event_id"]), digest, int(row["size_bytes"])
            )
        elif preflight:
            stage = self._retention_path(
                self._rehydration_stage_path(str(record["event_id"])),
                self.rehydration_dir,
                "rehydration preflight stage",
                must_exist=True,
            )
            staged = stage.read_bytes()
            if len(staged) != row["size_bytes"] or _sha256(staged) != digest:
                raise ChainValidationError("rehydration preflight stage integrity failure")

        connection.execute(
            "UPDATE blob_catalog SET availability = 'hot', hot_locator = ?, "
            "archive_locator = NULL, archive_sha256 = NULL WHERE blob_sha256 = ?",
            (hot_locator, digest),
        )
        self._refresh_blob_deadline(connection, digest)
        self._refresh_blob_pin_reasons(connection, digest)

    def _has_later_blob_lifecycle_event(
        self,
        digest: str,
        occurred_at: str,
        event_id: str,
        *,
        expected_tombstone_sha256: str | None = None,
    ) -> bool:
        if expected_tombstone_sha256 is not None:
            directory = self._retention_path(
                self._tombstone_directory(digest),
                self.tombstones_dir,
                "later lifecycle tombstone lookup",
            )
            if directory.exists():
                for path in sorted(directory.glob("*.json")):
                    receipt = self._read_tombstone(digest, path.stem)
                    if receipt["previous_tombstone_sha256"] == expected_tombstone_sha256:
                        return True
        current_key = (occurred_at, event_id)
        for path in sorted(self.events_dir.glob("*/*.jsonl"), key=lambda item: item.as_posix()):
            for _, candidate in self._read_segment(path):
                if candidate["type"] not in {
                    "blob/archived",
                    "blob/evicted",
                    "blob/rehydrated",
                }:
                    continue
                payload = self._resolve_payload(candidate)
                if payload.get("blob_sha256") != digest:
                    continue
                if (str(candidate["occurred_at"]), str(candidate["event_id"])) > current_key:
                    return True
        return False

    def _refresh_blob_deadline(self, connection: sqlite3.Connection, digest: str) -> None:
        row = connection.execute(
            "SELECT retention_class, availability FROM blob_catalog WHERE blob_sha256 = ?",
            (digest,),
        ).fetchone()
        if row is None or row["availability"] != "hot":
            return
        references = connection.execute(
            "SELECT DISTINCT task_id FROM blob_reference WHERE blob_sha256 = ?",
            (digest,),
        ).fetchall()
        terminal_times: list[str] = []
        for reference in references:
            task_id = reference["task_id"]
            if task_id is None:
                connection.execute(
                    "UPDATE blob_catalog SET task_terminal_at = NULL, expires_at = NULL "
                    "WHERE blob_sha256 = ?",
                    (digest,),
                )
                return
            terminal = connection.execute(
                "SELECT terminal_at FROM task_terminal WHERE task_id = ?", (task_id,)
            ).fetchone()
            if terminal is None:
                connection.execute(
                    "UPDATE blob_catalog SET task_terminal_at = NULL, expires_at = NULL "
                    "WHERE blob_sha256 = ?",
                    (digest,),
                )
                return
            terminal_times.append(str(terminal["terminal_at"]))
        if not terminal_times:
            return
        terminal_at = max(terminal_times)
        seconds = {
            "scratch": SCRATCH_HOT_SECONDS,
            "bulk": BULK_HOT_SECONDS,
            "evidence": EVIDENCE_HOT_SECONDS,
            "core": None,
        }[row["retention_class"]]
        expires_at = None
        if seconds is not None:
            terminal = datetime.fromisoformat(terminal_at.replace("Z", "+00:00"))
            expires_at = _utc_text(terminal + timedelta(seconds=seconds))
        connection.execute(
            "UPDATE blob_catalog SET task_terminal_at = ?, expires_at = ? "
            "WHERE blob_sha256 = ?",
            (terminal_at, expires_at, digest),
        )

    def _refresh_blob_pin_reasons(
        self, connection: sqlite3.Connection, digest: str
    ) -> None:
        catalog = connection.execute(
            "SELECT * FROM blob_catalog WHERE blob_sha256 = ?", (digest,)
        ).fetchone()
        if catalog is None:
            return
        references = connection.execute(
            "SELECT * FROM blob_reference WHERE blob_sha256 = ? ORDER BY event_id",
            (digest,),
        ).fetchall()
        reasons = self._blob_pin_reasons(
            connection, catalog, references, exact_scope_keys=()
        )
        connection.execute(
            "UPDATE blob_catalog SET pin_reasons_json = ? WHERE blob_sha256 = ?",
            (_canonical_text(list(reasons)), digest),
        )

    def _normalize_payload(
        self,
        event_type: str,
        payload: dict[str, Any],
        event_id: str,
        task_id: str | None,
        occurred_at: str,
    ) -> dict[str, Any]:
        _canonical_bytes(payload)
        if event_type == "memory/committed":
            required = {
                "memory_id",
                "kind",
                "subject",
                "predicate",
                "object",
                "searchable_text",
                "verification_status",
                "evidence",
            }
            missing = required - set(payload)
            if missing:
                raise MemoryStoreError(f"memory payload missing: {sorted(missing)}")
            if payload["kind"] not in _MEMORY_KINDS:
                raise MemoryStoreError(f"invalid memory kind: {payload['kind']}")
            for field in ("memory_id", "subject", "predicate", "searchable_text"):
                if not isinstance(payload[field], str) or not payload[field]:
                    raise MemoryStoreError(f"memory {field} must be non-empty text")
            status = payload.setdefault("status", "active")
            if status not in _MEMORY_STATUSES:
                raise MemoryStoreError(f"invalid memory status: {status}")
            if payload["verification_status"] not in _VERIFICATION_STATUSES:
                raise MemoryStoreError("invalid verification_status")
            for field in ("valid_from", "valid_to", "observed_at", "review_after"):
                if payload.get(field) is not None:
                    payload[field] = _utc_text(payload[field])
            payload.setdefault("recorded_at", occurred_at)
            payload["recorded_at"] = _utc_text(payload["recorded_at"])
            if payload.get("supersedes_id") == payload["memory_id"]:
                raise MemoryStoreError("memory cannot supersede itself")
            content = {
                "kind": payload["kind"],
                "subject": payload["subject"],
                "predicate": payload["predicate"],
                "object": payload["object"],
                "searchable_text": payload["searchable_text"],
            }
            computed_content_hash = _sha256(_canonical_bytes(content))
            supplied_hash = payload.get("content_sha256")
            if supplied_hash is not None and supplied_hash != computed_content_hash:
                raise MemoryStoreError("content_sha256 mismatch")
            payload["content_sha256"] = computed_content_hash
            evidence = payload["evidence"]
            if not isinstance(evidence, list) or not evidence:
                raise MemoryStoreError("committed memory requires at least one evidence row")
            normalized_evidence = []
            namespace = uuid.UUID(event_id)
            for index, raw in enumerate(evidence):
                if not isinstance(raw, Mapping):
                    raise MemoryStoreError("evidence rows must be mappings")
                item = dict(raw)
                item.setdefault("evidence_id", str(uuid.uuid5(namespace, f"evidence:{index}")))
                if item.get("source_kind") not in _EVIDENCE_KINDS:
                    raise MemoryStoreError("invalid evidence source_kind")
                if not isinstance(item.get("source_locator"), str) or not item["source_locator"]:
                    raise MemoryStoreError("evidence source_locator must be non-empty text")
                if not isinstance(item.get("authority"), str) or not item["authority"]:
                    raise MemoryStoreError("evidence authority must be non-empty text")
                item.setdefault("source_event_id", event_id)
                item.setdefault("observed_at", payload.get("observed_at") or occurred_at)
                item["observed_at"] = _utc_text(item["observed_at"])
                if item.get("source_sha256") is not None and not _SHA256_RE.fullmatch(
                    item["source_sha256"]
                ):
                    raise MemoryStoreError("evidence source_sha256 must be sha256:<hex>")
                normalized_evidence.append(item)
            payload["evidence"] = normalized_evidence
        elif event_type in {"memory/superseded", "memory/retired", "memory/disputed"}:
            if not isinstance(payload.get("memory_id"), str) or not payload["memory_id"]:
                raise MemoryStoreError("memory status event requires memory_id")
            if event_type in {"memory/superseded", "memory/retired"}:
                payload.setdefault("valid_to", occurred_at)
                payload["valid_to"] = _utc_text(payload["valid_to"])
            elif payload.get("valid_to") is not None:
                raise MemoryStoreError("memory/disputed cannot close the validity interval")
        elif event_type == "state/committed":
            if task_id is None:
                raise MemoryStoreError("state/committed requires task_id")
            if not isinstance(payload.get("revision"), int) or payload["revision"] < 1:
                raise MemoryStoreError("state revision must be a positive integer")
            state = payload.get("state")
            if not isinstance(state, dict):
                raise MemoryStoreError("state/committed requires a state object")
            state.setdefault("through_event_id", event_id)
            try:
                state = validate_task_state(state)
            except TaskStateError as error:
                raise MemoryStoreError(f"invalid task state: {error}") from error
            if state["task_id"] != task_id:
                raise MemoryStoreError("state task_id must equal event task_id")
            if state["through_event_id"] != event_id:
                raise MemoryStoreError("state through_event_id must equal commit event_id")
            payload["state"] = state
        elif event_type in {"task/completed", "task/failed"}:
            if task_id is None:
                raise MemoryStoreError(f"{event_type} requires task_id")
        elif event_type == "retention/cited":
            source_event_ids = payload.get("source_event_ids")
            if not isinstance(source_event_ids, list) or not source_event_ids:
                raise MemoryStoreError("retention/cited requires source_event_ids")
            normalized_ids = []
            for source_event_id in source_event_ids:
                normalized_ids.append(
                    _uuid_text(source_event_id, "source_event_id")  # type: ignore[arg-type]
                )
            payload["source_event_ids"] = sorted(set(normalized_ids))
        elif event_type == "production/claim":
            source_event_ids = payload.get("source_event_ids")
            if not isinstance(source_event_ids, list) or not source_event_ids:
                raise MemoryStoreError(
                    "production/claim requires non-empty source_event_ids"
                )
            payload["source_event_ids"] = sorted(
                {
                    _uuid_text(item, "source_event_id")  # type: ignore[arg-type]
                    for item in source_event_ids
                }
            )
        elif event_type in {"blob/archived", "blob/evicted"}:
            required = {
                "blob_sha256",
                "size_bytes",
                "retention_class",
                "availability",
                "plan_sha256",
                "tombstone_sha256",
                "source_event_ids",
                "archive_locator",
                "archive_sha256",
            }
            if set(payload) != required:
                raise MemoryStoreError("blob retention payload fields do not match v1")
            for field in ("blob_sha256", "plan_sha256", "tombstone_sha256"):
                if not isinstance(payload[field], str) or not _SHA256_RE.fullmatch(
                    payload[field]
                ):
                    raise MemoryStoreError(f"blob retention {field} is invalid")
            if payload["retention_class"] not in _RETENTION_CLASSES:
                raise MemoryStoreError("blob retention class is invalid")
            expected = "archived" if event_type == "blob/archived" else "evicted"
            if payload["availability"] != expected:
                raise MemoryStoreError("blob retention availability is invalid")
            if not isinstance(payload["size_bytes"], int) or payload["size_bytes"] < 0:
                raise MemoryStoreError("blob retention size_bytes is invalid")
            source_event_ids = payload["source_event_ids"]
            if not isinstance(source_event_ids, list) or not source_event_ids:
                raise MemoryStoreError("blob retention requires source event provenance")
            payload["source_event_ids"] = sorted(
                {
                    _uuid_text(item, "source_event_id")  # type: ignore[arg-type]
                    for item in source_event_ids
                }
            )
            if expected == "archived":
                if not isinstance(payload["archive_locator"], str) or not payload[
                    "archive_locator"
                ]:
                    raise MemoryStoreError("archived blob requires archive_locator")
                if not isinstance(payload["archive_sha256"], str) or not _SHA256_RE.fullmatch(
                    payload["archive_sha256"]
                ):
                    raise MemoryStoreError("archived blob requires archive_sha256")
            elif payload["archive_locator"] is not None or payload["archive_sha256"] is not None:
                raise MemoryStoreError("evicted blob cannot claim an archive")
        elif event_type == "blob/rehydrated":
            required = {
                "schema",
                "blob_sha256",
                "size_bytes",
                "retention_class",
                "expected_tombstone_sha256",
                "source_event_ids",
                "scope_keys",
                "provenance",
                "staged_locator",
            }
            if set(payload) != required or payload.get("schema") != BLOB_REHYDRATION_SCHEMA:
                raise MemoryStoreError("blob rehydration payload fields do not match v1")
            for field in ("blob_sha256", "expected_tombstone_sha256"):
                if not isinstance(payload[field], str) or not _SHA256_RE.fullmatch(
                    payload[field]
                ):
                    raise MemoryStoreError(f"blob rehydration {field} is invalid")
            if (
                not isinstance(payload["size_bytes"], int)
                or isinstance(payload["size_bytes"], bool)
                or payload["size_bytes"] < 1
            ):
                raise MemoryStoreError("blob rehydration size_bytes is invalid")
            if payload["retention_class"] not in _RETENTION_CLASSES:
                raise MemoryStoreError("blob rehydration retention class is invalid")
            source_event_ids = payload["source_event_ids"]
            if not isinstance(source_event_ids, list) or not source_event_ids:
                raise MemoryStoreError("blob rehydration requires source event ids")
            normalized_source_ids = sorted(
                {
                    _uuid_text(item, "source_event_id")  # type: ignore[arg-type]
                    for item in source_event_ids
                }
            )
            if len(normalized_source_ids) != len(source_event_ids):
                raise MemoryStoreError("blob rehydration source event ids must be unique")
            payload["source_event_ids"] = normalized_source_ids
            scope_keys = payload["scope_keys"]
            if not isinstance(scope_keys, list) or not scope_keys:
                raise MemoryStoreError("blob rehydration requires exact scope keys")
            normalized_scope_keys: list[str] = []
            for scope_key in scope_keys:
                if not isinstance(scope_key, str):
                    raise MemoryStoreError("blob rehydration scope keys must be text")
                try:
                    scope_parts = json.loads(scope_key)
                except json.JSONDecodeError as error:
                    raise MemoryStoreError("blob rehydration scope key is invalid JSON") from error
                if not isinstance(scope_parts, list) or len(scope_parts) != 5:
                    raise MemoryStoreError("blob rehydration scope key does not match v1")
                normalized_scope_keys.append(
                    MemoryScope.from_value(
                        {
                            "owner_id": scope_parts[0],
                            "workspace_id": scope_parts[1],
                            "task_id": scope_parts[2],
                            "agent_role": scope_parts[3],
                            "session_id": scope_parts[4],
                        }
                    ).key
                )
            if sorted(set(normalized_scope_keys)) != normalized_scope_keys:
                raise MemoryStoreError("blob rehydration scope keys must be unique and sorted")
            payload["scope_keys"] = normalized_scope_keys
            payload["provenance"] = self._normalize_rehydration_provenance(
                payload["provenance"], payload["blob_sha256"]
            )
            expected_locator = str(
                self._rehydration_stage_path(event_id).relative_to(self.root)
            ).replace("\\", "/")
            if payload["staged_locator"] != expected_locator:
                raise MemoryStoreError("blob rehydration staged locator is not event-owned")
        _canonical_bytes(payload)
        return payload

    def _read_segment(self, path: Path) -> list[tuple[int, dict[str, Any]]]:
        path = self._retention_path(path, self.events_dir, "event segment read")
        if not path.exists():
            return []
        expected_host = path.parent.name
        expected_session = path.stem
        rows: list[tuple[int, dict[str, Any]]] = []
        previous: str | None = None
        offset = 0
        with path.open("rb") as stream:
            for number, line in enumerate(stream, start=1):
                if not line.endswith(b"\n"):
                    raise ChainValidationError(f"unterminated event at {path}:{number}")
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ChainValidationError(f"invalid JSON at {path}:{number}") from error
                if not isinstance(record, dict) or set(record) != _EVENT_FIELDS:
                    raise ChainValidationError(f"invalid event fields at {path}:{number}")
                if _canonical_bytes(record) + b"\n" != line:
                    raise ChainValidationError(f"non-canonical JSON at {path}:{number}")
                if record["schema"] != EVENT_SCHEMA:
                    raise ChainValidationError(f"unknown schema at {path}:{number}")
                try:
                    self._validate_host_id(record["host_id"])
                    _uuid_text(record["event_id"], "event_id")
                    _uuid_text(record["session_id"], "session_id")
                    task_id = _uuid_text(record["task_id"], "task_id", nullable=True)
                    _uuid_text(record["parent_event_id"], "parent_event_id", nullable=True)
                    _utc_text(record["occurred_at"])
                    if not isinstance(record["actor"], dict) or set(record["actor"]) != {
                        "kind",
                        "id",
                        "model",
                    }:
                        raise MemoryStoreError("actor fields do not match v1")
                    self._validate_actor(record["actor"])
                    if not isinstance(record["scope"], dict) or set(record["scope"]) != {
                        "owner_id",
                        "workspace_id",
                        "task_id",
                        "agent_role",
                        "session_id",
                    }:
                        raise MemoryStoreError("scope fields do not match v1")
                    scope = MemoryScope.from_value(record["scope"])
                    if scope.session_id not in {None, record["session_id"]}:
                        raise MemoryStoreError("scope names a different session")
                    if scope.task_id not in {None, task_id}:
                        raise MemoryStoreError("scope names a different task")
                    if not isinstance(record["type"], str) or "/" not in record["type"]:
                        raise MemoryStoreError("event type is not namespaced")
                    if not isinstance(record["payload"], dict):
                        raise MemoryStoreError("payload is not an object")
                except MemoryStoreError as error:
                    raise ChainValidationError(
                        f"invalid event schema at {path}:{number}: {error}"
                    ) from error
                if record["host_id"] != expected_host or record["session_id"] != expected_session:
                    raise ChainValidationError(f"segment identity mismatch at {path}:{number}")
                if (
                    not isinstance(record["seq"], int)
                    or isinstance(record["seq"], bool)
                    or record["seq"] != number
                ):
                    raise ChainValidationError(f"broken sequence at {path}:{number}")
                if record["previous_record_sha256"] != previous:
                    raise ChainValidationError(f"broken previous hash at {path}:{number}")
                if previous is not None and not _SHA256_RE.fullmatch(previous):
                    raise ChainValidationError(f"invalid previous hash at {path}:{number}")
                if not _SHA256_RE.fullmatch(record["payload_sha256"]):
                    raise ChainValidationError(f"invalid payload hash at {path}:{number}")
                if not _SHA256_RE.fullmatch(record["record_sha256"]):
                    raise ChainValidationError(f"invalid record hash at {path}:{number}")
                without_hash = dict(record)
                record_hash = without_hash.pop("record_sha256")
                if _sha256(_canonical_bytes(without_hash)) != record_hash:
                    raise ChainValidationError(f"record hash mismatch at {path}:{number}")
                try:
                    payload = self._payload_bytes(record["payload"])
                except BlobEvictedError as error:
                    blob = self._blob_reference(record["payload"])
                    if blob is None:
                        raise
                    receipt = self._read_tombstone(blob["sha256"])
                    if (
                        receipt["size_bytes"] != blob["size_bytes"]
                        or receipt["retention_class"] != blob["retention_class"]
                    ):
                        raise ChainValidationError(
                            f"evicted blob tombstone metadata mismatch at {path}:{number}"
                        ) from error
                    if record["type"] in {
                        "memory/committed",
                        "memory/superseded",
                        "memory/retired",
                        "memory/disputed",
                        "state/committed",
                        "task/completed",
                        "task/failed",
                        "verification/result",
                        "artifact/observed",
                        "retention/cited",
                        "production/claim",
                        "blob/archived",
                        "blob/evicted",
                        "blob/rehydrated",
                    }:
                        raise ChainValidationError(
                            f"authoritative event payload was evicted at {path}:{number}"
                        ) from error
                else:
                    if _sha256(payload) != record["payload_sha256"]:
                        raise ChainValidationError(f"payload hash mismatch at {path}:{number}")
                    try:
                        payload_value = json.loads(payload)
                        if not isinstance(payload_value, dict):
                            raise MemoryStoreError("payload is not an object")
                        normalized = self._normalize_payload(
                            record["type"],
                            payload_value,
                            record["event_id"],
                            record["task_id"],
                            record["occurred_at"],
                        )
                        if _canonical_bytes(normalized) != payload:
                            raise MemoryStoreError("payload is not in normalized v1 form")
                    except (MemoryStoreError, json.JSONDecodeError) as error:
                        raise ChainValidationError(
                            f"invalid payload contract at {path}:{number}: {error}"
                        ) from error
                previous = record_hash
                rows.append((offset, record))
                offset += len(line)
        return rows

    def _payload_bytes(self, stored_payload: Any) -> bytes:
        blob = self._blob_reference(stored_payload)
        if blob is None:
            return _canonical_bytes(stored_payload)
        return self._read_blob(blob["sha256"], blob["size_bytes"])

    def _resolve_payload(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = self._payload_bytes(record["payload"])
        if _sha256(raw) != record["payload_sha256"]:
            raise ChainValidationError(f"payload hash mismatch for {record['event_id']}")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ProjectionError("event payload must be a JSON object")
        return value

    @staticmethod
    def _blob_reference(stored_payload: Any) -> dict[str, Any] | None:
        if not isinstance(stored_payload, dict) or set(stored_payload) != {"$blob"}:
            return None
        value = stored_payload["$blob"]
        required = {"sha256", "size_bytes", "media_type", "retention_class"}
        if not isinstance(value, dict) or set(value) != required:
            raise ChainValidationError("invalid blob reference")
        if not _SHA256_RE.fullmatch(value.get("sha256", "")):
            raise ChainValidationError("invalid blob digest")
        if not isinstance(value.get("size_bytes"), int) or value["size_bytes"] < 0:
            raise ChainValidationError("invalid blob byte count")
        if value.get("media_type") != "application/json":
            raise ChainValidationError("unsupported blob media type")
        if value.get("retention_class") not in _RETENTION_CLASSES:
            raise ChainValidationError("invalid blob retention class")
        return value

    def _blob_path(self, digest: str) -> Path:
        hexadecimal = digest.removeprefix("sha256:")
        return self.blobs_dir / hexadecimal[:2] / hexadecimal

    def _tombstone_directory(self, digest: str) -> Path:
        hexadecimal = digest.removeprefix("sha256:")
        return self.tombstones_dir / hexadecimal[:2] / hexadecimal

    def _tombstone_path(self, digest: str, action_event_id: str | None = None) -> Path:
        directory = self._retention_path(
            self._tombstone_directory(digest),
            self.tombstones_dir,
            "tombstone directory",
        )
        if action_event_id is not None:
            return self._retention_path(
                directory / f"{action_event_id}.json",
                self.tombstones_dir,
                "tombstone path",
            )
        candidates = sorted(directory.glob("*.json")) if directory.exists() else []
        if not candidates:
            return self._retention_path(
                directory / "missing.json",
                self.tombstones_dir,
                "missing tombstone path",
            )
        ranked: list[tuple[str, str, Path]] = []
        for lexical_path in candidates:
            path = self._retention_path(
                lexical_path,
                self.tombstones_dir,
                "tombstone candidate read",
                must_exist=True,
            )
            try:
                value = json.loads(path.read_bytes())
                ranked.append(
                    (str(value.get("decided_at", "")), str(value.get("action_event_id", "")), path)
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                ranked.append(("", "", path))
        return max(ranked)[2]

    def _tombstone_locator(self, digest: str, action_event_id: str) -> str:
        return str(
            self._tombstone_path(digest, action_event_id).relative_to(self.root)
        ).replace("\\", "/")

    def _read_tombstone(
        self, digest: str, action_event_id: str | None = None
    ) -> dict[str, Any]:
        path = self._retention_path(
            self._tombstone_path(digest, action_event_id),
            self.tombstones_dir,
            "tombstone read",
        )
        try:
            raw = path.read_bytes()
        except FileNotFoundError as error:
            raise ChainValidationError(f"missing blob tombstone: {digest}") from error
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChainValidationError(f"invalid blob tombstone: {digest}") from error
        required = {
            "schema",
            "blob_sha256",
            "size_bytes",
            "retention_class",
            "availability",
            "hot_locator_before",
            "archive_locator",
            "archive_sha256",
            "excerpt",
            "event_provenance",
            "source_event_ids",
            "scope_keys",
            "pin_reasons",
            "plan_sha256",
            "action_event_id",
            "decided_at",
            "previous_tombstone_sha256",
            "tombstone_sha256",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ChainValidationError(f"blob tombstone fields do not match v1: {digest}")
        if value["schema"] != RETENTION_TOMBSTONE_SCHEMA or value["blob_sha256"] != digest:
            raise ChainValidationError(f"blob tombstone identity mismatch: {digest}")
        if action_event_id is not None and value["action_event_id"] != action_event_id:
            raise ChainValidationError(f"blob tombstone event identity mismatch: {digest}")
        supplied = value["tombstone_sha256"]
        unsigned = dict(value)
        unsigned.pop("tombstone_sha256")
        expected = retention_sha256(retention_canonical_bytes(unsigned))
        if supplied != expected or retention_canonical_bytes(value) != raw:
            raise ChainValidationError(f"blob tombstone digest mismatch: {digest}")
        if value["availability"] not in {"archived", "evicted"}:
            raise ChainValidationError(f"blob tombstone availability is invalid: {digest}")
        if value["retention_class"] not in _RETENTION_CLASSES:
            raise ChainValidationError(f"blob tombstone retention class is invalid: {digest}")
        if not isinstance(value["size_bytes"], int) or value["size_bytes"] < 0:
            raise ChainValidationError(f"blob tombstone size is invalid: {digest}")
        if not isinstance(value["event_provenance"], list) or not value[
            "event_provenance"
        ]:
            raise ChainValidationError(f"blob tombstone lacks event provenance: {digest}")
        return value

    def _read_tombstone_by_sha256(
        self, digest: str, tombstone_sha256: str
    ) -> dict[str, Any]:
        directory = self._retention_path(
            self._tombstone_directory(digest),
            self.tombstones_dir,
            "tombstone history lookup",
            must_exist=True,
        )
        for lexical_path in sorted(directory.glob("*.json")):
            receipt = self._read_tombstone(digest, lexical_path.stem)
            if receipt["tombstone_sha256"] == tombstone_sha256:
                return receipt
        raise ChainValidationError(
            f"canonical tombstone history lacks {tombstone_sha256} for {digest}"
        )

    def _write_tombstone(self, value: Mapping[str, Any]) -> dict[str, Any]:
        unsigned = dict(value)
        unsigned.pop("tombstone_sha256", None)
        receipt = {
            **unsigned,
            "tombstone_sha256": retention_sha256(retention_canonical_bytes(unsigned)),
        }
        path = self._retention_path(
            self._tombstone_path(
                str(receipt["blob_sha256"]), str(receipt["action_event_id"])
            ),
            self.tombstones_dir,
            "tombstone write",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = retention_canonical_bytes(receipt)
        if path.exists():
            if path.read_bytes() != encoded:
                raise ChainValidationError(f"conflicting blob tombstone at {path}")
            return receipt
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb", buffering=0) as stream:
                if stream.write(encoded) != len(encoded):
                    raise MemoryStoreError("short blob tombstone write")
                stream.flush()
                self._sync_file(stream.fileno())
            os.replace(temporary, path)
            self._sync_directory(path.parent)
            self._sync_directory(path.parent.parent)
            self._sync_directory(self.tombstones_dir)
            self._sync_directory(self.tombstones_dir.parent)
            self._sync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)
        return receipt

    @staticmethod
    def _compress_archive(content: bytes) -> bytes:
        try:
            from compression import zstd

            return zstd.compress(content, level=9)
        except ImportError:
            try:
                import zstandard
            except ImportError as error:
                raise CapabilityError(
                    "zstd support is required to archive an eligible Memory v1 blob"
                ) from error
            return zstandard.ZstdCompressor(
                level=9, write_checksum=True, write_content_size=True
            ).compress(content)

    @staticmethod
    def _decompress_archive(content: bytes) -> bytes:
        try:
            from compression import zstd

            return zstd.decompress(content)
        except ImportError:
            try:
                import zstandard
            except ImportError as error:
                raise CapabilityError(
                    "zstd support is required to read an archived Memory v1 blob"
                ) from error
            return zstandard.ZstdDecompressor().decompress(content)

    def _archive_path(self, digest: str, observed_at: str) -> Path:
        month = observed_at[:7]
        hexadecimal = digest.removeprefix("sha256:")
        return self.archive_dir / month / f"{hexadecimal}.json.zst"

    def _write_archive(self, digest: str, content: bytes, observed_at: str) -> tuple[str, str]:
        path = self._retention_path(
            self._archive_path(digest, observed_at),
            self.archive_dir,
            "archive write",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = self._compress_archive(content)
        archive_sha256 = _sha256(encoded)
        if path.exists():
            if path.read_bytes() != encoded:
                raise ChainValidationError(f"conflicting blob archive at {path}")
        else:
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb", buffering=0) as stream:
                    if stream.write(encoded) != len(encoded):
                        raise MemoryStoreError("short blob archive write")
                    stream.flush()
                    self._sync_file(stream.fileno())
                os.replace(temporary, path)
                self._sync_directory(path.parent)
                self._sync_directory(self.archive_dir)
                self._sync_directory(self.root)
            finally:
                temporary.unlink(missing_ok=True)
        if self._decompress_archive(path.read_bytes()) != content:
            raise ChainValidationError(f"blob archive verification failed: {digest}")
        return str(path.relative_to(self.root)).replace("\\", "/"), archive_sha256

    def _rehydration_stage_path(self, event_id: str) -> Path:
        _uuid_text(event_id, "rehydration event_id")
        return self.rehydration_dir / f"{event_id}.blob"

    def _write_rehydration_stage(
        self, event_id: str, digest: str, content: bytes
    ) -> str:
        path = self._retention_path(
            self._rehydration_stage_path(event_id),
            self.rehydration_dir,
            "rehydration stage write",
        )
        if len(content) < 1 or _sha256(content) != digest:
            raise ChainValidationError("rehydration stage content identity mismatch")
        encoded_locator = str(path.relative_to(self.root)).replace("\\", "/")
        if path.exists():
            if path.read_bytes() != content:
                raise ChainValidationError(f"conflicting rehydration stage at {path}")
            return encoded_locator
        temporary = self._retention_path(
            self.rehydration_dir / f"{event_id}.{uuid.uuid4().hex}.tmp",
            self.rehydration_dir,
            "rehydration temporary write",
        )
        try:
            with temporary.open("xb", buffering=0) as stream:
                if stream.write(content) != len(content):
                    raise MemoryStoreError("short rehydration stage write")
                stream.flush()
                self._sync_file(stream.fileno())
            os.replace(temporary, path)
            self._sync_directory(self.rehydration_dir)
            self._sync_directory(self.rehydration_dir.parent)
            self._sync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)
        return encoded_locator

    def _unlink_rehydration_stage(self, event_id: str) -> None:
        path = self._retention_path(
            self._rehydration_stage_path(event_id),
            self.rehydration_dir,
            "rehydration stage unlink",
        )
        path.unlink(missing_ok=True)
        self._sync_directory(self.rehydration_dir)

    def _promote_rehydration_stage(
        self, event_id: str, digest: str, size_bytes: int
    ) -> str:
        stage = self._retention_path(
            self._rehydration_stage_path(event_id),
            self.rehydration_dir,
            "rehydration stage promotion",
        )
        hot = self._retention_path(
            self._blob_path(digest), self.blobs_dir, "rehydrated hot promotion"
        )
        hot.parent.mkdir(parents=True, exist_ok=True)
        if hot.exists():
            content = hot.read_bytes()
            if len(content) != size_bytes or _sha256(content) != digest:
                raise ChainValidationError(f"rehydrated hot blob integrity failure: {digest}")
            if stage.exists():
                staged = stage.read_bytes()
                if staged != content:
                    raise ChainValidationError(
                        f"rehydration stage conflicts with hot blob: {digest}"
                    )
                stage.unlink()
                self._sync_directory(self.rehydration_dir)
        else:
            try:
                content = stage.read_bytes()
            except FileNotFoundError as error:
                raise ChainValidationError(
                    f"canonical rehydration event lacks staged bytes: {event_id}"
                ) from error
            if len(content) != size_bytes or _sha256(content) != digest:
                raise ChainValidationError(f"rehydration stage integrity failure: {event_id}")
            os.replace(stage, hot)
            self._sync_directory(hot.parent)
            self._sync_directory(hot.parent.parent)
            self._sync_directory(self.blobs_dir.parent)
            self._sync_directory(self.rehydration_dir)
            self._sync_directory(self.root)
        return str(hot.relative_to(self.root)).replace("\\", "/")

    def _read_archived_blob(self, receipt: Mapping[str, Any]) -> bytes:
        locator = receipt.get("archive_locator")
        archive_sha256 = receipt.get("archive_sha256")
        if not isinstance(locator, str) or not locator or not isinstance(archive_sha256, str):
            raise ChainValidationError("archived blob tombstone lacks archive provenance")
        path = self._retention_path(
            self.root / locator,
            self.archive_dir,
            "archive read",
        )
        try:
            encoded = path.read_bytes()
        except FileNotFoundError as error:
            raise ChainValidationError(f"missing blob archive: {receipt['blob_sha256']}") from error
        if _sha256(encoded) != archive_sha256:
            raise ChainValidationError(f"blob archive digest mismatch: {receipt['blob_sha256']}")
        content = self._decompress_archive(encoded)
        if (
            len(content) != receipt["size_bytes"]
            or _sha256(content) != receipt["blob_sha256"]
        ):
            raise ChainValidationError(f"blob archive payload mismatch: {receipt['blob_sha256']}")
        return content

    def _write_blob(self, digest: str, content: bytes) -> None:
        path = self._retention_path(
            self._blob_path(digest), self.blobs_dir, "blob write"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise ChainValidationError(f"content-address collision at {path}")
            return
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb", buffering=0) as stream:
                if stream.write(content) != len(content):
                    raise MemoryStoreError("short blob write")
                stream.flush()
                self._sync_file(stream.fileno())
            os.replace(temporary, path)
            self._sync_directory(path.parent)
            self._sync_directory(path.parent.parent)
            self._sync_directory(self.blobs_dir.parent)
            self._sync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_blob(self, digest: str, size_bytes: int) -> bytes:
        path = self._retention_path(
            self._blob_path(digest), self.blobs_dir, "blob read"
        )
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            receipt = self._read_tombstone(digest)
            if receipt["size_bytes"] != size_bytes:
                raise ChainValidationError(f"blob tombstone byte count mismatch: {digest}")
            if receipt["availability"] == "archived":
                return self._read_archived_blob(receipt)
            raise BlobEvictedError(f"blob was evicted with a verified tombstone: {digest}")
        if len(content) != size_bytes or _sha256(content) != digest:
            raise ChainValidationError(f"blob integrity failure: {digest}")
        return content

    def _event_id_exists(self, event_id: str) -> bool:
        with closing(self._connect()) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM event_index WHERE event_id = ?", (event_id,)
                ).fetchone()
                is not None
            )

    def _record_hash_indexed(self, record_hash: str) -> bool:
        with closing(self._connect()) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM event_index WHERE record_sha256 = ?", (record_hash,)
                ).fetchone()
                is not None
            )

    def _segment_path(self, host_id: str, session_id: str) -> Path:
        return self._retention_path(
            self.events_dir / host_id / f"{session_id}.jsonl",
            self.events_dir,
            "event segment path",
        )

    @staticmethod
    def _validate_host_id(host_id: str) -> str:
        if not isinstance(host_id, str) or not _HOST_RE.fullmatch(host_id):
            raise MemoryStoreError("host_id must be a safe 1-128 character identifier")
        return host_id

    @staticmethod
    def _validate_actor(actor: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(actor, Mapping):
            raise MemoryStoreError("actor must be a mapping")
        unknown = set(actor) - {"kind", "id", "model"}
        if unknown:
            raise MemoryStoreError(f"unknown actor fields: {sorted(unknown)}")
        if actor.get("kind") not in _ACTOR_KINDS:
            raise MemoryStoreError("invalid actor kind")
        if not isinstance(actor.get("id"), str) or not actor["id"]:
            raise MemoryStoreError("actor id must be non-empty text")
        if actor.get("model") is not None and not isinstance(actor["model"], str):
            raise MemoryStoreError("actor model must be text or null")
        return {"kind": actor["kind"], "id": actor["id"], "model": actor.get("model")}

    @staticmethod
    def _word_query(query: str) -> str:
        tokens = re.findall(r"[^\W_]+(?:[_./:#@+\\-][^\W_]+)*", query, flags=re.UNICODE)
        unique = list(dict.fromkeys(token for token in tokens if token))
        return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in unique)

    @staticmethod
    def _search_filter_sql(
        scope_keys: Sequence[str], kinds: Sequence[str], as_of: str | None
    ) -> tuple[str, list[Any]]:
        scope_placeholders = ",".join("?" for _ in scope_keys)
        clauses = [f"m.scope_key IN ({scope_placeholders})"]
        parameters: list[Any] = list(scope_keys)
        if as_of is None:
            clauses.append("m.status IN ('active', 'disputed')")
        else:
            clauses.append("(m.valid_from IS NULL OR m.valid_from <= ?)")
            clauses.append("(m.valid_to IS NULL OR m.valid_to > ?)")
            parameters.extend([as_of, as_of])
        if kinds:
            kind_placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"m.kind IN ({kind_placeholders})")
            parameters.extend(kinds)
        return " AND ".join(clauses), parameters

    def _search_result(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        ranks: Mapping[str, int],
        score: Fraction,
        query: str,
    ) -> dict[str, Any]:
        value = self._memory_result(connection, row)
        exact_matches: list[str] = []
        if "exact" in ranks:
            # Expose the structured fields responsible for exact admission.
            folded = query.casefold()
            exact_matches = [
                field
                for field in ("memory_id", "subject", "predicate", "searchable_text")
                if str(row[field]).casefold() == folded
            ]
        evidence = value["evidence"]
        value["retrieval"] = {
            "score": round(float(score), 12),
            "channels": [
                {"channel": channel, "rank": ranks[channel]}
                for channel in ("exact", "word", "trigram")
                if channel in ranks
            ],
            "exact_matches": exact_matches,
            "scope": {
                key: row[key]
                for key in ("owner_id", "workspace_id", "task_id", "agent_role", "session_id")
            },
            "temporal_status": row["status"],
            "evidence_authorities": sorted({item["authority"] for item in evidence}),
            "source_event_ids": sorted(
                {
                    item["source_event_id"]
                    for item in evidence
                    if item["source_event_id"] is not None
                }
                | {row["source_event_id"]}
            ),
        }
        return value

    @staticmethod
    def _memory_result(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["object"] = json.loads(value.pop("object_json"))
        value["evidence"] = [
            dict(item)
            for item in connection.execute(
                "SELECT * FROM evidence WHERE memory_id = ? ORDER BY evidence_id",
                (row["memory_id"],),
            ).fetchall()
        ]
        return value

    @staticmethod
    def _timestamp_sort_value(value: str) -> float:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
