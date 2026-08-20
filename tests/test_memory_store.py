from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from agent_continuity.memory_store import (  # noqa: E402
    TASK_STATE_SCHEMA,
    ChainValidationError,
    MemoryScope,
    MemoryStore,
    ProjectionError,
    StoreLockTimeout,
)

HOST = "excalibur"
OWNER = "local-owner"
ACTOR = {"kind": "user", "id": "operator"}
MAY = "2026-05-01T00:00:00Z"
JUNE = "2026-06-01T00:00:00Z"

_CONCURRENT_APPEND_SCRIPT = r"""
import sys
import time
from pathlib import Path

from agent_continuity.memory_store import MemoryScope, MemoryStore

root, session_id, task_id, worker, raw_count, ready_name, gate_name = sys.argv[1:]
store = MemoryStore(root, lock_timeout=10)
ready = Path(root) / ready_name
gate = Path(root) / gate_name
ready.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 10
while not gate.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("concurrent test gate did not open")
    time.sleep(0.005)
for index in range(int(raw_count)):
    memory_id = f"{worker}-{index}"
    text = f"concurrent worker {worker} item {index}"
    store.append(
        host_id="excalibur",
        session_id=session_id,
        task_id=task_id,
        event_type="memory/committed",
        actor={"kind": "agent", "id": worker},
        scope=MemoryScope("local-owner", "workspace-a", task_id, None, session_id),
        payload={
            "memory_id": memory_id,
            "kind": "fact",
            "subject": memory_id,
            "predicate": "recorded",
            "object": {"text": text},
            "searchable_text": text,
            "verification_status": "tested",
            "evidence": [{
                "source_kind": "test",
                "source_locator": worker,
                "authority": "concurrent-process-test",
                "excerpt": text,
            }],
        },
    )
"""

_LOCK_HOLDER_SCRIPT = r"""
import sys
import time
from pathlib import Path

from agent_continuity.memory_store import MemoryStore

root, ready_name = sys.argv[1:]
store = MemoryStore(root, lock_timeout=10)
with store._exclusive_store_lock():
    (Path(root) / ready_name).write_text("locked", encoding="utf-8")
    time.sleep(60)
"""


def sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = MemoryStore(self.root, blob_threshold=256)
        self.session_id = str(uuid.uuid4())
        self.task_id = str(uuid.uuid4())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def subprocess_environment() -> dict[str, str]:
        environment = os.environ.copy()
        source = str(PACKAGE / "src")
        current = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = source if not current else source + os.pathsep + current
        return environment

    def wait_for_file(self, path: Path, processes: list[subprocess.Popen[str]]) -> None:
        deadline = time.monotonic() + 10
        while not path.exists():
            failures = [process for process in processes if process.poll() is not None]
            if failures:
                output = [process.communicate() for process in failures]
                self.fail(f"helper process exited before readiness: {output}")
            if time.monotonic() >= deadline:
                self.fail(f"timed out waiting for helper readiness: {path}")
            time.sleep(0.01)

    def append_generic(self, payload: dict[str, object]) -> dict[str, object]:
        return self.store.append(
            host_id=HOST,
            session_id=self.session_id,
            task_id=self.task_id,
            event_type="tool/result",
            actor={"kind": "tool", "id": "pytest"},
            scope=MemoryScope(OWNER, "workspace-a", self.task_id, None, self.session_id),
            payload=payload,
        )

    def append_memory(
        self,
        memory_id: str,
        text: str,
        *,
        scope: MemoryScope,
        occurred_at: str = MAY,
        valid_from: str | None = MAY,
        supersedes_id: str | None = None,
        verification_status: str = "tested",
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "memory_id": memory_id,
            "kind": "fact",
            "subject": memory_id,
            "predicate": "uses",
            "object": {"text": text},
            "searchable_text": text,
            "valid_from": valid_from,
            "verification_status": verification_status,
            "evidence": [
                {
                    "source_kind": "test",
                    "source_locator": "tests/test_memory_store.py",
                    "source_sha256": sha256_text(text),
                    "authority": "authoritative-test",
                    "excerpt": text,
                }
            ],
        }
        if supersedes_id is not None:
            payload["supersedes_id"] = supersedes_id
        return self.store.append(
            host_id=HOST,
            session_id=self.session_id,
            task_id=self.task_id,
            event_type="memory/committed",
            actor=ACTOR,
            scope=scope,
            payload=payload,
            occurred_at=occurred_at,
        )

    def test_append_writes_canonical_hash_chain_and_required_pragmas(self) -> None:
        first = self.append_generic({"result": "one"})
        second = self.append_generic({"result": "two"})

        self.assertEqual(first["seq"], 1)
        self.assertEqual(second["seq"], 2)
        self.assertEqual(second["previous_record_sha256"], first["record_sha256"])
        report = self.store.verify()
        self.assertEqual(report.event_count, 2)
        segment = self.root / "events" / HOST / f"{self.session_id}.jsonl"
        for line in segment.read_bytes().splitlines(keepends=True):
            parsed = json.loads(line)
            canonical = json.dumps(
                parsed,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            self.assertEqual(line, canonical + b"\n")

        connection = self.store._connect()
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA trusted_schema").fetchone()[0], 0)
        finally:
            connection.close()

    def test_simultaneous_fresh_process_appends_preserve_chain_and_projection(self) -> None:
        count_per_worker = 8
        gate_name = "concurrent-start.gate"
        processes: list[subprocess.Popen[str]] = []
        for worker in ("worker-a", "worker-b"):
            ready_name = f"{worker}.ready"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _CONCURRENT_APPEND_SCRIPT,
                    str(self.root),
                    self.session_id,
                    self.task_id,
                    worker,
                    str(count_per_worker),
                    ready_name,
                    gate_name,
                ],
                cwd=PACKAGE,
                env=self.subprocess_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(process)

        outputs: list[tuple[str, str]] = []
        try:
            for worker in ("worker-a", "worker-b"):
                self.wait_for_file(self.root / f"{worker}.ready", processes)
            (self.root / gate_name).write_text("go", encoding="utf-8")
            for process in processes:
                outputs.append(process.communicate(timeout=30))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)

        for process, (stdout, stderr) in zip(processes, outputs, strict=True):
            self.assertEqual(process.returncode, 0, f"stdout={stdout}\nstderr={stderr}")

        expected = count_per_worker * len(processes)
        fresh = MemoryStore(self.root, blob_threshold=256)
        report = fresh.verify()
        self.assertEqual(report.event_count, expected)
        segment = self.root / "events" / HOST / f"{self.session_id}.jsonl"
        records = [json.loads(line) for line in segment.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["seq"] for record in records], list(range(1, expected + 1)))
        self.assertEqual(len({record["event_id"] for record in records}), expected)
        self.assertEqual(len({record["record_sha256"] for record in records}), expected)

        connection = fresh._connect()
        try:
            projection = connection.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT event_id),
                       COUNT(DISTINCT record_sha256), MIN(seq), MAX(seq)
                FROM event_index
                """
            ).fetchone()
            self.assertEqual(tuple(projection), (expected, expected, expected, 1, expected))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM memory_item").fetchone()[0],
                expected,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
                expected,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM memory_fts_unicode").fetchone()[0],
                expected,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM memory_fts_trigram").fetchone()[0],
                expected,
            )
        finally:
            connection.close()

    def test_store_lock_times_out_and_is_released_when_holder_process_dies(self) -> None:
        ready_name = "lock-holder.ready"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _LOCK_HOLDER_SCRIPT,
                str(self.root),
                ready_name,
            ],
            cwd=PACKAGE,
            env=self.subprocess_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.wait_for_file(self.root / ready_name, [holder])
            started = time.monotonic()
            with self.assertRaisesRegex(StoreLockTimeout, "timed out.*store lock"):
                MemoryStore(self.root, lock_timeout=0.15)
            self.assertLess(time.monotonic() - started, 2.0)

            holder.kill()
            holder.communicate(timeout=10)
            recovered = MemoryStore(self.root, lock_timeout=2)
            self.assertEqual(recovered.verify().event_count, 0)
            self.assertTrue(recovered.lock_path.exists())
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.communicate(timeout=10)

    def test_replay_rebuilds_deleted_projection_from_raw_truth(self) -> None:
        scope = MemoryScope(OWNER, "workspace-a", None, None, None)
        self.append_memory("memory-path", r"Use D:\work\src\owner.ps1", scope=scope)
        original_log = next((self.root / "events").glob("*/*.jsonl")).read_bytes()

        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.root / 'memory.sqlite3'}{suffix}").unlink(missing_ok=True)
        rebuilt = MemoryStore(self.root, blob_threshold=256)

        self.assertEqual(rebuilt.replay().applied_events, 0)
        self.assertEqual(rebuilt.verify().event_count, 1)
        self.assertEqual(rebuilt.get("memory-path")["object"]["text"], r"Use D:\work\src\owner.ps1")
        self.assertEqual(next((self.root / "events").glob("*/*.jsonl")).read_bytes(), original_log)

    def test_rebuild_on_open_repairs_projection_rows_absent_from_raw_truth(self) -> None:
        self.append_generic({"result": "canonical"})
        phantom_id = str(uuid.uuid4())
        connection = self.store._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO event_index (
                      event_id, host_id, session_id, seq, event_type,
                      occurred_at, segment_path, byte_offset, payload_sha256,
                      record_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        phantom_id,
                        "phantom",
                        str(uuid.uuid4()),
                        1,
                        "test/phantom",
                        MAY,
                        "phantom.jsonl",
                        0,
                        "sha256:" + "1" * 64,
                        "sha256:" + "2" * 64,
                    ),
                )
        finally:
            connection.close()

        with self.assertRaisesRegex(ProjectionError, "absent from canonical logs"):
            MemoryStore(self.root, blob_threshold=256)

        rebuilt = MemoryStore(self.root, blob_threshold=256, rebuild_on_open=True)
        self.assertEqual(rebuilt.verify().event_count, 1)
        connection = rebuilt._connect()
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT event_id FROM event_index WHERE event_id = ?", (phantom_id,)
                ).fetchone()
            )
        finally:
            connection.close()

    def test_large_payload_is_content_addressed_and_tamper_is_rejected(self) -> None:
        with (
            mock.patch("agent_continuity.memory_store.os.fsync", wraps=os.fsync) as fsync,
            mock.patch("agent_continuity.memory_store.os.replace", wraps=os.replace) as replace,
        ):
            record = self.append_generic({"output": "x" * 2_000})
        self.assertGreaterEqual(fsync.call_count, 2)
        replace.assert_called_once()
        reference = record["payload"]["$blob"]
        self.assertEqual(self.store.verify().blob_count, 1)
        digest = reference["sha256"].removeprefix("sha256:")
        blob = self.root / "blobs" / "sha256" / digest[:2] / digest
        self.assertEqual(blob.stat().st_size, reference["size_bytes"])
        self.assertEqual(list(blob.parent.glob("*.tmp")), [])

        blob.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ChainValidationError, "blob integrity failure"):
            self.store.verify()

    def test_broken_record_hash_is_rejected(self) -> None:
        self.append_generic({"result": "before"})
        segment = next((self.root / "events").glob("*/*.jsonl"))
        record = json.loads(segment.read_text(encoding="utf-8"))
        record["payload"]["result"] = "after"
        segment.write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
            newline="",
        )
        with self.assertRaisesRegex(ChainValidationError, "record hash mismatch"):
            self.store.verify()

    def test_search_is_exactly_scoped_and_explains_independent_channels(self) -> None:
        allowed = MemoryScope(OWNER, "workspace-a", None, None, None)
        forbidden = MemoryScope(OWNER, "workspace-b", None, None, None)
        self.append_memory(
            "allowed",
            "The listenerPID must equal ownerPID before tunnel reuse.",
            scope=allowed,
        )
        self.append_memory(
            "forbidden",
            "The listenerPID belongs to a different workspace.",
            scope=forbidden,
        )

        first = self.store.search("listenerPID", [allowed])
        second = self.store.search("listenerPID", [allowed])
        self.assertEqual(first, second)
        self.assertEqual([item["memory_id"] for item in first], ["allowed"])
        channels = [item["channel"] for item in first[0]["retrieval"]["channels"]]
        self.assertIn("word", channels)
        self.assertIn("trigram", channels)
        self.assertEqual(first[0]["retrieval"]["scope"]["workspace_id"], "workspace-a")
        self.assertEqual(
            first[0]["retrieval"]["evidence_authorities"], ["authoritative-test"]
        )
        self.assertNotIn("forbidden", json.dumps(first))

        with self.assertRaisesRegex(ProjectionError, "different exact scope"):
            self.store.append(
                host_id=HOST,
                session_id=self.session_id,
                task_id=self.task_id,
                event_type="memory/retired",
                actor=ACTOR,
                scope=forbidden,
                payload={"memory_id": "allowed"},
                occurred_at=JUNE,
            )

    def test_supersession_preserves_current_and_historical_truth(self) -> None:
        scope = MemoryScope(OWNER, "workspace-a", None, None, None)
        self.append_memory(
            "launch-may",
            "Launch time is 7 PM",
            scope=scope,
            occurred_at=MAY,
            valid_from=MAY,
        )
        self.append_memory(
            "launch-june",
            "Launch time is 9 PM",
            scope=scope,
            occurred_at=JUNE,
            valid_from=JUNE,
            supersedes_id="launch-may",
        )

        current = self.store.search("Launch time", [scope])
        historical = self.store.search("Launch time", [scope], as_of="2026-05-15T00:00:00Z")
        self.assertEqual([item["memory_id"] for item in current], ["launch-june"])
        self.assertEqual([item["memory_id"] for item in historical], ["launch-may"])
        self.assertEqual(self.store.get("launch-may")["status"], "superseded")

    def test_invalid_projection_is_rejected_before_canonical_append(self) -> None:
        scope = MemoryScope(OWNER, "workspace-a", None, None, None)
        self.append_memory("first", "same canonical fact", scope=scope)
        segment = next((self.root / "events").glob("*/*.jsonl"))
        before = segment.read_bytes()

        with self.assertRaises(ProjectionError):
            self.append_memory("first", "same canonical fact", scope=scope)

        self.assertEqual(segment.read_bytes(), before)
        self.assertEqual(self.store.verify().event_count, 1)

    def test_task_state_projection_enforces_revision_chain(self) -> None:
        first_event = str(uuid.uuid4())
        state = {
            "schema": TASK_STATE_SCHEMA,
            "task_id": self.task_id,
            "objective": {"text": "Build Memory v1", "source_event_id": first_event},
            "success_criteria": [],
            "constraints": [],
            "decisions": [],
            "completed": [],
            "current_action": {"text": "Implement", "owner": "agent", "started_at": MAY},
            "next_actions": [],
            "blockers": [],
            "open_tool_calls": [],
            "artifacts": [],
            "disputes": [],
            "through_event_id": first_event,
            "previous_state_sha256": None,
        }
        self.store.append(
            host_id=HOST,
            session_id=self.session_id,
            task_id=self.task_id,
            event_id=first_event,
            event_type="state/committed",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", self.task_id, None, self.session_id),
            payload={"revision": 1, "state": state},
            occurred_at=MAY,
        )

        connection = sqlite3.connect(self.root / "memory.sqlite3")
        try:
            revision, state_hash = connection.execute(
                "SELECT revision, state_sha256 FROM task_state WHERE task_id = ?",
                (self.task_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(revision, 1)

        second_event = str(uuid.uuid4())
        state["through_event_id"] = second_event
        state["previous_state_sha256"] = state_hash
        self.store.append(
            host_id=HOST,
            session_id=self.session_id,
            task_id=self.task_id,
            event_id=second_event,
            event_type="state/committed",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", self.task_id, None, self.session_id),
            payload={"revision": 2, "state": state},
            occurred_at=JUNE,
        )
        self.assertEqual(self.store.replay(rebuild=True).verified_events, 2)


if __name__ == "__main__":
    unittest.main()
