from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from agent_continuity.run_memory import (  # noqa: E402
    RUN_FINALIZATION_SCHEMA,
    RunIdentity,
    RunMemoryError,
    RunMemoryRecorder,
    file_sha256,
    text_sha256,
)


class RunMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.source = self.workspace / "feature.py"
        self.source.write_text("answer = 1\n", encoding="utf-8")
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.trajectory = self.stage / "continuity-trajectory.json"
        self.trajectory.write_text('{"bounded":true}\n', encoding="utf-8")
        self.identity = RunIdentity(str(uuid.uuid4()), str(uuid.uuid4()))
        self.recorder = RunMemoryRecorder(
            root=self.root / "memory",
            identity=self.identity,
            workspace=self.workspace,
            objective="Repair the bounded fixture.",
            model="fake-local-model",
            host_id="test-host",
        )
        self.empty_hash = text_sha256("")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def summary(self, *, status: str = "verified") -> dict[str, object]:
        return {
            "schema": RUN_FINALIZATION_SCHEMA,
            "status": status,
            "process_exit": 0 if status == "verified" else 1,
            "command_exit": 0,
            "model_calls": 2,
            "automatic_retries": 0,
            "failure_sha256": None,
            "implementation": {
                "status": "verified",
                "stage_locator": str(self.stage),
                "trajectory_locator": str(self.trajectory),
                "trajectory_sha256": file_sha256(self.trajectory),
                "request_sha256": text_sha256("request"),
                "prompt_sha256": text_sha256("prompt"),
                "response_sha256": text_sha256("response"),
                "response_observed": True,
                "response_bytes": 8,
                "http_status": 200,
                "inference_seconds": 0.25,
                "files": [
                    {
                        "path": "feature.py",
                        "before_sha256": text_sha256("answer = 1\n"),
                        "after_sha256": text_sha256("answer = 2\n"),
                    }
                ],
                "diff_sha256": text_sha256("diff"),
                "test_command_sha256": text_sha256("test command"),
                "test_exit": 0,
                "test_output_sha256": text_sha256("ok"),
            },
            "verifier": {
                "status": "accepted",
                "model": "fake-verifier",
                "verdict": "accept",
                "reason_sha256": text_sha256("correct"),
                "risks_sha256": self.empty_hash,
                "model_calls": 1,
            },
            "restore": {
                "status": "ready",
                "backend": "Qwen38",
                "error_sha256": None,
            },
        }

    def test_event_order_projection_evidence_final_state_and_idempotent_resume(self) -> None:
        self.recorder.ensure_started(
            mutable=["feature.py"],
            context=["feature.py"],
            verify_context=["tests"],
            test_command=[sys.executable, "-m", "unittest"],
        )
        self.recorder.record_model_intent(
            request_sha256=text_sha256("request"),
            prompt_sha256=text_sha256("prompt"),
            trajectory_path=self.trajectory,
        )
        self.assertEqual(self.recorder.resume_classification(), "outcome_unknown")
        self.recorder.record_model_response(
            response_sha256=text_sha256("response"),
            response_bytes=8,
            http_status=200,
            inference_seconds=0.25,
        )
        self.recorder.record_implementation(
            files=self.summary()["implementation"]["files"],
            diff_sha256=text_sha256("diff"),
            test_command_sha256=text_sha256("test command"),
            test_exit=0,
            test_output_sha256=text_sha256("ok"),
            trajectory_path=self.trajectory,
            trajectory_sha256=file_sha256(self.trajectory),
            status="verified",
        )
        first = self.recorder.finalize(self.summary())
        count = first["event_count"]
        second = self.recorder.finalize(self.summary())
        self.assertEqual(second["event_count"], count)

        events = self.recorder._events()
        self.assertEqual(
            [event["type"] for event in events],
            [
                "task/started",
                "state/committed",
                "model/request",
                "model/response",
                "file/edited",
                "verification/result",
                "verification/result",
                "tool/result",
                "task/completed",
                "memory/committed",
                "state/committed",
            ],
        )
        response = next(event for event in events if event["type"] == "model/response")
        self.assertIn("response_sha256", response["payload"])
        self.assertNotIn("raw_response", response["payload"])
        memory = self.recorder.store.get(f"episode:{self.identity.task_id}")
        self.assertEqual(len(memory["evidence"]), 2)
        self.assertEqual(
            {item["authority"] for item in memory["evidence"]},
            {"authoritative-test", "independent-verifier"},
        )
        with closing(self.recorder.store._connect()) as connection:
            revision, state_json = connection.execute(
                "SELECT revision, state_json FROM task_state WHERE task_id = ?",
                (self.identity.task_id,),
            ).fetchone()
        self.assertEqual(revision, 2)
        self.assertEqual(json.loads(state_json)["open_tool_calls"], [])
        self.assertEqual(self.source.read_text(encoding="utf-8"), "answer = 1\n")

    def test_secret_rejection_and_unmatched_intent_prevent_repeat(self) -> None:
        secret = RunMemoryRecorder(
            root=self.root / "secret-memory",
            identity=RunIdentity(str(uuid.uuid4()), str(uuid.uuid4())),
            workspace=self.workspace,
            objective="password=abcdefghijk",
            model="fake-local-model",
            host_id="test-host",
        )
        with self.assertRaisesRegex(RunMemoryError, "secret-like"):
            secret.ensure_started()
        self.assertEqual(secret.store.verify().event_count, 0)

        self.recorder.ensure_started()
        self.recorder.record_model_intent(
            request_sha256=text_sha256("request"),
            prompt_sha256=text_sha256("prompt"),
            trajectory_path=self.trajectory,
        )
        with self.assertRaisesRegex(RunMemoryError, "not safe to repeat"):
            self.recorder.record_model_intent(
                request_sha256=text_sha256("request"),
                prompt_sha256=text_sha256("prompt"),
                trajectory_path=self.trajectory,
            )
        self.assertEqual(self.recorder.resume_classification(), "outcome_unknown")
        failed = copy.deepcopy(self.summary(status="failed"))
        failed["model_calls"] = 1
        failed["implementation"]["status"] = "failed"
        failed["implementation"]["response_observed"] = False
        failed["implementation"]["response_bytes"] = 0
        failed["implementation"]["http_status"] = 0
        failed["implementation"]["test_exit"] = -1
        failed["verifier"].update(
            status="not_run", verdict=None, reason_sha256=None, model_calls=0
        )
        self.recorder.finalize(failed)
        self.assertEqual(self.recorder.resume_classification(), "outcome_unknown")
        self.assertNotIn("model/response", [event["type"] for event in self.recorder._events()])
        with closing(self.recorder.store._connect()) as connection:
            state_json = connection.execute(
                "SELECT state_json FROM task_state WHERE task_id = ?",
                (self.identity.task_id,),
            ).fetchone()[0]
        self.assertEqual(
            json.loads(state_json)["open_tool_calls"],
            [
                {
                    "call_id": "model-call-1",
                    "outcome": "unknown",
                    "tool": "local-model-post",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
