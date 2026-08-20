from __future__ import annotations

import ctypes
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from adapters.deepseek import DeepSeekAdapter
from adapters.deepseek.event_bridge import (
    EventAudit,
    EventProtocolError,
    bridge,
    strict_json_loads,
)
from adapters.protocol import TaskCapsule


def process_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


class _DaemonServer(ThreadingHTTPServer):
    daemon_threads = True


class ScriptedOpenAI:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.requests = 0
        self.bodies: list[bytes] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                outer.requests += 1
                outer.bodies.append(body)
                if outer.mode == "hang":
                    time.sleep(30)
                    return
                if outer.mode == "error":
                    payload = b'{"error":{"message":"synthetic failure","type":"server_error"}}'
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                def event(
                    choices: list[dict[str, object]], usage: dict[str, int] | None = None
                ) -> None:
                    value: dict[str, object] = {
                        "id": f"chatcmpl-{outer.requests}",
                        "object": "chat.completion.chunk",
                        "created": 1_787_000_000,
                        "model": "fake-qwen",
                        "choices": choices,
                    }
                    if usage is not None:
                        value["usage"] = usage
                    self.wfile.write(
                        ("data: " + json.dumps(value, separators=(",", ":")) + "\n\n").encode()
                    )
                    self.wfile.flush()

                tool: tuple[str, str, str] | None = None
                request = outer.requests
                if outer.mode == "success":
                    if request == 1:
                        tool = (
                            "edit",
                            "call-edit",
                            json.dumps(
                                {
                                    "path": "src/value.txt",
                                    "old_text": "old\n",
                                    "new_text": "new\n",
                                },
                                separators=(",", ":"),
                            ),
                        )
                    elif request == 2:
                        tool = ("test", "call-test", "{}")
                elif outer.mode == "scope" and request == 1:
                    tool = (
                        "edit",
                        "call-scope",
                        json.dumps(
                            {
                                "path": "../escape.txt",
                                "old_text": "",
                                "new_text": "bad",
                            },
                            separators=(",", ":"),
                        ),
                    )
                elif outer.mode == "duplicate_json" and request == 1:
                    tool = (
                        "edit",
                        "call-duplicate-json",
                        '{"path":"src/value.txt","old_text":"old\\n",'
                        '"old_text":"wrong\\n","new_text":"bad\\n"}',
                    )
                elif outer.mode == "double_edit" and request in (1, 2):
                    tool = (
                        "edit",
                        f"call-edit-{request}",
                        json.dumps(
                            {
                                "path": "src/value.txt",
                                "old_text": "old\n",
                                "new_text": "new\n",
                            },
                            separators=(",", ":"),
                        ),
                    )
                elif outer.mode == "test_first" and request == 1:
                    tool = ("test", "call-test-first", "{}")

                if tool is not None:
                    name, call_id, arguments = tool
                    event(
                        [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": call_id,
                                            "type": "function",
                                            "function": {
                                                "name": name,
                                                "arguments": arguments,
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": None,
                            }
                        ]
                    )
                    event([{"index": 0, "delta": {}, "finish_reason": "tool_calls"}])
                else:
                    event(
                        [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "done"},
                                "finish_reason": None,
                            }
                        ]
                    )
                    event(
                        [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
                    )
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = _DaemonServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> ScriptedOpenAI:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class DeepSeekFixture:
    def __init__(self, *, timeout: int = 10, test_command: list[str] | None = None) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source"
        self.stage = root / "stage"
        for workspace in (self.source, self.stage):
            (workspace / "src").mkdir(parents=True)
            (workspace / "src" / "value.txt").write_bytes(b"old\n")
            (workspace / "README.md").write_bytes(b"Only change src/value.txt.\n")
            (workspace / "verify.py").write_bytes(b"# HIDDEN_VERIFIER_CANARY_9F322F\n")
        command = test_command or [
            sys.executable,
            "-c",
            "from pathlib import Path; secret='HIDDEN_COMMAND_CANARY_388ED1'; "
            "assert Path('src/value.txt').read_text(encoding='utf-8') == 'new\\n'",
        ]
        self.task = root / "task.json"
        self.task.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "backend": "Qwen38",
                    "workspace": str(self.source),
                    "objective": "Replace old with new in src/value.txt.",
                    "mutable": ["src/value.txt"],
                    "context": ["README.md"],
                    "verify_context": ["verify.py"],
                    "test_command": command,
                    "timeout": timeout,
                }
            ),
            encoding="utf-8",
        )
        self.capsule = TaskCapsule.load(self.task)

    def close(self) -> None:
        self.temp.cleanup()


class DeepSeekAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = DeepSeekFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def run_mode(self, mode: str) -> tuple[dict[str, object], ScriptedOpenAI]:
        endpoint = ScriptedOpenAI(mode)
        events: list[dict[str, object]] = []
        with endpoint:
            result = DeepSeekAdapter().run(
                self.fixture.capsule,
                self.fixture.stage,
                endpoint=endpoint.endpoint,
                model="fake-qwen",
                max_turns=4,
                max_tool_calls=4,
                emit=events.append,
            )
        self.last_events = events
        return result, endpoint

    def test_real_deepseek_plugin_edit_test_loop_and_hidden_verifier(self) -> None:
        result, endpoint = self.run_mode("success")
        self.assertEqual(endpoint.requests, 3)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["stop"], "verified")
        self.assertEqual(result["turns"], 3)
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(result["automatic_retries"], 0)
        self.assertEqual(sum(event["type"] == "terminal" for event in self.last_events), 1)
        self.assertIs(self.last_events[-1], result)
        self.assertIn("live_event_evidence", self.last_events[-1])
        self.assertEqual(result["diff"]["changed"], ["src/value.txt"])
        self.assertTrue(result["test"]["passed"])
        evidence = result["live_event_evidence"]
        self.assertTrue(evidence["agent_end"])
        self.assertTrue(evidence["edit_succeeded"])
        self.assertTrue(evidence["test_succeeded"])
        self.assertEqual(evidence["model_requests"], 3)
        self.assertEqual(evidence["pre_side_effect_intents"], 2)
        self.assertEqual(len(evidence["tool_call_ids"]), 2)
        self.assertEqual(len(evidence["effect_ids"]), 2)
        self.assertEqual(evidence["denied_calls"], 0)
        request_text = b"\n".join(endpoint.bodies).decode("utf-8")
        self.assertNotIn("HIDDEN_VERIFIER_CANARY_9F322F", request_text)
        self.assertNotIn("HIDDEN_COMMAND_CANARY_388ED1", request_text)
        self.assertNotIn("verify.py", request_text)
        self.assertEqual(
            (self.fixture.stage / "src/value.txt").read_text(encoding="utf-8"), "new\n"
        )
        self.assertEqual(
            (self.fixture.source / "src/value.txt").read_text(encoding="utf-8"), "old\n"
        )

    def test_path_escape_is_denied_before_write(self) -> None:
        result, endpoint = self.run_mode("scope")
        self.assertEqual(endpoint.requests, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop"], "scope_violation")
        self.assertFalse((self.fixture.stage.parent / "escape.txt").exists())
        self.assertEqual(
            (self.fixture.source / "src/value.txt").read_text(encoding="utf-8"), "old\n"
        )

    def test_duplicate_argument_key_is_rejected_without_edit(self) -> None:
        result, endpoint = self.run_mode("duplicate_json")
        self.assertEqual(endpoint.requests, 2)
        self.assertEqual(result["status"], "failed")
        evidence = result["live_event_evidence"]
        self.assertEqual(evidence["denied_calls"], 1)
        self.assertIn("duplicate object key", evidence["denial_reasons"][0])
        self.assertEqual(
            (self.fixture.stage / "src/value.txt").read_text(encoding="utf-8"), "old\n"
        )

    def test_test_before_edit_is_denied(self) -> None:
        result, endpoint = self.run_mode("test_first")
        self.assertEqual(endpoint.requests, 2)
        self.assertEqual(result["status"], "failed")
        self.assertIn(
            "test requires one successful edit first",
            result["live_event_evidence"]["denial_reasons"],
        )

    def test_duplicate_side_effect_is_stopped(self) -> None:
        result, endpoint = self.run_mode("double_edit")
        self.assertEqual(endpoint.requests, 2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop"], "duplicate_side_effect")
        self.assertEqual(
            (self.fixture.stage / "src/value.txt").read_text(encoding="utf-8"), "new\n"
        )
        self.assertEqual(
            (self.fixture.source / "src/value.txt").read_text(encoding="utf-8"), "old\n"
        )

    def test_provider_failure_makes_one_request_and_zero_retry(self) -> None:
        result, endpoint = self.run_mode("error")
        self.assertEqual(endpoint.requests, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["automatic_retries"], 0)
        self.assertEqual(result["live_event_evidence"]["model_requests"], 1)

    def test_strict_event_parser_and_schema_reject_malformed_data(self) -> None:
        with self.assertRaisesRegex(EventProtocolError, "duplicate JSON key"):
            strict_json_loads('{"type":"turn_start","type":"agent_end"}')
        audit = EventAudit(max_turns=2, max_tool_calls=2)
        with self.assertRaisesRegex(EventProtocolError, "attempt"):
            audit.accept({"type": "turn_start", "turn": 1, "step": 1, "attempt": 2})
        with self.assertRaisesRegex(EventProtocolError, "allowlist"):
            audit.accept(
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call-1",
                    "toolName": "shell",
                    "args": {},
                    "effectId": None,
                    "intent": {"recorded": True, "side_effect": False},
                    "denied": None,
                }
            )

    def test_bridge_reports_child_crash_without_fabricating_agent_end(self) -> None:
        stream = io.StringIO()
        child = [
            sys.executable,
            "-c",
            "import json,sys; print(json.dumps({'type':'turn_start','turn':1,"
            "'step':1,'attempt':1}),flush=True); sys.exit(7)",
        ]
        with redirect_stdout(stream):
            exit_code = bridge(child, max_turns=2, max_tool_calls=2)
        events = [strict_json_loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(exit_code, 2)
        self.assertEqual([event["type"] for event in events], ["turn_start", "message_end"])
        self.assertIn("exited with code 7 without agent_end", events[-1]["message"]["errorMessage"])

    def test_timeout_kills_owned_test_grandchild(self) -> None:
        self.fixture.close()
        grandchild_temp = tempfile.TemporaryDirectory()
        self.addCleanup(grandchild_temp.cleanup)
        root = Path(grandchild_temp.name)
        pid_file = root / "grandchild.pid"
        command = [
            sys.executable,
            "-c",
            "import subprocess,sys,time; from pathlib import Path; "
            f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"Path({str(pid_file)!r}).write_text(str(p.pid),encoding='utf-8'); time.sleep(60)",
        ]
        self.fixture = DeepSeekFixture(timeout=10, test_command=command)
        result, _endpoint = self.run_mode("success")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stop"], "timeout")
        self.assertTrue(result["telemetry"]["child_cleaned"])
        self.assertTrue(result["telemetry"]["job_assigned"])
        self.assertTrue(pid_file.is_file())
        pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 3
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(process_alive(pid), f"owned test grandchild {pid} survived timeout")


if __name__ == "__main__":
    unittest.main()
