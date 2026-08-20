from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MAC = _load_module("mac_swift_verifier", PACKAGE / "bin" / "mac-swift-verifier.py")
REMOTE = _load_module("mac_swift_remote", PACKAGE / "mac" / "swift-verifier-remote.py")


class _VerifierHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        self.server.requests.append(request)  # type: ignore[attr-defined]
        content = json.dumps(
            {"verdict": "accept", "reason": "Mac Swift evidence passed.", "risks": []}
        )
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
def verifier_server():
    with _ThreadingServer(("127.0.0.1", 0), _VerifierHandler) as server:
        server.requests = []  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def _swift_fixture(root: Path) -> tuple[Path, Path, dict[str, object], str]:
    workspace = root / "workspace"
    source = workspace / "Sources" / "ContinuityFixture" / "BackendState.swift"
    hidden = workspace / "Tests" / "ContinuityFixtureTests" / "BackendStateTests.swift"
    source.parent.mkdir(parents=True)
    hidden.parent.mkdir(parents=True)
    (workspace / "Package.swift").write_text(
        "// swift-tools-version: 6.0\nimport PackageDescription\n",
        encoding="utf-8",
    )
    source.write_text("public func ready() -> Bool { true }\n", encoding="utf-8")
    hidden.write_text("import Testing\n@Test func readiness() {}\n", encoding="utf-8")
    (workspace / "not-selected.txt").write_text("must not transfer\n", encoding="utf-8")
    task_value: dict[str, object] = {
        "schema_version": 1,
        "backend": "Qwen38",
        "workspace": str(workspace),
        "objective": "Verify the staged Swift ownership correction.",
        "mutable": ["Sources/ContinuityFixture/BackendState.swift"],
        "context": ["Package.swift", "Sources"],
        "verify_context": ["Tests"],
        "test_command": ["swift", "test"],
        "timeout": 90,
        "execution_verifier": "mac-swift",
    }
    task = root / "task.json"
    task.write_text(json.dumps(task_value), encoding="utf-8")
    task_sha256 = MAC._sha256(task.read_bytes())
    return workspace, task, task_value, task_sha256


def _valid_remote(
    manifest: dict[str, object], manifest_sha256: str, archive_sha256: str
) -> dict[str, object]:
    return {
        "schema": MAC.REMOTE_SCHEMA,
        "status": "verified",
        "automatic_retries": 0,
        "archive_sha256": archive_sha256,
        "manifest_sha256": manifest_sha256,
        "stage_manifest_sha256": manifest["stage_manifest_sha256"],
        "source_snapshot_sha256": manifest["source_snapshot_sha256"],
        "task_sha256": manifest["task_sha256"],
        "identity": {"matched": True},
        "test": {
            "argv": [MAC.EXPECTED_MAC["swift_path"], *manifest["command"][1:]],
            "exit_code": 0,
            "timed_out": False,
            "output_limit_exceeded": False,
            "owned_process_group_residual": False,
            "stdout": {
                "bytes": 2,
                "sha256": MAC._sha256(b"ok"),
                "text": "ok",
                "truncated": False,
            },
            "stderr": {
                "bytes": 0,
                "sha256": MAC._sha256(b""),
                "text": "",
                "truncated": False,
            },
        },
        "cleanup": {"succeeded": True, "post_exists": False},
    }


