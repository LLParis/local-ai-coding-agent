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
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from agent_continuity.checkpoint import (  # noqa: E402
    CheckpointError,
    create_checkpoint,
    load_checkpoint,
    validate_checkpoint,
)
from agent_continuity.endpoint import EndpointError, check_endpoint  # noqa: E402
from agent_continuity.launcher import LaunchError, emergency_plan, primary_plan  # noqa: E402
from agent_continuity.tunnel import (  # noqa: E402
    TunnelError,
    TunnelSpec,
    ensure_tunnel,
    require_loopback_listener,
)


def task_file(root: Path, scope: list[str] | None = None) -> Path:
    path = root / "task.json"
    path.write_text(
        json.dumps(
            {
                "objective": "Continue the feature.",
                "state": "in_progress",
                "current_focus": "Build continuity.",
                "scope": scope or ["src"],
                "completed": [],
                "next": ["Verify it."],
                "constraints": ["Do not commit."],
                "decisions": [],
                "evidence": [],
            }
        )
    )
    return path


class CheckpointTests(unittest.TestCase):
    def test_no_git_checkpoint_is_deterministic_and_detects_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "feature.py").write_text("answer = 42\n")
            task = task_file(root)
            first = root / "one.json"
            second = root / "two.json"
            value = create_checkpoint(root, task, first)
            create_checkpoint(root, task, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(value["payload"]["workspace"]["vcs"], {"kind": "none"})
            self.assertEqual(value["payload"]["workspace"]["manifest"]["file_count"], 1)
            validate_checkpoint(first, root, require_current=True)
            (root / "src" / "feature.py").write_text("answer = 43\n")
            with self.assertRaisesRegex(CheckpointError, "stale"):
                validate_checkpoint(first, root, require_current=True)

    def test_tamper_and_secret_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "a").write_text("x")
            task = task_file(root)
            output = root / "checkpoint.json"
            create_checkpoint(root, task, output)
            data = json.loads(output.read_text())
            data["payload"]["task"]["objective"] = "tampered"
            output.write_text(json.dumps(data))
            with self.assertRaisesRegex(CheckpointError, "digest mismatch"):
                load_checkpoint(output)
            task.write_text(
                '{"objective":"api_key=abcdefghijk","state":"in_progress",'
                '"current_focus":"x","scope":["src"]}'
            )
            with self.assertRaisesRegex(CheckpointError, "secret-like"):
                create_checkpoint(root, task, root / "secret.json")

    def test_refuses_to_overwrite_different_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src" / "a").write_text("x")
            task = task_file(root)
            output = root / "checkpoint.json"
            output.write_text("user data")
            with self.assertRaisesRegex(CheckpointError, "refusing to overwrite"):
                create_checkpoint(root, task, output)
            self.assertEqual(output.read_text(), "user data")

    def test_git_context_records_worktree_without_diff_content(self) -> None:
        if not shutil_which("git"):
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
            (root / "src").mkdir()
            target = root / "src" / "a.txt"
            target.write_text("before")
            subprocess.run(["git", "-C", str(root), "add", "src/a.txt"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial"], check=True)
            target.write_text("SECRET DIFF CONTENT")
            value = create_checkpoint(root, task_file(root), root / "checkpoint.json")
            vcs = value["payload"]["workspace"]["vcs"]
            self.assertEqual(vcs["kind"], "git")
            self.assertEqual(vcs["changes"][0]["path"], "src/a.txt")
            self.assertNotIn("SECRET DIFF CONTENT", json.dumps(vcs))


class _APIHandler(http.server.BaseHTTPRequestHandler):
    model = "gpt-oss:20b"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/models":
            self._json({"data": [{"id": self.model, "object": "model"}]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        json.loads(self.rfile.read(length))
        if self.path == "/v1/responses":
            self._json({"output": [{"content": [{"type": "output_text", "text": "EXCALIBUR_OK"}]}]})
        else:
            self.send_error(404)

    def _json(self, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


@contextlib.contextmanager
def api_server(model: str = "gpt-oss:20b"):
    handler = type("Handler", (_APIHandler,), {"model": model})
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            thread.join()


class EndpointTests(unittest.TestCase):
    def test_models_and_responses_gate(self) -> None:
        with api_server() as port:
            report = check_endpoint(f"http://127.0.0.1:{port}/v1", "gpt-oss:20b", timeout=2)
            self.assertEqual(report.model_count, 1)
            self.assertIsNotNone(report.response_latency_ms)

    def test_rejects_non_loopback_and_missing_model(self) -> None:
        with self.assertRaisesRegex(EndpointError, "loopback"):
            check_endpoint("http://192.0.2.1:11434/v1", "gpt-oss:20b")
        with api_server("another") as port:
            with self.assertRaisesRegex(EndpointError, "not loaded"):
                check_endpoint(f"http://127.0.0.1:{port}/v1", "gpt-oss:20b", timeout=2)


class TunnelTests(unittest.TestCase):
    @mock.patch("agent_continuity.tunnel.listener_names", return_value=["*:12434"])
    def test_rejects_public_listener(self, _listeners: mock.Mock) -> None:
        with self.assertRaisesRegex(TunnelError, "exposed beyond loopback"):
            require_loopback_listener(12434)

    @mock.patch("agent_continuity.tunnel._run")
    def test_rejects_non_ssh_listener(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "cpython3\nn127.0.0.1:12434\n", "")
        with self.assertRaisesRegex(TunnelError, "not owned by an SSH forward"):
            require_loopback_listener(12434)

    @mock.patch("agent_continuity.tunnel.check_endpoint")
    @mock.patch("agent_continuity.tunnel.listener_names", return_value=["127.0.0.1:12434"])
    @unittest.skipUnless(sys.platform == "darwin", "SSH tunnel ownership is a macOS edge contract")
    def test_reuses_existing_healthy_tunnel(self, _listeners: mock.Mock, endpoint: mock.Mock) -> None:
        endpoint.return_value = mock.Mock(model="gpt-oss:20b")
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"CODING_INTELLIGENCE_RUNTIME_DIR": temporary}):
                report = ensure_tunnel(TunnelSpec())
        self.assertEqual(report.action, "reused")
        endpoint.assert_called_once()


class LauncherTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        (root / "src").mkdir()
        (root / "src" / "a").write_text("x")
        checkpoint = root / "checkpoint.json"
        create_checkpoint(root, task_file(root), checkpoint)
        return root, checkpoint

    @mock.patch("agent_continuity.launcher._codex_binary", return_value="/usr/bin/codex")
    @mock.patch("agent_continuity.launcher._validate_profile")
    @mock.patch("agent_continuity.launcher.ensure_tunnel")
    def test_primary_is_named_profile_workspace_write_never(
        self, tunnel: mock.Mock, _profile: mock.Mock, _binary: mock.Mock
    ) -> None:
        tunnel.return_value = mock.Mock(action="reused", endpoint=mock.Mock(model="gpt-oss:20b"))
        with tempfile.TemporaryDirectory() as temporary:
            workspace, checkpoint = self._fixture(Path(temporary))
            plan = primary_plan(workspace=workspace, checkpoint_path=checkpoint, task="Continue.")
        self.assertIn("excalibur-local", plan.command)
        self.assertIn("workspace-write", plan.command)
        self.assertIn("never", plan.command)

    @mock.patch("agent_continuity.launcher._codex_binary", return_value="/usr/bin/codex")
    @mock.patch("agent_continuity.launcher._validate_profile")
    @mock.patch("agent_continuity.launcher.check_endpoint")
    def test_mac_requires_literal_confirmation_and_is_read_only(
        self, endpoint: mock.Mock, _profile: mock.Mock, _binary: mock.Mock
    ) -> None:
        endpoint.return_value = mock.Mock(model="qwen3.5:4b")
        with tempfile.TemporaryDirectory() as temporary:
            workspace, checkpoint = self._fixture(Path(temporary))
            with self.assertRaisesRegex(LaunchError, "EXPLICIT-TRIAGE-ONLY"):
                emergency_plan(
                    workspace=workspace,
                    checkpoint_path=checkpoint,
                    task="Diagnose.",
                    confirmation="yes",
                )
            plan = emergency_plan(
                workspace=workspace,
                checkpoint_path=checkpoint,
                task="Diagnose.",
                confirmation="EXPLICIT-TRIAGE-ONLY",
            )
        self.assertIn("mac-emergency-triage", plan.command)
        self.assertIn("read-only", plan.command)
        self.assertIn("never", plan.command)


def shutil_which(command: str) -> str | None:
    import shutil

    return shutil.which(command)


if __name__ == "__main__":
    unittest.main()
