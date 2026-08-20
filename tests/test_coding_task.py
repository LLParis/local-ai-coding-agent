from __future__ import annotations

import contextlib
import http.server
import json
import os
import socketserver
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]


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
        workspace.mkdir()
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
        edit = {
            "status": "verified",
            "model": "arm-qwen38-q6-text",
            "stage": str(root / "stage"),
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
        ]
        if plan_only:
            command.append("-PlanOnly")
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


if __name__ == "__main__":
    unittest.main()