class MacSwiftHelperTests(unittest.TestCase):
    def test_archive_contains_only_explicit_hash_bound_stage_and_hidden_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, task_path, _, task_sha256 = _swift_fixture(root)
            task, _ = MAC._load_task(task_path, task_sha256)
            source_snapshot = MAC._snapshot(workspace, task, task_sha256)
            archive, manifest, manifest_sha256 = MAC._build_archive(
                task=task,
                task_sha256=task_sha256,
                source_snapshot_sha256=source_snapshot["manifest_sha256"],
                stage=workspace,
                timeout_seconds=60,
            )

            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                names = set(bundle.namelist())
                self.assertIn("manifest.json", names)
                self.assertIn("manifest.sha256", names)
                self.assertIn("payload/Tests/ContinuityFixtureTests/BackendStateTests.swift", names)
                self.assertNotIn("payload/not-selected.txt", names)
                self.assertEqual(bundle.read("manifest.sha256").decode(), manifest_sha256)
            self.assertEqual(
                sum(item["role"] == "hidden_verifier" for item in manifest["files"]),
                1,
            )

    def test_traversal_drive_unc_and_unsafe_test_argv_fail_before_ssh(self) -> None:
        for value in ("../escape", "/absolute", r"C:\escape", r"\\host\share"):
            with self.subTest(value=value):
                with self.assertRaises(MAC.MacVerifierError):
                    MAC._relative(value, "path")
        for command in (
            ["swift", "test; rm -rf /"],
            ["swift", "test", "--package-path", "../escape"],
            ["swift", "test", "--disable-sandbox"],
        ):
            with self.subTest(command=command), self.assertRaises(MAC.MacVerifierError):
                MAC._command(command)

    def test_secret_like_source_is_refused_before_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, task_path, _, task_sha256 = _swift_fixture(root)
            (workspace / "Sources" / "ContinuityFixture" / "BackendState.swift").write_text(
                'let api_key = "1234567890abcdef"\n', encoding="utf-8"
            )
            task, _ = MAC._load_task(task_path, task_sha256)
            with self.assertRaisesRegex(MAC.MacVerifierError, "secret-like"):
                MAC._snapshot(workspace, task, task_sha256)

    def test_symlink_or_reparse_escape_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, task_path, task_value, task_sha256 = _swift_fixture(root)
            outside = root / "outside"
            outside.mkdir()
            link = workspace / "Sources" / "Linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:  # Windows may withhold symlink privilege
                self.assertTrue(str(exc))
                task, _ = MAC._load_task(task_path, task_sha256)
                with mock.patch.object(MAC, "_is_reparse", return_value=True):
                    with self.assertRaisesRegex(MAC.MacVerifierError, "real directory"):
                        MAC._snapshot(workspace, task, task_sha256)
                return
            task, _ = MAC._load_task(task_path, task_sha256)
            with self.assertRaisesRegex(MAC.MacVerifierError, "symlink or reparse"):
                MAC._snapshot(workspace, task, task_sha256)
            self.assertEqual(task_value["execution_verifier"], "mac-swift")

    def test_remote_result_fails_closed_on_hash_identity_output_or_cleanup_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, task_path, _, task_sha256 = _swift_fixture(root)
            task, _ = MAC._load_task(task_path, task_sha256)
            snapshot = MAC._snapshot(workspace, task, task_sha256)
            archive, manifest, manifest_sha256 = MAC._build_archive(
                task=task,
                task_sha256=task_sha256,
                source_snapshot_sha256=snapshot["manifest_sha256"],
                stage=workspace,
                timeout_seconds=60,
            )
            archive_sha256 = MAC._sha256(archive)
            transport = {
                "exit_code": 0,
                "remote": _valid_remote(manifest, manifest_sha256, archive_sha256),
            }
            MAC._validate_remote(
                transport,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                archive_sha256=archive_sha256,
            )
            mutations = (
                (
                    "archive hash",
                    lambda value: value.update(archive_sha256=MAC._sha256(b"other")),
                ),
                ("task hash", lambda value: value.update(task_sha256=MAC._sha256(b"other"))),
                ("identity", lambda value: value["identity"].update(matched=False)),
                (
                    "output",
                    lambda value: value["test"]["stdout"].update(truncated=True),
                ),
                (
                    "residual process group",
                    lambda value: value["test"].update(owned_process_group_residual=True),
                ),
                ("cleanup", lambda value: value["cleanup"].update(succeeded=False)),
            )
            for label, mutate in mutations:
                with self.subTest(label=label):
                    changed = json.loads(json.dumps(transport))
                    mutate(changed["remote"])
                    with self.assertRaises(MAC.MacVerifierError):
                        MAC._validate_remote(
                            changed,
                            manifest=manifest,
                            manifest_sha256=manifest_sha256,
                            archive_sha256=archive_sha256,
                        )

    def test_stage_mutation_after_transfer_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, task_path, _, task_sha256 = _swift_fixture(root)
            stage = root / "stage"
            shutil.copytree(workspace, stage)
            task, _ = MAC._load_task(task_path, task_sha256)
            snapshot = MAC._snapshot(workspace, task, task_sha256)

            def fake_ssh(archive: bytes, _: int) -> dict[str, object]:
                with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                    manifest_raw = bundle.read("manifest.json")
                manifest = json.loads(manifest_raw)
                (stage / "Sources/ContinuityFixture/BackendState.swift").write_text(
                    "public func changedConcurrently() {}\n", encoding="utf-8"
                )
                return {
                    "exit_code": 0,
                    "remote": _valid_remote(
                        manifest, MAC._sha256(manifest_raw), MAC._sha256(archive)
                    ),
                }

            with (
                mock.patch.object(MAC, "_invoke_ssh", side_effect=fake_ssh),
                self.assertRaisesRegex(MAC.MacVerifierError, "staged files mutated"),
            ):
                MAC.run_verification(
                    task_path=task_path,
                    expected_task_sha256=task_sha256,
                    stage=stage,
                    expected_source_snapshot_sha256=snapshot["manifest_sha256"],
                    timeout_seconds=60,
                )

    def test_remote_extractor_rejects_unmanifested_member_and_cleans_exact_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, task_path, _, task_sha256 = _swift_fixture(root)
            task, _ = MAC._load_task(task_path, task_sha256)
            snapshot = MAC._snapshot(workspace, task, task_sha256)
            archive, _, _ = MAC._build_archive(
                task=task,
                task_sha256=task_sha256,
                source_snapshot_sha256=snapshot["manifest_sha256"],
                stage=workspace,
                timeout_seconds=60,
            )
            tampered = io.BytesIO()
            with (
                zipfile.ZipFile(io.BytesIO(archive)) as source,
                zipfile.ZipFile(tampered, "w") as target,
            ):
                for item in source.infolist():
                    target.writestr(item, source.read(item.filename))
                target.writestr("payload/../escape", b"bad")
            destination = root / "remote"
            destination.mkdir()
            with self.assertRaisesRegex(REMOTE.VerificationError, "members differ"):
                REMOTE._extract(tampered.getvalue(), destination)


