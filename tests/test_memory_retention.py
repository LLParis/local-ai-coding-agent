from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from agent_continuity.memory_store import (  # noqa: E402
    BlobEvictedError,
    CapabilityError,
    MemoryScope,
    MemoryStore,
    MemoryStoreError,
    ProjectionError,
    RetentionPressureError,
)

HOST = "retention-test"
OWNER = "operator-a"
ACTOR = {"kind": "system", "id": "retention-test"}
JANUARY = "2026-01-01T00:00:00Z"
TERMINAL = "2026-01-02T00:00:00Z"
AFTER_SCRATCH_TTL = "2026-01-03T00:00:01Z"
AFTER_BULK_TTL = "2026-01-16T00:00:01Z"


class MemoryRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = MemoryStore(self.root, blob_threshold=256)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def identity() -> tuple[str, str]:
        return str(uuid.uuid4()), str(uuid.uuid4())

    def append_blob(
        self,
        *,
        task_id: str,
        session_id: str,
        retention_class: str,
        marker: str,
        workspace: str = "workspace-a",
        event_type: str = "tool/result",
        size: int = 2_048,
    ) -> dict[str, object]:
        return self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type=event_type,
            actor=ACTOR,
            scope=MemoryScope(OWNER, workspace, task_id, None, None),
            payload={"marker": marker, "content": marker * size},
            occurred_at=JANUARY,
            retention_class=retention_class,
        )

    def terminal(
        self,
        *,
        task_id: str,
        session_id: str,
        workspace: str = "workspace-a",
    ) -> dict[str, object]:
        return self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type="task/completed",
            actor=ACTOR,
            scope=MemoryScope(OWNER, workspace, task_id, None, None),
            payload={"status": "verified"},
            occurred_at=TERMINAL,
        )

    def catalog(self, digest: str) -> dict[str, object]:
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM blob_catalog WHERE blob_sha256 = ?", (digest,)
            ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def test_scratch_dry_run_precedes_exact_eviction_and_preserves_canonical_event(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="scratch",
            marker="scratch-one",
        )
        self.terminal(task_id=task_id, session_id=session_id)
        digest = source["payload"]["$blob"]["sha256"]
        segment = self.root / "events" / HOST / f"{session_id}.jsonl"
        canonical_before = segment.read_bytes()
        hot_path = self.store._blob_path(digest)

        first = self.store.plan_retention(now=AFTER_SCRATCH_TTL)
        second = self.store.plan_retention(now=AFTER_SCRATCH_TTL)
        self.assertEqual(first.as_dict(), second.as_dict())
        target = next(item for item in first.decisions if item.blob_sha256 == digest)
        self.assertEqual((target.action, target.reason), (
            "evict",
            "terminal_unreferenced_scratch_after_24h",
        ))
        dry_run = self.store.run_gc(first)
        self.assertEqual(dry_run["status"], "dry_run")
        self.assertTrue(hot_path.is_file())
        self.assertEqual(segment.read_bytes(), canonical_before)

        applied = self.store.run_gc(first, apply=True)
        self.assertEqual(applied["actions_applied"], 1)
        self.assertFalse(hot_path.exists())
        self.assertEqual(segment.read_bytes(), canonical_before)
        receipt = self.store._read_tombstone(digest)
        self.assertEqual(receipt["availability"], "evicted")
        self.assertEqual(receipt["size_bytes"], target.size_bytes)
        self.assertEqual(receipt["source_event_ids"], [source["event_id"]])
        self.assertIn("segment_path", receipt["event_provenance"][0])
        self.assertIn("head", receipt["excerpt"])
        self.assertIn("tail", receipt["excerpt"])
        self.assertEqual(self.store.verify().event_count, 3)
        self.assertEqual(self.store.replay(rebuild=True).verified_events, 3)

    def test_hot_ttl_boundaries_are_eligible_at_exact_expiry_not_before(self) -> None:
        scratch_task, scratch_session = self.identity()
        scratch = self.append_blob(
            task_id=scratch_task,
            session_id=scratch_session,
            retention_class="scratch",
            marker="scratch-boundary",
        )
        self.terminal(task_id=scratch_task, session_id=scratch_session)
        bulk_task, bulk_session = self.identity()
        bulk = self.append_blob(
            task_id=bulk_task,
            session_id=bulk_session,
            retention_class="bulk",
            marker="bulk-boundary",
        )
        self.terminal(task_id=bulk_task, session_id=bulk_session)
        scratch_digest = scratch["payload"]["$blob"]["sha256"]
        bulk_digest = bulk["payload"]["$blob"]["sha256"]

        before_scratch = self.store.plan_retention(now="2026-01-02T23:59:59Z")
        at_scratch = self.store.plan_retention(now="2026-01-03T00:00:00Z")
        self.assertEqual(
            next(
                item
                for item in before_scratch.decisions
                if item.blob_sha256 == scratch_digest
            ).action,
            "keep",
        )
        self.assertEqual(
            next(
                item
                for item in at_scratch.decisions
                if item.blob_sha256 == scratch_digest
            ).action,
            "evict",
        )
        before_bulk = self.store.plan_retention(now="2026-01-15T23:59:59Z")
        at_bulk = self.store.plan_retention(now="2026-01-16T00:00:00Z")
        self.assertEqual(
            next(
                item
                for item in before_bulk.decisions
                if item.blob_sha256 == bulk_digest
            ).action,
            "keep",
        )
        self.assertEqual(
            next(
                item
                for item in at_bulk.decisions
                if item.blob_sha256 == bulk_digest
            ).action,
            "evict",
        )

    def test_cited_bulk_archives_but_uncited_bulk_evicts(self) -> None:
        cited_task, cited_session = self.identity()
        cited = self.append_blob(
            task_id=cited_task,
            session_id=cited_session,
            retention_class="bulk",
            marker="cited-bulk",
        )
        self.store.append(
            host_id=HOST,
            session_id=cited_session,
            task_id=cited_task,
            event_type="retention/cited",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", cited_task, None, None),
            payload={"source_event_ids": [cited["event_id"]]},
            occurred_at="2026-01-01T01:00:00Z",
        )
        self.terminal(task_id=cited_task, session_id=cited_session)

        uncited_task, uncited_session = self.identity()
        uncited = self.append_blob(
            task_id=uncited_task,
            session_id=uncited_session,
            retention_class="bulk",
            marker="uncited-bulk",
        )
        self.terminal(task_id=uncited_task, session_id=uncited_session)
        cited_digest = cited["payload"]["$blob"]["sha256"]
        uncited_digest = uncited["payload"]["$blob"]["sha256"]

        plan = self.store.plan_retention(now=AFTER_BULK_TTL)
        actions = {item.blob_sha256: item.action for item in plan.decisions}
        self.assertEqual(actions[cited_digest], "archive")
        self.assertEqual(actions[uncited_digest], "evict")
        result = self.store.run_gc(plan, apply=True)
        self.assertEqual(result["actions_applied"], 2)
        cited_row = self.catalog(cited_digest)
        uncited_row = self.catalog(uncited_digest)
        self.assertEqual(cited_row["availability"], "archived")
        self.assertEqual(uncited_row["availability"], "evicted")
        archive = self.root / str(cited_row["archive_locator"])
        self.assertTrue(archive.is_file())
        self.assertFalse(self.store._blob_path(cited_digest).exists())
        self.assertFalse(self.store._blob_path(uncited_digest).exists())
        # The two canonical blob-retention events may themselves be content-addressed
        # at this deliberately tiny test threshold; both source blobs remain verifiable.
        self.assertGreaterEqual(self.store.verify().blob_count, 2)
        rebuilt = MemoryStore(self.root, blob_threshold=256, rebuild_on_open=True)
        self.assertEqual(rebuilt.verify().blob_count, self.store.verify().blob_count)

        archive_expiry_plan = self.store.plan_retention(now="2026-04-17T00:00:00Z")
        archived_bulk = next(
            item
            for item in archive_expiry_plan.decisions
            if item.blob_sha256 == cited_digest
        )
        self.assertEqual(
            (archived_bulk.action, archived_bulk.reason),
            ("evict", "cited_bulk_archive_expired_after_90d"),
        )
        self.store.run_gc(archive_expiry_plan, apply=True)
        self.assertEqual(self.catalog(cited_digest)["availability"], "evicted")
        self.assertFalse(archive.exists())
        receipts = sorted(self.store._tombstone_directory(cited_digest).glob("*.json"))
        self.assertEqual(len(receipts), 2)
        latest = self.store._read_tombstone(cited_digest)
        self.assertEqual(latest["availability"], "evicted")
        self.assertIsNotNone(latest["previous_tombstone_sha256"])
        self.assertEqual(self.store.replay(rebuild=True).verified_events, 8)

    def test_active_memory_evidence_and_production_claim_are_never_removed(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="evidence",
            marker="protected-source",
            event_type="tool/result",
        )
        source_digest = source["payload"]["$blob"]["sha256"]
        memory_event_id = str(uuid.uuid4())
        memory_record = self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_id=memory_event_id,
            event_type="memory/committed",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
            retention_class="evidence",
            payload={
                "memory_id": "protected-active-memory",
                "kind": "fact",
                "subject": "protected source",
                "predicate": "status",
                "object": "pinned",
                "searchable_text": "protected source remains pinned",
                "verification_status": "tested",
                "evidence": [
                    {
                        "source_kind": "event",
                        "source_locator": "retention-test://protected-source",
                        "source_event_id": source["event_id"],
                        "authority": "retention-test",
                    }
                ],
                "padding": "m" * 1_500,
            },
            occurred_at="2026-01-01T00:30:00Z",
        )
        self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type="production/claim",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
            payload={"source_event_ids": [source["event_id"]], "status": "production"},
            occurred_at="2026-01-01T00:45:00Z",
        )
        self.terminal(task_id=task_id, session_id=session_id)

        plan = self.store.plan_retention(now="2027-01-01T00:00:00Z")
        target = next(item for item in plan.decisions if item.blob_sha256 == source_digest)
        self.assertEqual(target.action, "keep")
        self.assertIn("evidence:", " ".join(target.pin_reasons))
        self.assertIn("production_claim:", " ".join(target.pin_reasons))
        self.assertTrue(self.store._blob_path(source_digest).is_file())
        self.assertNotIn(source_digest, [item.blob_sha256 for item in plan.actionable])
        memory_digest = memory_record["payload"]["$blob"]["sha256"]
        active_memory_blob = next(
            item for item in plan.decisions if item.blob_sha256 == memory_digest
        )
        self.assertIn(
            "memory:protected-active-memory:active", active_memory_blob.pin_reasons
        )

        self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type="memory/disputed",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
            payload={"memory_id": "protected-active-memory"},
            occurred_at="2027-01-01T00:00:01Z",
        )
        disputed_plan = self.store.plan_retention(now="2027-01-02T00:00:00Z")
        disputed_memory_blob = next(
            item
            for item in disputed_plan.decisions
            if item.blob_sha256 == memory_digest
        )
        self.assertEqual(disputed_memory_blob.action, "keep")
        self.assertIn(
            "memory:protected-active-memory:disputed",
            disputed_memory_blob.pin_reasons,
        )

    def test_production_claim_requires_resolvable_exact_scope_blob_citations(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="evidence",
            marker="production-citation-source",
        )
        incompatible = self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type="note/observed",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
            payload={"status": "small-inline-event-is-not-a-blob-source"},
            occurred_at="2026-01-01T00:10:00Z",
        )
        other_task, other_session = self.identity()
        verified_before = self.store.verify().event_count
        invalid_payloads = (
            {"status": "missing"},
            {"source_event_ids": [], "status": "empty"},
            {"source_event_ids": [str(uuid.uuid4())], "status": "unknown"},
            {
                "source_event_ids": [incompatible["event_id"]],
                "status": "inline-incompatible",
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises((MemoryStoreError, ProjectionError)):
                    self.store.append(
                        host_id=HOST,
                        session_id=session_id,
                        task_id=task_id,
                        event_type="production/claim",
                        actor=ACTOR,
                        scope=MemoryScope(
                            OWNER, "workspace-a", task_id, None, None
                        ),
                        payload=payload,
                        occurred_at="2026-01-01T00:20:00Z",
                    )
                self.assertEqual(self.store.verify().event_count, verified_before)

        with self.assertRaisesRegex(ProjectionError, "cross an exact memory scope"):
            self.store.append(
                host_id=HOST,
                session_id=other_session,
                task_id=other_task,
                event_type="production/claim",
                actor=ACTOR,
                scope=MemoryScope(OWNER, "workspace-b", other_task, None, None),
                payload={
                    "source_event_ids": [source["event_id"]],
                    "status": "cross-scope",
                },
                occurred_at="2026-01-01T00:20:00Z",
            )
        self.assertEqual(self.store.verify().event_count, verified_before)
        self.assertFalse(
            (self.store.events_dir / HOST / f"{other_session}.jsonl").exists()
        )

        claim = self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type="production/claim",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
            payload={
                "source_event_ids": [source["event_id"]],
                "status": "production",
            },
            occurred_at="2026-01-01T00:30:00Z",
        )
        with closing(self.store._connect()) as connection:
            citation = connection.execute(
                "SELECT source_event_id, citation_kind FROM blob_citation "
                "WHERE citation_event_id = ?",
                (claim["event_id"],),
            ).fetchone()
        self.assertEqual(
            (citation["source_event_id"], citation["citation_kind"]),
            (source["event_id"], "production"),
        )

    def test_active_task_and_exact_scope_prevent_cross_scope_removal(self) -> None:
        shared_payload = {"marker": "shared", "content": "q" * 2_048}
        task_a, session_a = self.identity()
        task_b, session_b = self.identity()
        first = self.store.append(
            host_id=HOST,
            session_id=session_a,
            task_id=task_a,
            event_type="tool/result",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", task_a, None, None),
            payload=shared_payload,
            occurred_at=JANUARY,
            retention_class="scratch",
        )
        self.store.append(
            host_id=HOST,
            session_id=session_b,
            task_id=task_b,
            event_type="tool/result",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-b", task_b, None, None),
            payload=shared_payload,
            occurred_at=JANUARY,
            retention_class="scratch",
        )
        self.terminal(task_id=task_a, session_id=session_a, workspace="workspace-a")
        digest = first["payload"]["$blob"]["sha256"]

        active_plan = self.store.plan_retention(now=AFTER_SCRATCH_TTL)
        active = next(item for item in active_plan.decisions if item.blob_sha256 == digest)
        self.assertIn(f"active_task:{task_b}", active.pin_reasons)
        scoped_plan = self.store.plan_retention(
            now=AFTER_SCRATCH_TTL,
            scopes=[MemoryScope(OWNER, "workspace-a", task_a, None, None)],
        )
        scoped = next(item for item in scoped_plan.decisions if item.blob_sha256 == digest)
        self.assertIn("scope:outside_requested_exact_scope", scoped.pin_reasons)
        self.assertEqual(scoped.action, "keep")

    def test_stale_plan_is_rejected_after_new_citation(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="bulk",
            marker="stale-plan",
        )
        self.terminal(task_id=task_id, session_id=session_id)
        digest = source["payload"]["$blob"]["sha256"]
        plan = self.store.plan_retention(now=AFTER_BULK_TTL)
        self.assertEqual(
            next(item for item in plan.decisions if item.blob_sha256 == digest).action,
            "evict",
        )
        forged = json.loads(json.dumps(plan.as_dict()))
        forged["decisions"][0]["reason"] = "forged-evaluator-result"
        with self.assertRaisesRegex(MemoryStoreError, "plan digest mismatch"):
            self.store.run_gc(forged, apply=True)
        self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type="retention/cited",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
            payload={"source_event_ids": [source["event_id"]]},
            occurred_at="2026-01-03T00:00:00Z",
        )
        with self.assertRaisesRegex(ProjectionError, "plan is stale"):
            self.store.run_gc(plan, apply=True)
        self.assertTrue(self.store._blob_path(digest).is_file())
        self.assertFalse(self.store._tombstone_path(digest).exists())

    def test_two_processes_apply_one_plan_exactly_once_and_one_goes_stale(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="scratch",
            marker="cross-process-gc",
        )
        self.terminal(task_id=task_id, session_id=session_id)
        digest = source["payload"]["$blob"]["sha256"]
        plan_path = self.root / "gc-plan.json"
        plan_path.write_text(
            json.dumps(self.store.plan_retention(now=AFTER_SCRATCH_TTL).as_dict()),
            encoding="utf-8",
        )
        gate = self.root / "gc-start.gate"
        worker = r'''
import json
import sys
import time
from pathlib import Path

package, root, plan_path, ready_path, gate_path = map(Path, sys.argv[1:])
sys.path.insert(0, str(package / "src"))
from agent_continuity.memory_store import MemoryStore

store = MemoryStore(root, blob_threshold=256)
ready_path.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 20
while not gate_path.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("GC race gate did not open")
    time.sleep(0.01)
plan = json.loads(plan_path.read_text(encoding="utf-8"))
try:
    result = store.run_gc(plan, apply=True)
except Exception as error:
    print(json.dumps({"error_type": type(error).__name__, "error": str(error)}))
else:
    print(json.dumps({"status": result["status"], "applied": result["actions_applied"]}))
'''
        processes: list[subprocess.Popen[str]] = []
        try:
            for index in range(2):
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            worker,
                            str(PACKAGE),
                            str(self.root),
                            str(plan_path),
                            str(self.root / f"gc-worker-{index}.ready"),
                            str(gate),
                        ],
                        cwd=PACKAGE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            deadline = time.monotonic() + 20
            while not all(
                (self.root / f"gc-worker-{index}.ready").exists()
                for index in range(2)
            ):
                if time.monotonic() >= deadline:
                    self.fail("GC workers did not become ready")
                time.sleep(0.01)
            gate.write_text("go", encoding="utf-8")
            outputs = [process.communicate(timeout=30) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
        for process, (stdout, stderr) in zip(processes, outputs, strict=True):
            self.assertEqual(process.returncode, 0, f"stdout={stdout} stderr={stderr}")
        observations = [json.loads(stdout) for stdout, _ in outputs]
        self.assertEqual(
            sum(item.get("status") == "applied" for item in observations), 1
        )
        stale = [item for item in observations if item.get("error_type")]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["error_type"], "ProjectionError")
        self.assertIn("plan is stale", stale[0]["error"])
        recovered = MemoryStore(self.root, blob_threshold=256)
        self.assertFalse(recovered._blob_path(digest).exists())
        self.assertEqual(self.catalog(digest)["availability"], "evicted")

    def test_unpinned_bulk_is_refused_before_any_write_at_95_percent_pressure(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        try:
            root = Path(temporary.name)
            store = MemoryStore(root, blob_threshold=256, hot_blob_budget_bytes=6_000)
            task_id, session_id = self.identity()
            store.append(
                host_id=HOST,
                session_id=session_id,
                task_id=task_id,
                event_type="tool/result",
                actor=ACTOR,
                scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
                payload={"content": "s" * 4_900},
                retention_class="scratch",
            )
            before = store.verify()
            blob_files_before = sorted(path.as_posix() for path in store.blobs_dir.rglob("*"))
            with self.assertRaisesRegex(RetentionPressureError, "refused before durable write"):
                store.append(
                    host_id=HOST,
                    session_id=session_id,
                    task_id=task_id,
                    event_type="model/response",
                    actor=ACTOR,
                    scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
                    payload={"content": "b" * 1_000},
                    retention_class="bulk",
                )
            self.assertEqual(store.verify().event_count, before.event_count)
            self.assertEqual(
                sorted(path.as_posix() for path in store.blobs_dir.rglob("*")),
                blob_files_before,
            )
        finally:
            temporary.cleanup()

    def test_95_percent_refusal_boundary_uses_exact_integer_arithmetic(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        try:
            root = Path(temporary.name)
            store = MemoryStore(
                root, blob_threshold=256, hot_blob_budget_bytes=1_000_000
            )
            task_id, session_id = self.identity()
            store.append(
                host_id=HOST,
                session_id=session_id,
                task_id=task_id,
                event_type="tool/result",
                actor=ACTOR,
                scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
                payload={"content": "s" * 1_024},
                retention_class="scratch",
            )
            with closing(store._connect()) as connection:
                hot_bytes = int(
                    connection.execute(
                        "SELECT SUM(size_bytes) FROM blob_catalog "
                        "WHERE availability = 'hot'"
                    ).fetchone()[0]
                )
            payload = None
            payload_size = 0
            for characters in range(300, 400):
                candidate = {"content": "b" * characters}
                candidate_size = len(
                    json.dumps(
                        candidate,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                )
                if (hot_bytes + candidate_size) % 19 == 0:
                    payload = candidate
                    payload_size = candidate_size
                    break
            self.assertIsNotNone(payload)
            projected = hot_bytes + payload_size
            exact_budget = projected * 20 // 19
            self.assertEqual(projected * 10_000, exact_budget * 9_500)
            store.hot_blob_budget_bytes = exact_budget
            before = store.verify().event_count
            with self.assertRaises(RetentionPressureError):
                store.append(
                    host_id=HOST,
                    session_id=session_id,
                    task_id=task_id,
                    event_type="model/response",
                    actor=ACTOR,
                    scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
                    payload=payload,
                    retention_class="bulk",
                )
            self.assertEqual(store.verify().event_count, before)

            store.hot_blob_budget_bytes = exact_budget + 1
            store.append(
                host_id=HOST,
                session_id=session_id,
                task_id=task_id,
                event_type="model/response",
                actor=ACTOR,
                scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
                payload=payload,
                retention_class="bulk",
            )
            self.assertLess(
                store.plan_retention().pressure_basis_points, 9_500
            )
        finally:
            temporary.cleanup()

    def test_interrupted_post_event_unlink_is_reconciled_on_fresh_open(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="scratch",
            marker="crash-safe",
        )
        self.terminal(task_id=task_id, session_id=session_id)
        digest = source["payload"]["$blob"]["sha256"]
        plan = self.store.plan_retention(now=AFTER_SCRATCH_TTL)
        with mock.patch.object(
            self.store, "_unlink_hot_blob", side_effect=OSError("injected unlink failure")
        ):
            with self.assertRaisesRegex(OSError, "injected unlink failure"):
                self.store.run_gc(plan, apply=True)
        self.assertTrue(self.store._blob_path(digest).is_file())
        self.assertTrue(self.store._tombstone_path(digest).is_file())
        self.assertEqual(self.catalog(digest)["availability"], "evicted")

        recovered = MemoryStore(self.root, blob_threshold=256)
        self.assertFalse(recovered._blob_path(digest).exists())
        self.assertEqual(recovered.verify().event_count, 3)
        self.assertEqual(recovered.replay(rebuild=True).verified_events, 3)

    def test_pre_action_tombstone_crash_is_recovered_before_verify(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="scratch",
            marker="pre-action-tombstone",
        )
        self.terminal(task_id=task_id, session_id=session_id)
        digest = source["payload"]["$blob"]["sha256"]
        plan = self.store.plan_retention(now=AFTER_SCRATCH_TTL)
        plan_path = self.root / "pre-action-crash-plan.json"
        plan_path.write_text(json.dumps(plan.as_dict()), encoding="utf-8")
        worker = r'''
import json
import os
import sys
from pathlib import Path

package, root, plan_path = map(Path, sys.argv[1:])
sys.path.insert(0, str(package / "src"))
from agent_continuity.memory_store import MemoryStore

store = MemoryStore(root, blob_threshold=256)
store.append = lambda *args, **kwargs: os._exit(74)
store.run_gc(json.loads(plan_path.read_text(encoding="utf-8")), apply=True)
'''
        crashed = subprocess.run(
            [
                sys.executable,
                "-c",
                worker,
                str(PACKAGE),
                str(self.root),
                str(plan_path),
            ],
            cwd=PACKAGE,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, 74, crashed.stderr)
        self.assertEqual((crashed.stdout, crashed.stderr), ("", ""))
        self.assertTrue(self.store._tombstone_path(digest).is_file())
        self.assertTrue(self.store._blob_path(digest).is_file())

        recovered = MemoryStore(self.root, blob_threshold=256)
        self.assertFalse(recovered._tombstone_path(digest).exists())
        self.assertTrue(recovered._blob_path(digest).is_file())
        target = next(
            item
            for item in recovered.plan_retention(now=AFTER_SCRATCH_TTL).decisions
            if item.blob_sha256 == digest
        )
        self.assertEqual(target.action, "evict")
        self.assertEqual(recovered.verify().event_count, 2)

    def test_durable_action_event_without_projection_replays_then_unlinks(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="scratch",
            marker="action-before-projection",
        )
        self.terminal(task_id=task_id, session_id=session_id)
        digest = source["payload"]["$blob"]["sha256"]
        plan = self.store.plan_retention(now=AFTER_SCRATCH_TTL)
        original_apply = self.store._apply_event
        calls = 0

        def fail_actual_projection(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected death before projection commit")
            original_apply(*args, **kwargs)

        with mock.patch.object(
            self.store, "_apply_event", side_effect=fail_actual_projection
        ):
            with self.assertRaisesRegex(
                ProjectionError, "event is durable but projection failed"
            ):
                self.store.run_gc(plan, apply=True)
        self.assertEqual(calls, 2)
        self.assertTrue(self.store._tombstone_path(digest).is_file())
        self.assertTrue(self.store._blob_path(digest).is_file())
        self.assertEqual(self.catalog(digest)["availability"], "hot")

        recovered = MemoryStore(self.root, blob_threshold=256)
        self.assertFalse(recovered._blob_path(digest).exists())
        self.assertEqual(self.catalog(digest)["availability"], "evicted")
        self.assertEqual(recovered.verify().event_count, 3)

    def test_pre_tombstone_archive_crash_removes_only_orphan_archive(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="bulk",
            marker="pre-tombstone-archive",
        )
        self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type="retention/cited",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
            payload={"source_event_ids": [source["event_id"]]},
            occurred_at="2026-01-01T01:00:00Z",
        )
        self.terminal(task_id=task_id, session_id=session_id)
        digest = source["payload"]["$blob"]["sha256"]
        plan = self.store.plan_retention(now=AFTER_BULK_TTL)
        archive = self.store._archive_path(digest, plan.observed_at)

        with mock.patch.object(
            self.store,
            "_write_tombstone",
            side_effect=RuntimeError("injected death before tombstone"),
        ):
            with self.assertRaisesRegex(RuntimeError, "before tombstone"):
                self.store.run_gc(plan, apply=True)
        self.assertTrue(archive.is_file())
        self.assertFalse(self.store._tombstone_path(digest).exists())
        self.assertTrue(self.store._blob_path(digest).is_file())

        recovered = MemoryStore(self.root, blob_threshold=256)
        self.assertFalse(archive.exists())
        self.assertTrue(recovered._blob_path(digest).is_file())
        self.assertEqual(self.catalog(digest)["availability"], "hot")
        self.assertEqual(recovered.verify().event_count, 3)

    def test_evicted_digest_reuse_is_refused_before_any_write(self) -> None:
        task_id, session_id = self.identity()
        payload = {"marker": "evicted-reuse", "content": "evicted-reuse" * 2_048}
        source = self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type="tool/result",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
            payload=payload,
            occurred_at=JANUARY,
            retention_class="scratch",
        )
        self.terminal(task_id=task_id, session_id=session_id)
        digest = source["payload"]["$blob"]["sha256"]
        self.store.run_gc(
            self.store.plan_retention(now=AFTER_SCRATCH_TTL), apply=True
        )
        verified_before = self.store.verify()
        new_task, new_session = self.identity()

        with self.assertRaisesRegex(
            BlobEvictedError, "admission was refused before any write"
        ):
            self.store.append(
                host_id=HOST,
                session_id=new_session,
                task_id=new_task,
                event_type="tool/result",
                actor=ACTOR,
                scope=MemoryScope(OWNER, "workspace-a", new_task, None, None),
                payload=payload,
                occurred_at="2026-02-01T00:00:00Z",
                retention_class="scratch",
            )
        self.assertEqual(self.store.verify(), verified_before)
        self.assertFalse((self.store.events_dir / HOST / f"{new_session}.jsonl").exists())
        self.assertFalse(self.store._blob_path(digest).exists())
        self.assertEqual(self.catalog(digest)["reference_count"], 1)

    def test_retention_writes_fail_closed_on_symlink_or_junction_escape(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        redirected = self.store.archive_dir / "2026-01"
        if os.name == "nt":
            command = (
                "$ErrorActionPreference='Stop'; "
                f"New-Item -ItemType Junction -Path '{redirected}' "
                f"-Target '{outside}' | Out-Null"
            )
            created = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest(f"junction creation unavailable: {created.stderr}")
        else:
            os.symlink(outside, redirected, target_is_directory=True)
        try:
            with self.assertRaisesRegex(
                MemoryStoreError, "archive write escapes"
            ):
                self.store._write_archive(
                    "sha256:" + "a" * 64,
                    b"retention path containment",
                    "2026-01-01T00:00:00Z",
                )
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            redirected.rmdir()

    def test_missing_zstd_is_runtime_capability_failure_not_false_success(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="bulk",
            marker="archive-runtime-blocked",
        )
        self.store.append(
            host_id=HOST,
            session_id=session_id,
            task_id=task_id,
            event_type="retention/cited",
            actor=ACTOR,
            scope=MemoryScope(OWNER, "workspace-a", task_id, None, None),
            payload={"source_event_ids": [source["event_id"]]},
        )
        self.terminal(task_id=task_id, session_id=session_id)
        digest = source["payload"]["$blob"]["sha256"]
        plan = self.store.plan_retention(now=AFTER_BULK_TTL)
        with mock.patch.object(
            self.store,
            "_compress_archive",
            side_effect=CapabilityError("zstd unavailable"),
        ):
            with self.assertRaisesRegex(CapabilityError, "zstd unavailable"):
                self.store.run_gc(plan, apply=True)
        self.assertTrue(self.store._blob_path(digest).is_file())
        self.assertFalse(self.store._tombstone_path(digest).exists())
        self.assertEqual(self.catalog(digest)["availability"], "hot")

    def test_tombstone_mutation_is_detected(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="scratch",
            marker="mutate-tombstone",
        )
        self.terminal(task_id=task_id, session_id=session_id)
        digest = source["payload"]["$blob"]["sha256"]
        plan = self.store.plan_retention(now=AFTER_SCRATCH_TTL)
        self.store.run_gc(plan, apply=True)
        path = self.store._tombstone_path(digest)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["excerpt"]["head"] = "tampered"
        path.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(Exception, "tombstone digest mismatch"):
            self.store.verify()

    def test_projection_upgrade_marker_forces_canonical_backfill(self) -> None:
        task_id, session_id = self.identity()
        source = self.append_blob(
            task_id=task_id,
            session_id=session_id,
            retention_class="scratch",
            marker="projection-upgrade",
        )
        digest = source["payload"]["$blob"]["sha256"]
        with closing(self.store._connect()) as connection:
            with connection:
                connection.execute("DELETE FROM blob_reference")
                connection.execute(
                    "DELETE FROM projection_metadata WHERE key = 'schema_version'"
                )
        rebuilt = MemoryStore(self.root, blob_threshold=256)
        with closing(rebuilt._connect()) as connection:
            reference = connection.execute(
                "SELECT event_id FROM blob_reference WHERE blob_sha256 = ?", (digest,)
            ).fetchone()
            version = connection.execute(
                "SELECT value FROM projection_metadata WHERE key = 'schema_version'"
            ).fetchone()
        self.assertEqual(reference["event_id"], source["event_id"])
        self.assertEqual(version["value"], "2")
        target = next(
            item
            for item in rebuilt.plan_retention(now=AFTER_SCRATCH_TTL).decisions
            if item.blob_sha256 == digest
        )
        self.assertIn(f"active_task:{task_id}", target.pin_reasons)


if __name__ == "__main__":
    unittest.main()
