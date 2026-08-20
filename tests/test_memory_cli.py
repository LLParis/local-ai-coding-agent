from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from agent_continuity.cli import (  # noqa: E402
    SEMANTIC_VERIFICATION_SCHEMA,
    main,
)
from agent_continuity.compaction import (  # noqa: E402
    TASK_STATE_SCHEMA,
    load_task_state,
    task_state_sha256,
)

SESSION_ID = "00000000-0000-4000-8000-000000000001"
TASK_ID = "00000000-0000-4000-8000-000000000002"
EVENT_ID = "00000000-0000-4000-8000-000000000003"
STATE_EVENT_ID = "00000000-0000-4000-8000-000000000006"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def task_state(through_event_id: str, previous_sha256: str | None) -> dict[str, object]:
    return {
        "schema": TASK_STATE_SCHEMA,
        "task_id": "task-memory-cli",
        "objective": {
            "text": "Preserve the exact active task state.",
            "source_event_id": "event-objective",
        },
        "success_criteria": [],
        "constraints": [],
        "decisions": [],
        "completed": [],
        "current_action": {
            "text": "Validate the candidate state.",
            "owner": "deterministic-validator",
            "started_at": "2026-08-20T08:00:00Z",
        },
        "next_actions": [],
        "blockers": [],
        "open_tool_calls": [],
        "artifacts": [],
        "disputes": [],
        "through_event_id": through_event_id,
        "previous_state_sha256": previous_sha256,
    }


class MemoryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store_root = self.root / "store"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: object) -> tuple[int, dict[str, object] | None, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([str(argument) for argument in arguments])
        output = stdout.getvalue().strip()
        return code, json.loads(output) if output else None, stderr.getvalue()

    def run_fresh_process(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PACKAGE / "src")
        return subprocess.run(
            [sys.executable, "-m", "agent_continuity.cli", *map(str, arguments)],
            cwd=PACKAGE,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def run_wrapper(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        if os.name == "nt":
            command = [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                str(PACKAGE / "bin" / "continuity.cmd"),
                *map(str, arguments),
            ]
        else:
            command = [str(PACKAGE / "bin" / "continuity"), *map(str, arguments)]
        return subprocess.run(
            command,
            cwd=PACKAGE,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def memory_event(self) -> dict[str, object]:
        text = r"Use D:\work\src\owner.ps1 as the exact owner script."
        source_hash = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
        return {
            "host_id": "excalibur",
            "session_id": SESSION_ID,
            "task_id": TASK_ID,
            "event_type": "memory/committed",
            "actor": {"kind": "user", "id": "operator"},
            "scope": {
                "owner_id": "local-owner",
                "workspace_id": "workspace-a",
                "task_id": TASK_ID,
                "agent_role": None,
                "session_id": None,
            },
            "payload": {
                "memory_id": "owner-path",
                "kind": "fact",
                "subject": "owner-path",
                "predicate": "uses",
                "object": {"text": text},
                "searchable_text": text,
                "valid_from": "2026-08-20T08:00:00Z",
                "verification_status": "tested",
                "evidence": [
                    {
                        "source_kind": "test",
                        "source_locator": "tests/test_memory_cli.py",
                        "source_sha256": source_hash,
                        "authority": "authoritative-test",
                        "excerpt": text,
                    }
                ],
            },
            "parent_event_id": None,
            "event_id": EVENT_ID,
            "occurred_at": "2026-08-20T08:01:00Z",
            "retention_class": "core",
        }

    def memory_state_event(self) -> dict[str, object]:
        state = {
            "schema": TASK_STATE_SCHEMA,
            "task_id": TASK_ID,
            "objective": {
                "text": "Use the exact owner script.",
                "source_event_id": STATE_EVENT_ID,
            },
            "success_criteria": [],
            "constraints": [],
            "decisions": [],
            "completed": [],
            "current_action": {
                "text": "Pack the current working set.",
                "owner": "retrieval-gate",
                "started_at": "2026-08-20T08:00:00Z",
            },
            "next_actions": [],
            "blockers": [],
            "open_tool_calls": [],
            "artifacts": [],
            "disputes": [],
            "through_event_id": STATE_EVENT_ID,
            "previous_state_sha256": None,
        }
        return {
            "host_id": "excalibur",
            "session_id": SESSION_ID,
            "task_id": TASK_ID,
            "event_type": "state/committed",
            "actor": {"kind": "user", "id": "operator"},
            "scope": {
                "owner_id": "local-owner",
                "workspace_id": "workspace-a",
                "task_id": TASK_ID,
                "agent_role": None,
                "session_id": None,
            },
            "payload": {"revision": 1, "state": state},
            "event_id": STATE_EVENT_ID,
            "occurred_at": "2026-08-20T08:00:00Z",
        }

    def compaction_inputs(self) -> tuple[Path, Path, Path, Path, dict[str, object]]:
        previous = task_state("event-previous", None)
        candidate = task_state("event-range", task_state_sha256(previous))
        previous_path = self.root / "previous.json"
        candidate_path = self.root / "candidate.json"
        range_path = self.root / "range.json"
        citations_path = self.root / "citations.json"
        write_json(previous_path, previous)
        write_json(candidate_path, candidate)
        write_json(
            range_path,
            {
                "first_event_id": "event-range",
                "first_record_sha256": HASH_A,
                "last_event_id": "event-range",
                "last_record_sha256": HASH_A,
                "retained_tail_event_id": "event-range",
            },
        )
        write_json(
            citations_path,
            {
                "events": {"event-objective": HASH_B, "event-range": HASH_A},
                "evidence": [],
                "artifacts": {},
            },
        )
        return previous_path, candidate_path, range_path, citations_path, candidate

    def test_memory_commands_persist_across_process_and_rebuild_projection(self) -> None:
        event_path = self.root / "event.json"
        scope_path = self.root / "scope.json"
        write_json(event_path, self.memory_event())
        write_json(
            scope_path,
            {
                "owner_id": "local-owner",
                "workspace_id": "workspace-a",
                "task_id": TASK_ID,
                "agent_role": None,
                "session_id": None,
            },
        )

        with mock.patch("agent_continuity.cli.run_local_edit") as model_path:
            code, report, error = self.run_cli("memory-init", "--root", self.store_root)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(report["event_count"], 0)

            code, report, error = self.run_cli(
                "memory-append", "--root", self.store_root, "--event", event_path
            )
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(report["event_id"], EVENT_ID)

            code, report, error = self.run_cli("memory-verify", "--root", self.store_root)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(report["event_count"], 1)
            model_path.assert_not_called()

        fresh = self.run_fresh_process(
            "memory-search",
            "--root",
            self.store_root,
            "--query",
            "owner.ps1",
            "--scope",
            scope_path,
        )
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        search = json.loads(fresh.stdout)
        self.assertEqual(search["count"], 1)
        self.assertEqual(search["results"][0]["memory_id"], "owner-path")

        phantom_id = "00000000-0000-4000-8000-000000000004"
        connection = sqlite3.connect(self.store_root / "memory.sqlite3")
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
                        "00000000-0000-4000-8000-000000000005",
                        1,
                        "test/phantom",
                        "2026-08-20T08:02:00Z",
                        "phantom.jsonl",
                        0,
                        HASH_A,
                        HASH_B,
                    ),
                )
        finally:
            connection.close()

        rebuilt = self.run_wrapper("memory-rebuild", "--root", self.store_root)
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        report = json.loads(rebuilt.stdout)
        self.assertEqual(report["status"], "rebuilt")
        self.assertEqual(report["event_count"], 1)
        connection = sqlite3.connect(self.store_root / "memory.sqlite3")
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM event_index WHERE event_id = ?", (phantom_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)

    def test_memory_append_rejects_unknown_and_duplicate_json_keys(self) -> None:
        bad_path = self.root / "bad-event.json"
        event = self.memory_event()
        event["unknown"] = True
        write_json(bad_path, event)

        code, report, error = self.run_cli(
            "memory-append", "--root", self.store_root, "--event", bad_path
        )
        self.assertEqual(code, 2)
        self.assertIsNone(report)
        self.assertIn("unknown keys: unknown", error)

        bad_path.write_text('{"host_id":"one","host_id":"two"}', encoding="utf-8")
        code, report, error = self.run_cli(
            "memory-append", "--root", self.store_root, "--event", bad_path
        )
        self.assertEqual(code, 2)
        self.assertIsNone(report)
        self.assertIn("duplicate JSON key: host_id", error)
        self.assertFalse((self.store_root / "events").exists())

    def test_memory_get_and_offline_pack_return_state_first_provenance(self) -> None:
        state_path = self.root / "state-event.json"
        memory_path = self.root / "memory-event.json"
        scope_path = self.root / "scope.json"
        write_json(state_path, self.memory_state_event())
        write_json(memory_path, self.memory_event())
        write_json(scope_path, self.memory_event()["scope"])
        for path in (state_path, memory_path):
            code, _, error = self.run_cli(
                "memory-append", "--root", self.store_root, "--event", path
            )
            self.assertEqual((code, error), (0, ""))

        code, report, error = self.run_cli(
            "memory-get", "--root", self.store_root, "--memory-id", "owner-path"
        )
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(report["memory"]["memory_id"], "owner-path")
        self.assertEqual(
            report["memory"]["evidence"][0]["source_locator"],
            "tests/test_memory_cli.py",
        )

        code, report, error = self.run_cli(
            "memory-pack",
            "--root",
            self.store_root,
            "--task-id",
            TASK_ID,
            "--request",
            "Find owner.ps1",
            "--scope",
            scope_path,
            "--file-symbol",
            r"D:\work\src\owner.ps1",
            "--offline-counter",
            "whitespace-v1",
        )
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(report["model_text"].startswith("## CURRENT TYPED TASK STATE\n"))
        self.assertEqual(report["included_memory_ids"], ["owner-path"])
        self.assertEqual(
            report["retrieval_audit"]["memory"]["candidates"][0]["decision"],
            "included",
        )
        self.assertEqual(report["retrieval_audit"]["token_counter"], "offline:whitespace-v1")

        code, report, error = self.run_cli(
            "memory-get", "--root", self.store_root, "--memory-id", "absent"
        )
        self.assertEqual((code, error), (1, ""))
        self.assertEqual(report, {"memory_id": "absent", "status": "not_found"})

    def test_memory_verify_rejects_a_tampered_canonical_log(self) -> None:
        event_path = self.root / "event.json"
        write_json(event_path, self.memory_event())
        self.assertEqual(
            self.run_cli("memory-append", "--root", self.store_root, "--event", event_path)[0],
            0,
        )
        segment = next((self.store_root / "events").glob("*/*.jsonl"))
        record = json.loads(segment.read_text(encoding="utf-8"))
        record["payload_sha256"] = HASH_A
        segment.write_bytes(
            (
                json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                + "\n"
            ).encode("utf-8")
        )

        code, report, error = self.run_cli("memory-verify", "--root", self.store_root)
        self.assertEqual(code, 2)
        self.assertIsNone(report)
        self.assertIn("record hash mismatch", error)

    def test_compaction_validate_and_commit_require_bound_semantic_approval(self) -> None:
        previous, candidate, source_range, citations, candidate_value = self.compaction_inputs()
        common = (
            "--previous",
            previous,
            "--candidate",
            candidate,
            "--source-range",
            source_range,
            "--citations",
            citations,
        )

        with mock.patch("agent_continuity.cli.run_local_edit") as model_path:
            code, report, error = self.run_cli("compaction-validate", *common)
            self.assertEqual((code, error), (0, ""))
            self.assertTrue(report["accepted"])
            self.assertEqual(report["semantic_verifier"]["status"], "pending")

            semantic_path = self.root / "semantic.json"
            write_json(
                semantic_path,
                {
                    "schema": SEMANTIC_VERIFICATION_SCHEMA,
                    "candidate_state_sha256": "sha256:" + "c" * 64,
                    "verdict": "accepted",
                    "verifier_id": "independent-verifier",
                    "checks": ["semantic_omissions", "semantic_contradictions"],
                },
            )
            output_path = self.root / "states" / "revision-2.json"
            code, report, error = self.run_cli(
                "compaction-commit",
                *common,
                "--semantic-verifier",
                semantic_path,
                "--output",
                output_path,
            )
            self.assertEqual(code, 2)
            self.assertIsNone(report)
            self.assertIn("different candidate", error)
            self.assertFalse(output_path.exists())

            semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
            semantic["candidate_state_sha256"] = task_state_sha256(candidate_value)
            write_json(semantic_path, semantic)
            code, report, error = self.run_cli(
                "compaction-commit",
                *common,
                "--semantic-verifier",
                semantic_path,
                "--output",
                output_path,
            )
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(report["status"], "committed")
            self.assertEqual(load_task_state(output_path), candidate_value)
            model_path.assert_not_called()

        rejected = copy.deepcopy(candidate_value)
        rejected["objective"]["text"] = "Changed objective."
        rejected_path = self.root / "rejected.json"
        write_json(rejected_path, rejected)
        code, report, error = self.run_cli(
            "compaction-validate",
            "--previous",
            previous,
            "--candidate",
            rejected_path,
            "--source-range",
            source_range,
            "--citations",
            citations,
        )
        self.assertEqual((code, error), (1, ""))
        self.assertFalse(report["accepted"])
        self.assertIn("objective_changed", {item["code"] for item in report["issues"]})


if __name__ == "__main__":
    unittest.main()