@unittest.skipUnless(os.name == "nt", "coding-task.ps1 is the Windows entry point")
class CodingTaskMacIntegrationTests(unittest.TestCase):
    def _run(
        self,
        *,
        root: Path,
        task: Path,
        edit_report: dict[str, object],
        verifier_uri: str,
        plan_only: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        edit_path = root / "edit.json"
        edit_path.write_text(json.dumps(edit_report), encoding="utf-8")
        switcher = root / "switcher.ps1"
        switcher.write_text(
            "param([string]$Backend)\n"
            "[IO.File]::AppendAllText($env:MAC_SWITCH_LOG,$Backend+[Environment]::NewLine)\n"
            "[pscustomobject]@{status='ready';backend=$Backend;uacPrompt=$false}|"
            "ConvertTo-Json -Compress\n",
            encoding="utf-8",
        )
        continuity = root / "continuity.cmd"
        continuity.write_text(
            '@echo off\r\necho %* > "%MAC_CONTINUITY_ARGS%"\r\n'
            'type "%MAC_EDIT_REPORT%"\r\nexit /b 0\r\n',
            encoding="utf-8",
        )
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
            "-MacVerifierPath",
            str(PACKAGE / "bin" / "mac-swift-verifier.py"),
            "-VerifierUri",
            verifier_uri,
            "-MemoryRoot",
            str(root / "memory"),
            "-RunTaskId",
            "00000000-0000-4000-8000-000000000201",
            "-RunSessionId",
            "00000000-0000-4000-8000-000000000202",
        ]
        if plan_only:
            command.append("-PlanOnly")
        return subprocess.run(
            command,
            cwd=PACKAGE,
            env={
                **os.environ,
                "CODING_INTELLIGENCE_PYTHON": sys.executable,
                "MAC_SWITCH_LOG": str(root / "switch.log"),
                "MAC_CONTINUITY_ARGS": str(root / "continuity-args.txt"),
                "MAC_EDIT_REPORT": str(edit_path),
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def _edit_report(self, root: Path, task_sha256: str, snapshot_sha256: str) -> dict[str, object]:
        mac_result = {
            "schema": MAC.RESULT_SCHEMA,
            "status": "verified",
            "model_calls": 0,
            "automatic_retries": 0,
            "checkpoint": {
                "task_sha256": task_sha256,
                "source_snapshot_before_sha256": snapshot_sha256,
                "source_snapshot_after_sha256": snapshot_sha256,
                "source_workspace_mutated": False,
                "stage_manifest_sha256": MAC._sha256(b"stage"),
                "stage_snapshot_after_sha256": MAC._sha256(b"stage"),
            },
            "transport": {
                "ssh_sessions": 1,
                "connection_attempts": 1,
                "automatic_retries": 0,
                "exit_code": 0,
            },
            "result": {
                "status": "verified",
                "identity": {"matched": True},
                "test": {
                    "exit_code": 0,
                    "timed_out": False,
                    "output_limit_exceeded": False,
                    "owned_process_group_residual": False,
                },
                "cleanup": {"succeeded": True, "post_exists": False},
            },
        }
        stage = root / "stage"
        stage.mkdir(exist_ok=True)
        (stage / "trajectory.json").write_text("{}\n", encoding="utf-8")
        return {
            "status": "verified",
            "model": "arm-qwen38-q6-text",
            "stage": str(stage),
            "trajectory": str(stage / "trajectory.json"),
            "diagnosis": "ownership condition missing",
            "diff": "--- a/file\n+++ b/file\n",
            "test_command": ["swift", "test"],
            "test_exit": 0,
            "test_output": json.dumps(mac_result, separators=(",", ":")),
            "response_observed": True,
            "model_calls": 1,
        }

    def test_plan_only_preflights_source_without_backend_model_memory_or_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, task, _, task_sha256 = _swift_fixture(root)
            snapshot = MAC._snapshot(workspace, MAC._load_task(task, task_sha256)[0], task_sha256)
            result = self._run(
                root=root,
                task=task,
                edit_report=self._edit_report(root, task_sha256, snapshot["manifest_sha256"]),
                verifier_uri="http://127.0.0.1:9/api/chat",
                plan_only=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["executionVerifier"], "mac-swift")
            self.assertEqual(
                report["sourceSnapshot"]["manifest_sha256"], snapshot["manifest_sha256"]
            )
            self.assertEqual(report["modelCalls"], 0)
            self.assertFalse((root / "switch.log").exists())
            self.assertFalse((root / "memory").exists())

    def test_verified_mac_result_enters_devstral_and_final_memory_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, task, _, task_sha256 = _swift_fixture(root)
            snapshot = MAC._snapshot(workspace, MAC._load_task(task, task_sha256)[0], task_sha256)
            source_before = MAC._sha256(
                (workspace / "Sources/ContinuityFixture/BackendState.swift").read_bytes()
            )
            with verifier_server() as server:
                result = self._run(
                    root=root,
                    task=task,
                    edit_report=self._edit_report(root, task_sha256, snapshot["manifest_sha256"]),
                    verifier_uri=f"http://127.0.0.1:{server.server_address[1]}/api/chat",
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "verified")
            self.assertEqual(report["executionVerifier"], "mac-swift")
            self.assertEqual(report["macVerifier"]["status"], "verified")
            self.assertEqual(report["verifier"]["status"], "accepted")
            self.assertEqual(report["memory"]["terminal_status"], "verified")
            self.assertEqual(len(server.requests), 1)  # type: ignore[attr-defined]
            args = (root / "continuity-args.txt").read_text(encoding="utf-8")
            self.assertIn("mac-swift-verifier.py verify", args)
            self.assertIn("--expected-source-snapshot-sha256", args)
            self.assertEqual(
                MAC._sha256(
                    (workspace / "Sources/ContinuityFixture/BackendState.swift").read_bytes()
                ),
                source_before,
            )

    def test_forged_mac_success_fails_closed_before_devstral(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace, task, _, task_sha256 = _swift_fixture(root)
            snapshot = MAC._snapshot(workspace, MAC._load_task(task, task_sha256)[0], task_sha256)
            edit = self._edit_report(root, task_sha256, snapshot["manifest_sha256"])
            forged = json.loads(str(edit["test_output"]))
            forged["checkpoint"]["source_workspace_mutated"] = True
            edit["test_output"] = json.dumps(forged)
            result = self._run(
                root=root,
                task=task,
                edit_report=edit,
                verifier_uri="http://127.0.0.1:9/api/chat",
            )
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["verifier"]["status"], "not_run")
            self.assertIn("failed closed", report["rawFailure"])
            self.assertEqual(
                (root / "switch.log").read_text(encoding="utf-8").splitlines(),
                ["Qwen38", "Qwen38"],
            )


if __name__ == "__main__":
    unittest.main()
