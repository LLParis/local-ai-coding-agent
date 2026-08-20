from __future__ import annotations

import contextlib
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from agent_continuity.local_edit import LocalEditError, run_local_edit  # noqa: E402


class _EditHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        request_document = json.loads(self.rfile.read(length))
        self.server.requests.append(request_document)  # type: ignore[attr-defined]
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        content = json.dumps(self.server.model_document)  # type: ignore[attr-defined]
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
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
def edit_server(model_document: object):
    with _ThreadingServer(("127.0.0.1", 0), _EditHandler) as server:
        server.model_document = model_document  # type: ignore[attr-defined]
        server.requests = []  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def _workspace_fixture(root: Path) -> tuple[Path, str]:
    workspace = root / "workspace"
    (workspace / "src" / "tests").mkdir(parents=True)
    source = "def add(left, right):\n    return left - right\n"
    (workspace / "src" / "calculator.py").write_text(source, encoding="utf-8")
    verifier = (
        "# VERIFIER_ONLY_SENTINEL_54d971\n"
        "import sys\n"
        "import unittest\n"
        "from pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "from calculator import add\n\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n"
    )
    (workspace / "src" / "tests" / "test_calculator.py").write_text(verifier, encoding="utf-8")
    return workspace, source


def _valid_model_document() -> dict[str, object]:
    return {
        "diagnosis": "add subtracts the right operand",
        "edits": [
            {
                "path": "src/calculator.py",
                "old_text": "return left - right",
                "new_text": "return left + right",
            }
        ],
    }


class LocalEditContractTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows wrapper proof runs on Windows")
    def test_windows_wrapper_hides_verifier_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace, original_source = _workspace_fixture(Path(temporary))
            with edit_server(_valid_model_document()) as server:
                port = server.server_address[1]
                environment = {
                    **os.environ,
                    "CODING_INTELLIGENCE_PYTHON": sys.executable,
                }
                result = subprocess.run(
                    [
                        str(PACKAGE / "bin" / "continuity.cmd"),
                        "local-edit",
                        "--workspace",
                        str(workspace),
                        "--objective",
                        "Repair addition and prove it with the authoritative test.",
                        "--mutable",
                        "src/calculator.py",
                        "--context",
                        "src",
                        "--verify-context",
                        "src/tests",
                        "--base-url",
                        f"http://127.0.0.1:{port}/v1",
                        "--model",
                        "fake-local-model",
                        "--timeout",
                        "10",
                        "--",
                        sys.executable,
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "src/tests",
                        "-v",
                    ],
                    cwd=PACKAGE,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                stage = Path(report["stage"])
                self.addCleanup(shutil.rmtree, stage, ignore_errors=True)
                self.assertEqual(report["status"], "verified")
                self.assertEqual(report["model_calls"], 1)
                trajectory = Path(report["trajectory"])
                self.assertTrue(trajectory.is_file())
                trajectory_document = json.loads(trajectory.read_text(encoding="utf-8"))
                self.assertEqual(trajectory_document["model_calls"], 1)
                self.assertNotIn(
                    "VERIFIER_ONLY_SENTINEL_54d971",
                    json.dumps(trajectory_document["request"]),
                )
                self.assertEqual(len(server.requests), 1)  # type: ignore[attr-defined]
                request_text = json.dumps(server.requests[0])  # type: ignore[attr-defined]
                self.assertIn("return left - right", request_text)
                self.assertNotIn("VERIFIER_ONLY_SENTINEL_54d971", request_text)
                self.assertTrue((stage / "src" / "tests" / "test_calculator.py").is_file())
                self.assertIn("return left + right", (stage / "src" / "calculator.py").read_text())
                self.assertEqual(
                    (workspace / "src" / "calculator.py").read_text(encoding="utf-8"),
                    original_source,
                )

    def test_runtime_enforces_string_diagnosis_when_server_ignores_schema(self) -> None:
        document = _valid_model_document()
        document["diagnosis"] = 7
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            workspace, original_source = _workspace_fixture(base)
            stage = base / "stage"
            with (
                edit_server(document) as server,
                mock.patch("agent_continuity.local_edit.tempfile.mkdtemp", return_value=str(stage)),
            ):
                with self.assertRaisesRegex(LocalEditError, "diagnosis must be"):
                    run_local_edit(
                        workspace=workspace,
                        objective="Repair addition.",
                        mutable=[Path("src/calculator.py")],
                        context=[Path("src")],
                        verify_context=[Path("src/tests")],
                        test_command=[sys.executable, "-c", "raise SystemExit(0)"],
                        base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
                        model="fake-local-model",
                        timeout=5,
                    )
                self.assertEqual(len(server.requests), 1)  # type: ignore[attr-defined]
                self.assertEqual(
                    (workspace / "src" / "calculator.py").read_text(encoding="utf-8"),
                    original_source,
                )

    def test_runtime_enforces_one_to_four_edits_when_server_ignores_schema(self) -> None:
        for edit_count in (0, 5):
            with self.subTest(edit_count=edit_count), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                workspace, original_source = _workspace_fixture(base)
                document = _valid_model_document()
                document["edits"] = document["edits"] * edit_count  # type: ignore[operator]
                stage = base / "stage"
                with (
                    edit_server(document) as server,
                    mock.patch(
                        "agent_continuity.local_edit.tempfile.mkdtemp", return_value=str(stage)
                    ),
                ):
                    with self.assertRaisesRegex(LocalEditError, "between 1 and 4 edits"):
                        run_local_edit(
                            workspace=workspace,
                            objective="Repair addition.",
                            mutable=[Path("src/calculator.py")],
                            context=[Path("src")],
                            verify_context=[Path("src/tests")],
                            test_command=[sys.executable, "-c", "raise SystemExit(0)"],
                            base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
                            model="fake-local-model",
                            timeout=5,
                        )
                    self.assertEqual(len(server.requests), 1)  # type: ignore[attr-defined]
                    self.assertEqual(
                        (workspace / "src" / "calculator.py").read_text(encoding="utf-8"),
                        original_source,
                    )


if __name__ == "__main__":
    unittest.main()
