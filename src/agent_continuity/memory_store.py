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
import sys
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
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

EVENT_SCHEMA = "coding-intelligence-memory-event/v1"
BLOB_THRESHOLD_BYTES = 16 * 1024
DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
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
    ):
        self.root = Path(root).resolve()
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
        self.blob_threshold = blob_threshold
        self.lock_timeout = float(lock_timeout)
        self.events_dir = self.root / "events"
        self.blobs_dir = self.root / "blobs" / "sha256"
        self.database_path = self.root / "memory.sqlite3"
        self.lock_path = self.root / ".memory-v1.lock"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = _process_lock(self.lock_path)
        with self._exclusive_store_lock():
            with closing(self._connect()) as connection:
                self._probe_capabilities(connection)
                self._create_schema(connection)
        self.replay(rebuild=rebuild_on_open)

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
    ) -> dict[str, Any]:
        """Append, fsync, then transactionally project one canonical event.

        Known reducer payloads:

        * ``memory/committed``: memory fields plus a non-empty ``evidence`` list.
        * ``memory/superseded|retired|disputed``: ``memory_id`` and optional
          ``valid_to``.
        * ``state/committed``: ``revision`` and ``state``.

        Other event families remain queryable through ``event_index``.
        """

        with self._exclusive_store_lock():
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
                blob = self._blob_reference(record["payload"])
                if blob is not None:
                    self._read_blob(blob["sha256"], blob["size_bytes"])
                    blobs.add(blob["sha256"])
                event_count += 1
            if rows:
                last_hashes[f"{path.parent.name}/{path.stem}"] = rows[-1][1][
                    "record_sha256"
                ]
        return VerificationReport(len(paths), event_count, len(blobs), last_hashes)

    def replay(self, *, rebuild: bool = False) -> ReplayReport:
        """Verify raw truth and apply missing events, or rebuild every projection row."""

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
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
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
              pin_reasons_json TEXT NOT NULL CHECK(json_valid(pin_reasons_json))
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

    def _clear_projection(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM evidence")
        connection.execute("DELETE FROM task_state")
        connection.execute("DELETE FROM memory_item")
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
    ) -> None:
        self._index_event(connection, record, path, offset)
        self._apply_projection(connection, record, resolved_payload)

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
    ) -> None:
        payload = (
            resolved_payload
            if resolved_payload is not None
            else self._resolve_payload(record)
        )
        event_type = record["type"]
        if event_type == "memory/committed":
            self._apply_memory_commit(connection, record, payload)
        elif event_type in {"memory/superseded", "memory/retired", "memory/disputed"}:
            self._apply_memory_status(connection, record, payload)
        elif event_type == "state/committed":
            self._apply_task_state(connection, record, payload)
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
                  reference_count = reference_count + 1
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

    def _projection_order(
        self, events: Sequence[tuple[Path, int, dict[str, Any]]]
    ) -> list[tuple[Path, int, dict[str, Any]]]:
        """Order cross-segment reducers without weakening per-segment chain truth."""

        neutral: list[tuple[Path, int, dict[str, Any]]] = []
        commits: dict[str, tuple[Path, int, dict[str, Any]]] = {}
        statuses: list[tuple[Path, int, dict[str, Any]]] = []
        states: list[tuple[Path, int, dict[str, Any]]] = []
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
        return [*neutral, *ordered_commits, *statuses, *states]

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
                self._apply_event(connection, record, path, offset, resolved_payload)
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
                "SELECT scope_key, status FROM memory_item WHERE memory_id = ?", (supersedes,)
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
            "SELECT scope_key FROM memory_item WHERE memory_id = ?", (payload["memory_id"],)
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
        _canonical_bytes(payload)
        return payload

    def _read_segment(self, path: Path) -> list[tuple[int, dict[str, Any]]]:
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
                payload = self._payload_bytes(record["payload"])
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

    def _write_blob(self, digest: str, content: bytes) -> None:
        path = self._blob_path(digest)
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
        path = self._blob_path(digest)
        try:
            content = path.read_bytes()
        except FileNotFoundError as error:
            raise ChainValidationError(f"missing blob: {digest}") from error
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
        return self.events_dir / host_id / f"{session_id}.jsonl"

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
