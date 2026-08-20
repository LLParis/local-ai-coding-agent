from __future__ import annotations

import contextlib
import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from agent_continuity.run_memory import RunIdentity, RunMemoryRecorder  # noqa: E402


class _VerifierHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        request = json.loads(raw)
        self.server.requests.append(request)  # type: ignore[attr-defined]
        self.server.raw_requests.append(raw)  # type: ignore[attr-defined]
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        content = json.dumps(self.server.verdict)  # type: ignore[attr-defined]
        body = json.dumps(
            {"message": {"role": "assistant", "content": content}, "done": True}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextlib.contextmanager
def verifier_server(verdict: object):
    with _ThreadingServer(("127.0.0.1", 0), _VerifierHandler) as server:
        server.verdict = verdict  # type: ignore[attr-defined]
        server.requests = []  # type: ignore[attr-defined]
        server.raw_requests = []  # type: ignore[attr-defined]
        server.paths = []  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


@unittest.skipUnless(os.name == "nt", "the everyday command is a Windows PowerShell entry point")
class CodingTaskVerifierTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, dict[str, object]]:
        workspace = root / "workspace"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "calculator.py").write_text(
            "def add(left, right):\n    return left - right\n", encoding="utf-8"
        )
        task = root / "task.json"
        task.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "backend": "Qwen38",
                    "workspace": str(workspace),
                    "objective": "Repair addition without changing hidden tests.",
                    "mutable": ["src/calculator.py"],
                    "context": ["src/calculator.py"],
                    "verify_context": ["tests"],
                    "test_command": ["py", "-3", "-m", "unittest"],
                    "timeout": 10,
                }
            ),
            encoding="utf-8",
        )
        switch_log = root / "switch.log"
        switcher = root / "fake-switcher.ps1"
        switcher.write_text(
            "param([string]$Backend)\n"
            "[IO.File]::AppendAllText($env:CODING_TASK_SWITCH_LOG, "
            "$Backend + [Environment]::NewLine)\n"
            "[pscustomobject]@{status='ready';backend=$Backend;uacPrompt=$false} "
            "| ConvertTo-Json -Compress\n",
            encoding="utf-8",
        )
        continuity = root / "fake-continuity.cmd"
        continuity.write_text(
            '@echo off\r\ntype "%CODING_TASK_EDIT_REPORT%"\r\nexit /b 0\r\n',
            encoding="utf-8",
        )
        stage = root / "stage"
        stage.mkdir()
        (stage / "preserved.txt").write_text("preserve me\n", encoding="utf-8")
        edit = {
            "status": "verified",
            "model": "arm-qwen38-q6-text",
            "stage": str(stage),
            "diagnosis": "add subtracts",
            "diff": (
                "--- a/src/calculator.py\n"
                "+++ b/src/calculator.py\n"
                "-return left - right\n"
                "+return left + right\n"
            ),
            "test_command": ["py", "-3", "-m", "unittest"],
            "test_exit": 0,
            "test_output": "test_add ... ok — café 🧪\n",
            "response_observed": True,
            "model_calls": 1,
        }
        report_path = root / "edit.json"
        report_path.write_text(json.dumps(edit), encoding="utf-8")
        return (
            task,
            switcher,
            continuity,
            {
                "CODING_TASK_SWITCH_LOG": str(switch_log),
                "CODING_TASK_EDIT_REPORT": str(report_path),
                "CODING_TASK_MEMORY_ROOT": str(root / "memory"),
                "CODING_TASK_RUN_TASK_ID": "00000000-0000-4000-8000-000000000101",
                "CODING_TASK_RUN_SESSION_ID": "00000000-0000-4000-8000-000000000102",
                "CODING_INTELLIGENCE_PYTHON": sys.executable,
            },
        )

    def _run(
        self,
        task: Path,
        switcher: Path,
        continuity: Path,
        environment: dict[str, object],
        *,
        verifier_uri: str,
        plan_only: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PACKAGE / "bin" / "coding-task.ps1"),
            "-Task",
            str(task),
            "-SwitcherPath",
            str(switcher),
            "-ContinuityPath",
            str(continuity),
            "-VerifierUri",
            verifier_uri,
            "-MemoryRoot",
            str(environment["CODING_TASK_MEMORY_ROOT"]),
            "-RunTaskId",
            str(environment["CODING_TASK_RUN_TASK_ID"]),
            "-RunSessionId",
            str(environment["CODING_TASK_RUN_SESSION_ID"]),
        ]
        if plan_only:
            command.append("-PlanOnly")
        if "CODING_TASK_MEMORY_CONTINUITY" in environment:
            command.extend(
                ["-MemoryContinuityPath", str(environment["CODING_TASK_MEMORY_CONTINUITY"])]
            )
        return subprocess.run(
            command,
            cwd=PACKAGE,
            env={**os.environ, **{key: str(value) for key, value in environment.items()}},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_accept_calls_devstral_once_and_restores_qwen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, switcher, continuity, environment = self._fixture(root)
            verdict = {"verdict": "accept", "reason": "Patch is correct.", "risks": []}
            with verifier_server(verdict) as server:
                result = self._run(
                    task,
                    switcher,
                    continuity,
                    environment,
                    verifier_uri=f"http://127.0.0.1:{server.server_address[1]}/api/chat",
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "verified")
            self.assertEqual(report["commandExit"], 0)
            self.assertEqual(report["processExit"], 0)
            self.assertEqual(report["modelCalls"], 2)
            self.assertEqual(report["verifier"]["status"], "accepted")
            self.assertEqual(report["verifier"]["model"], "devstral-small-2:24b")
            self.assertEqual(report["restore"]["backend"], "Qwen38")
            self.assertFalse(report["restore"]["uacPrompt"])
            self.assertEqual(
                (root / "switch.log").read_text(encoding="utf-8").splitlines(),
                ["Qwen38", "Ollama", "Qwen38"],
            )
            self.assertEqual(len(server.requests), 1)  # type: ignore[attr-defined]
            request = server.requests[0]  # type: ignore[attr-defined]
            self.assertEqual(request["model"], "devstral-small-2:24b")
            self.assertEqual(request["format"]["type"], "object")
            self.assertFalse(request["format"]["additionalProperties"])
            self.assertEqual(request["options"], {"temperature": 0, "num_predict": 1024})
            self.assertFalse(request["stream"])
            self.assertNotIn("response_format", request)
            self.assertEqual(server.paths, ["/api/chat"])  # type: ignore[attr-defined]
            server.raw_requests[0].decode("utf-8", errors="strict")  # type: ignore[attr-defined]
            prompt = request["messages"][1]["content"]
            self.assertIn("Repair addition", prompt)
            self.assertIn("return left + right", prompt)
            self.assertIn("test_add ... ok — café 🧪", prompt)
            self.assertNotIn("add subtracts", prompt)
            self.assertNotIn(str(root / "stage"), prompt)
            self.assertNotIn("VERIFIER_ONLY_SENTINEL", prompt)
            recorder = RunMemoryRecorder(
                root=Path(str(environment["CODING_TASK_MEMORY_ROOT"])),
                identity=RunIdentity(
                    str(environment["CODING_TASK_RUN_TASK_ID"]),
                    str(environment["CODING_TASK_RUN_SESSION_ID"]),
                ),
                workspace=root / "workspace",
                objective="Repair addition without changing hidden tests.",
                model="arm-qwen38-q6-text",
                host_id="test-host",
            )
            events = recorder._events()
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
            episode = recorder.store.get(
                f"episode:{environment['CODING_TASK_RUN_TASK_ID']}"
            )
            self.assertEqual(len(episode["evidence"]), 2)
            raw_events = "".join(
                path.read_text(encoding="utf-8")
                for path in Path(str(environment["CODING_TASK_MEMORY_ROOT"])).glob(
                    "events/*/*.jsonl"
                )
            )
            self.assertNotIn("return left + right", raw_events)
            self.assertNotIn("test_add ... ok", raw_events)
            self.assertEqual(
                (root / "workspace" / "src" / "calculator.py").read_text(encoding="utf-8"),
                "def add(left, right):\n    return left - right\n",
            )

    def test_reject_is_a_truthful_terminal_result_and_restores_qwen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, switcher, continuity, environment = self._fixture(root)
            verdict = {
                "verdict": "reject",
                "reason": "The diff does not fully satisfy the objective.",
                "risks": ["Behavior remains incomplete."],
            }
            with verifier_server(verdict) as server:
                result = self._run(
                    task,
                    switcher,
                    continuity,
                    environment,
                    verifier_uri=f"http://127.0.0.1:{server.server_address[1]}/api/chat",
                )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "rejected")
            self.assertEqual(report["commandExit"], 0)
            self.assertEqual(report["processExit"], 1)
            self.assertEqual(report["verifier"]["verdict"], "reject")
            self.assertEqual(report["verifier"]["modelCalls"], 1)
            self.assertEqual(report["modelCalls"], 2)
            self.assertEqual(report["restore"]["backend"], "Qwen38")
            self.assertEqual(len(server.requests), 1)  # type: ignore[attr-defined]

    def test_invalid_verdict_fails_truthfully_without_retry_and_restores_qwen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, switcher, continuity, environment = self._fixture(root)
            invalid = {"verdict": "accept", "reason": "Looks fine.", "risks": [], "extra": True}
            with verifier_server(invalid) as server:
                result = self._run(
                    task,
                    switcher,
                    continuity,
                    environment,
                    verifier_uri=f"http://127.0.0.1:{server.server_address[1]}/api/chat",
                )

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["commandExit"], 0)
            self.assertEqual(report["processExit"], 1)
            self.assertEqual(report["modelCalls"], 2)
            self.assertEqual(report["automaticRetries"], 0)
            self.assertEqual(report["verifier"]["status"], "failed")
            self.assertIn("contain only", report["verifier"]["reason"])
            self.assertEqual(len(server.requests), 1)  # type: ignore[attr-defined]
            self.assertEqual(
                (root / "switch.log").read_text(encoding="utf-8").splitlines(),
                ["Qwen38", "Ollama", "Qwen38"],
            )

    def test_verdict_text_limits_are_enforced_at_runtime(self) -> None:
        invalid_verdicts = (
            ({"verdict": "accept", "reason": "x" * 2001, "risks": []}, "2000"),
            ({"verdict": "accept", "reason": "Fine.", "risks": ["x" * 1001]}, "1000"),
        )
        for verdict, expected_error in invalid_verdicts:
            with (
                self.subTest(expected_error=expected_error),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                task, switcher, continuity, environment = self._fixture(root)
                with verifier_server(verdict) as server:
                    result = self._run(
                        task,
                        switcher,
                        continuity,
                        environment,
                        verifier_uri=(f"http://127.0.0.1:{server.server_address[1]}/api/chat"),
                    )

                self.assertNotEqual(result.returncode, 0)
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "failed")
                self.assertIn(expected_error, report["verifier"]["reason"])
                self.assertEqual(report["modelCalls"], 2)
                self.assertEqual(report["automaticRetries"], 0)
                self.assertEqual(len(server.requests), 1)  # type: ignore[attr-defined]

    def test_plan_only_makes_no_backend_or_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, switcher, continuity, environment = self._fixture(root)
            result = self._run(
                task,
                switcher,
                continuity,
                environment,
                verifier_uri="http://127.0.0.1:9/api/chat",
                plan_only=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "planned")
            self.assertEqual(report["modelCalls"], 0)
            self.assertEqual(report["automaticRetries"], 0)
            self.assertFalse((root / "switch.log").exists())
            self.assertFalse(Path(str(environment["CODING_TASK_MEMORY_ROOT"])).exists())

    def test_failed_project_test_skips_verifier_but_still_restores_qwen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, switcher, continuity, environment = self._fixture(root)
            report_path = Path(str(environment["CODING_TASK_EDIT_REPORT"]))
            edit = json.loads(report_path.read_text(encoding="utf-8"))
            edit.update(status="failed", test_exit=1, test_output="test_add ... FAIL\n")
            report_path.write_text(json.dumps(edit), encoding="utf-8")

            result = self._run(
                task,
                switcher,
                continuity,
                environment,
                verifier_uri="http://127.0.0.1:9/api/chat",
            )

            self.assertNotEqual(result.returncode, 0)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["modelCalls"], 1)
            self.assertEqual(report["verifier"]["status"], "not_run")
            self.assertEqual(report["verifier"]["modelCalls"], 0)
            self.assertEqual(report["processExit"], 1)
            self.assertEqual(
                (root / "switch.log").read_text(encoding="utf-8").splitlines(),
                ["Qwen38", "Qwen38"],
            )

    def test_pre_model_memory_failure_prevents_backend_and_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, switcher, continuity, environment = self._fixture(root)
            failing_memory = root / "fail-memory.cmd"
            failing_memory.write_text(
                "@echo off\r\necho forced pre-model memory failure 1>&2\r\nexit /b 9\r\n",
                encoding="utf-8",
            )
            environment["CODING_TASK_MEMORY_CONTINUITY"] = str(failing_memory)

            result = self._run(
                task,
                switcher,
                continuity,
                environment,
                verifier_uri="http://127.0.0.1:9/api/chat",
            )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["modelCalls"], 0)
            self.assertEqual(report["automaticRetries"], 0)
            self.assertIn("pre-model memory checkpoint failed", report["rawFailure"])
            self.assertFalse((root / "switch.log").exists())
            self.assertEqual(report["memory"]["status"], "failed")

    def test_terminal_memory_failure_fails_truthfully_and_preserves_edit_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task, switcher, continuity, environment = self._fixture(root)
            terminal_failure = root / "terminal-memory.cmd"
            real_continuity = PACKAGE / "bin" / "continuity.cmd"
            terminal_failure.write_text(
                "@echo off\r\n"
                'if /i "%~1"=="run-memory-finalize" (\r\n'
                "  echo forced terminal memory failure 1>&2\r\n"
                "  exit /b 9\r\n"
                ")\r\n"
                f'call "{real_continuity}" %*\r\n'
                "exit /b %errorlevel%\r\n",
                encoding="utf-8",
            )
            environment["CODING_TASK_MEMORY_CONTINUITY"] = str(terminal_failure)
            verdict = {"verdict": "accept", "reason": "Patch is correct.", "risks": []}
            with verifier_server(verdict) as server:
                result = self._run(
                    task,
                    switcher,
                    continuity,
                    environment,
                    verifier_uri=f"http://127.0.0.1:{server.server_address[1]}/api/chat",
                )

            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["modelCalls"], 2)
            self.assertEqual(report["automaticRetries"], 0)
            self.assertEqual(report["edit"]["stage"], str(root / "stage"))
            self.assertEqual(
                (root / "stage" / "preserved.txt").read_text(encoding="utf-8"),
                "preserve me\n",
            )
            self.assertIn("terminal memory finalization failed", report["rawFailure"])
            self.assertEqual(report["memory"]["status"], "failed")
            self.assertEqual(len(server.requests), 1)  # type: ignore[attr-defined]
            self.assertEqual(
                (root / "switch.log").read_text(encoding="utf-8").splitlines(),
                ["Qwen38", "Ollama", "Qwen38"],
            )


if __name__ == "__main__":
    unittest.main()
