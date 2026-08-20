from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ops import codex_catalog_maintenance as catalog


class CatalogMaintenanceTests(unittest.TestCase):
    def test_exact_subagent_source_is_fail_closed(self) -> None:
        self.assertTrue(catalog.exact_subagent_source('{"subagent":{"other":"guardian"}}'))
        self.assertTrue(
            catalog.exact_subagent_source(
                '{"subagent":{"thread_spawn":{"parent_thread_id":"root"}}}'
            )
        )
        for rejected in (
            "vscode",
            "{malformed",
            '{"subagent":null}',
            '{"subagent":{},"vscode":true}',
            '{"source":"subagent"}',
        ):
            with self.subTest(rejected=rejected):
                self.assertFalse(catalog.exact_subagent_source(rejected))

    def test_counts_tolerate_non_json_root_sources(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE threads (archived INTEGER, source TEXT)")
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?)",
            (
                (0, "vscode"),
                (0, '{"subagent":{"other":"guardian"}}'),
                (0, "not-json"),
                (1, '{"subagent":{"other":"guardian"}}'),
            ),
        )
        try:
            self.assertEqual(
                catalog.counts(connection),
                {"active": 3, "archived": 1, "active_subagents": 1, "active_vscode": 1},
            )
        finally:
            connection.close()

    def test_terminal_task_complete_requires_exact_final_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            sessions.mkdir()
            thread_id = "019cfd32-fd40-79f2-8765-824cdce99f98"
            rollout = sessions / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
            cutoff = int(time.time()) - 86400

            def write_final(record: dict[str, object], *, recent: bool = False) -> None:
                rollout.write_text(
                    json.dumps({"type": "session_meta"}) + "\n" + json.dumps(record) + "\n",
                    encoding="utf-8",
                )
                modified = int(time.time()) if recent else cutoff - 60
                os.utime(rollout, (modified, modified))

            write_final({"type": "event_msg", "payload": {"type": "task_complete"}})
            self.assertEqual(
                catalog.terminal_task_complete(rollout, sessions.resolve(), thread_id, cutoff),
                (True, "terminal_task_complete"),
            )

            write_final({"type": "event_msg", "payload": {"type": "agent_message"}})
            self.assertEqual(
                catalog.terminal_task_complete(rollout, sessions.resolve(), thread_id, cutoff)[1],
                "terminal_event_not_task_complete",
            )

            write_final({"type": "response_item", "payload": {"type": "task_complete"}})
            self.assertEqual(
                catalog.terminal_task_complete(rollout, sessions.resolve(), thread_id, cutoff)[1],
                "terminal_record_not_event",
            )

            write_final(
                {"type": "event_msg", "payload": {"type": "task_complete"}}, recent=True
            )
            self.assertEqual(
                catalog.terminal_task_complete(rollout, sessions.resolve(), thread_id, cutoff)[1],
                "rollout_recently_modified",
            )

    def test_candidate_query_never_selects_vscode(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE threads (id TEXT, source TEXT, updated_at INTEGER, "
            "rollout_path TEXT, archived INTEGER)"
        )
        cutoff = int(time.time()) - 86400
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
            (
                ("sub", '{"subagent":{"other":"guardian"}}', cutoff - 1, "sub.jsonl", 0),
                ("root", "vscode", cutoff - 1, "root.jsonl", 0),
                ("recent", '{"subagent":{}}', cutoff + 1, "recent.jsonl", 0),
                ("archived", '{"subagent":{}}', cutoff - 1, "archived.jsonl", 1),
            ),
        )
        try:
            self.assertEqual(
                [row["id"] for row in catalog.candidate_rows(connection, cutoff)], ["sub"]
            )
        finally:
            connection.close()

    def test_single_instance_lock_rejects_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "maintenance.lock"
            first = catalog.acquire_instance_lock(lock_path)
            try:
                with self.assertRaises(catalog.AlreadyRunningError):
                    catalog.acquire_instance_lock(lock_path)
            finally:
                first.close()

    def test_failed_uuid_is_recorded_once_and_never_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex_home = root / ".codex"
            sessions = codex_home / "sessions/2026/01/01"
            sessions.mkdir(parents=True)
            state_root = codex_home / "coding-intelligence/catalog-maintenance"
            state_root.mkdir(parents=True)
            thread_id = "019cfd32-fd40-79f2-8765-824cdce99f98"
            rollout = sessions / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
            rollout.write_text(
                json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}})
                + "\n",
                encoding="utf-8",
            )
            old = int(time.time()) - 172800
            os.utime(rollout, (old, old))
            db_path = codex_home / "state_1.sqlite"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, updated_at INTEGER, "
                "rollout_path TEXT, archived INTEGER, archived_at INTEGER)"
            )
            connection.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, 0, NULL)",
                (thread_id, '{"subagent":{"other":"guardian"}}', old, str(rollout)),
            )
            connection.commit()
            connection.close()
            args = argparse.Namespace(limit=1, dry_run=False, probe_only=False, self_check=False)
            fake_codex = root / "codex"
            failure = ({"exit_code": 1, "elapsed_seconds": 0.1}, False)

            with (
                mock.patch.object(catalog, "archive_one", return_value=failure) as archive,
                mock.patch.object(
                    catalog,
                    "verify_archived",
                    return_value=(False, {"archived": False}),
                ),
            ):
                first = catalog.run_maintenance(
                    args, codex_home, fake_codex, state_root, db_path, state_root / "log"
                )
                second = catalog.run_maintenance(
                    args, codex_home, fake_codex, state_root, db_path, state_root / "log"
                )

            self.assertIsNotNone(first["failure"])
            self.assertEqual(second["selected"], [])
            self.assertEqual(second["skipped"], {"previously_attempted_no_retry": 1})
            archive.assert_called_once_with(fake_codex, thread_id)
            attempted = json.loads((state_root / "attempted-ids.json").read_text(encoding="utf-8"))
            self.assertEqual(attempted[thread_id]["status"], "failed_no_automatic_retry")
            self.assertEqual(
                attempted[thread_id]["unarchive_command"],
                [str(fake_codex), "unarchive", thread_id],
            )


if __name__ == "__main__":
    unittest.main()
