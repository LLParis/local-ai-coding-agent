from __future__ import annotations

import ctypes
import io
import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from adapters.deepseek import DEEPSEEK_SOURCE_COMMIT, DeepSeekAdapter
from adapters.pi import PiAdapter
from adapters.protocol import AdapterContractError, TaskCapsule, validate_loopback_endpoint
from adapters.runner import HarnessProcessSpec, run_harness_process, stdout_emitter

FAKE_PROCESS = r"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

mode = sys.argv[1]
def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

emit({"type": "session", "version": 3, "id": "fake", "cwd": os.getcwd()})
if mode == "garbage":
    print("not-json", flush=True)
    time.sleep(30)
elif mode == "retry":
    emit({"type": "turn_start"})
    emit({"type": "auto_retry_start", "attempt": 1})
    time.sleep(30)
elif mode == "scope":
    emit({"type": "turn_start"})
    emit({
        "type": "tool_execution_start",
        "toolCallId": "1",
        "toolName": "edit",
        "args": {"path": "../escape.txt", "old_text": "", "new_text": "bad"},
    })
    time.sleep(30)
elif mode == "double-edit":
    emit({"type": "turn_start"})
    emit({
        "type": "tool_execution_start",
        "toolCallId": "1",
        "toolName": "edit",
        "args": {"path": "src/value.txt"},
    })
    emit({
        "type": "tool_execution_start",
        "toolCallId": "2",
        "toolName": "edit",
        "args": {"path": "src/value.txt"},
    })
    time.sleep(30)
elif mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(os.environ["FAKE_GRANDCHILD_PID"]).write_text(str(child.pid), encoding="utf-8")
    emit({"type": "turn_start"})
    time.sleep(60)
else:
    emit({"type": "turn_start"})
    emit({"type": "agent_end", "messages": []})
"""


class FakeOpenAI:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                outer.requests += 1
                if outer.mode == "error":
                    body = (
                        b'{"error":{"message":"synthetic provider failure","type":"server_error"}}'
                    )
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                created = 1_787_000_000

                def event(
                    choices: list[dict[str, object]], usage: dict[str, int] | None = None
                ) -> None:
                    value: dict[str, object] = {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": "fake-qwen",
                        "choices": choices,
                    }
                    if usage is not None:
                        value["usage"] = usage
                    self.wfile.write(
                        ("data: " + json.dumps(value, separators=(",", ":")) + "\n\n").encode()
                    )
                    self.wfile.flush()

                if outer.mode == "scope" or (outer.mode == "edit" and outer.requests <= 2):
                    if outer.mode == "scope":
                        tool_name = "edit"
                        tool_args = {
                            "path": "../escape.txt",
                            "edits": [{"oldText": "", "newText": "bad"}],
                        }
                        call_id = "call_scope"
                    elif outer.requests == 1:
                        tool_name = "edit"
                        tool_args = {
                            "path": "src/value.txt",
                            "edits": [{"oldText": "old\n", "newText": "new\n"}],
                        }
                        call_id = "call_edit"
                    else:
                        tool_name = "test"
                        tool_args = {}
                        call_id = "call_test"
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
                                                "name": tool_name,
                                                "arguments": json.dumps(
                                                    tool_args, separators=(",", ":")
                                                ),
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

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> FakeOpenAI:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


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
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


class AdapterFixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source"
        self.stage = root / "stage"
        for workspace in (self.source, self.stage):
            (workspace / "src").mkdir(parents=True)
            (workspace / "src" / "value.txt").write_bytes(b"old\n")
            (workspace / "README.md").write_bytes(b"fixture\n")
            (workspace / "verify.py").write_bytes(b"# hidden verifier\n")
        self.fake_process = root / "fake_harness.py"
        self.fake_process.write_text(textwrap.dedent(FAKE_PROCESS), encoding="utf-8")
        self.task_path = root / "task.json"
        self.task_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "backend": "Qwen38",
                    "workspace": str(self.source),
                    "objective": "Keep the fixture correct.",
                    "mutable": ["src/value.txt"],
                    "context": ["README.md"],
                    "verify_context": ["verify.py"],
                    "test_command": [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; assert "
                        "Path('src/value.txt').read_text(encoding='utf-8') == 'old\\n'",
                    ],
                    "timeout": 10,
                }
            ),
            encoding="utf-8",
        )
        self.capsule = TaskCapsule.load(self.task_path)

    def close(self) -> None:
        self.temp.cleanup()

    def spec(self, mode: str, **overrides: object) -> HarnessProcessSpec:
        env = dict(os.environ)
        env.update(overrides.pop("env", {}))
        return HarnessProcessSpec(
            harness="fake",
            model="fake-qwen",
            provider="ci-loopback",
            command=(sys.executable, str(self.fake_process), mode),
            env=env,
            cwd=self.stage,
            max_turns=int(overrides.pop("max_turns", 2)),
            max_tool_calls=int(overrides.pop("max_tool_calls", 2)),
            timeout_seconds=float(overrides.pop("timeout_seconds", 2.0)),
        )


class HarnessAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = AdapterFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_loopback_endpoint_rejects_remote_hosts(self) -> None:
        self.assertEqual(
            validate_loopback_endpoint("http://127.0.0.1:8818/v1/"), "http://127.0.0.1:8818/v1"
        )
        with self.assertRaises(AdapterContractError):
            validate_loopback_endpoint("https://example.com/v1")
        with self.assertRaises(AdapterContractError):
            validate_loopback_endpoint("http://localhost:8818/v1")

    def test_protocol_clean_stdout_and_one_terminal_event(self) -> None:
        stream = io.StringIO()
        result = run_harness_process(
            self.fixture.spec("garbage"),
            self.fixture.capsule,
            self.fixture.stage,
            emit=stdout_emitter(stream),
            run_verifier=False,
        )
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(sum(event["type"] == "terminal" for event in events), 1)
        self.assertEqual(result["stop"], "protocol_error")
        self.assertTrue(result["telemetry"]["child_cleaned"])

    def test_retry_event_fails_closed_without_second_turn(self) -> None:
        result = run_harness_process(
            self.fixture.spec("retry"),
            self.fixture.capsule,
            self.fixture.stage,
            emit=lambda _event: None,
            run_verifier=False,
        )
        self.assertEqual(result["stop"], "retry_forbidden")
        self.assertEqual(result["turns"], 1)
        self.assertEqual(result["automatic_retries"], 1)
        self.assertTrue(result["telemetry"]["child_cleaned"])

    def test_scope_violation_stops_before_outside_file_exists(self) -> None:
        result = run_harness_process(
            self.fixture.spec("scope"),
            self.fixture.capsule,
            self.fixture.stage,
            emit=lambda _event: None,
            run_verifier=False,
        )
        self.assertEqual(result["stop"], "scope_violation")
        self.assertFalse((self.fixture.stage.parent / "escape.txt").exists())
        self.assertEqual(
            (self.fixture.source / "src" / "value.txt").read_text(encoding="utf-8"), "old\n"
        )

    def test_repeated_edit_is_stopped_as_an_anti_loop_boundary(self) -> None:
        result = run_harness_process(
            self.fixture.spec("double-edit", max_tool_calls=4),
            self.fixture.capsule,
            self.fixture.stage,
            emit=lambda _event: None,
            run_verifier=False,
        )
        self.assertEqual(result["stop"], "repeated_mutation_or_test")
        self.assertEqual(result["tool_calls"], 2)
        self.assertTrue(result["telemetry"]["child_cleaned"])

    def test_timeout_kills_owned_grandchild(self) -> None:
        pid_file = self.fixture.stage.parent / "grandchild.pid"
        result = run_harness_process(
            self.fixture.spec(
                "hang", timeout_seconds=0.75, env={"FAKE_GRANDCHILD_PID": str(pid_file)}
            ),
            self.fixture.capsule,
            self.fixture.stage,
            emit=lambda _event: None,
            run_verifier=False,
        )
        self.assertEqual(result["stop"], "timeout")
        self.assertTrue(pid_file.is_file())
        pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 3
        while process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(process_alive(pid), f"owned grandchild {pid} survived adapter timeout")
        self.assertTrue(result["telemetry"]["child_cleaned"])

    def test_deepseek_reviewed_plugin_has_a_runnable_pinned_plan(self) -> None:
        plan = DeepSeekAdapter().launch_plan(
            self.fixture.capsule,
            self.fixture.stage,
            endpoint="http://127.0.0.1:8818/v1",
            model="fake-qwen",
        )
        self.assertTrue(plan["runnable"])
        self.assertEqual(plan["tool_surface"], ["read", "search", "edit", "test"])
        self.assertEqual(plan["automatic_retries"], 0)
        self.assertEqual(plan["runtime_identity"]["source_commit"], DEEPSEEK_SOURCE_COMMIT)
        self.assertEqual(
            (self.fixture.source / "src" / "value.txt").read_text(encoding="utf-8"), "old\n"
        )

    def test_pi_fake_endpoint_success_and_independent_verifier(self) -> None:
        events: list[dict[str, object]] = []
        with FakeOpenAI("success") as endpoint:
            result = PiAdapter().run(
                self.fixture.capsule,
                self.fixture.stage,
                endpoint=endpoint.endpoint,
                model="fake-qwen",
                max_turns=2,
                max_tool_calls=2,
                emit=events.append,
            )
            self.assertEqual(endpoint.requests, 1)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["automatic_retries"], 0)
        self.assertTrue(result["test"]["passed"])
        self.assertFalse(result["test"]["not_run"])
        self.assertEqual(sum(event["type"] == "terminal" for event in events), 1)
        self.assertTrue(result["telemetry"]["source_workspace_preserved"])

    def test_pi_scoped_edit_test_loop_and_hidden_verifier(self) -> None:
        raw = json.loads(self.fixture.task_path.read_text(encoding="utf-8"))
        raw["objective"] = "Replace old with new in src/value.txt."
        raw["test_command"] = [
            sys.executable,
            "-c",
            "from pathlib import Path; assert "
            "Path('src/value.txt').read_text(encoding='utf-8') == 'new\\n'",
        ]
        task = self.fixture.stage.parent / "edit-task.json"
        task.write_text(json.dumps(raw), encoding="utf-8")
        capsule = TaskCapsule.load(task)
        with FakeOpenAI("edit") as endpoint:
            result = PiAdapter().run(
                capsule,
                self.fixture.stage,
                endpoint=endpoint.endpoint,
                model="fake-qwen",
                max_turns=4,
                max_tool_calls=3,
                emit=lambda _event: None,
            )
            self.assertEqual(endpoint.requests, 3)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["turns"], 3)
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(result["automatic_retries"], 0)
        self.assertEqual(result["diff"]["changed"], ["src/value.txt"])
        self.assertIn("+new", result["diff"]["unified"])
        self.assertTrue(result["test"]["passed"])
        self.assertEqual(
            (self.fixture.stage / "src" / "value.txt").read_text(encoding="utf-8"), "new\n"
        )
        self.assertEqual(
            (self.fixture.source / "src" / "value.txt").read_text(encoding="utf-8"), "old\n"
        )

    def test_pi_provider_failure_makes_exactly_one_request(self) -> None:
        with FakeOpenAI("error") as endpoint:
            result = PiAdapter().run(
                self.fixture.capsule,
                self.fixture.stage,
                endpoint=endpoint.endpoint,
                model="fake-qwen",
                max_turns=2,
                max_tool_calls=2,
                emit=lambda _event: None,
            )
            self.assertEqual(endpoint.requests, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["automatic_retries"], 0)
        self.assertIn(result["stop"], ("provider_error", "child_exit_nonzero"))
        self.assertTrue(result["test"]["not_run"])

    def test_pi_out_of_scope_tool_call_never_writes(self) -> None:
        outside = self.fixture.stage.parent / "escape.txt"
        with FakeOpenAI("scope") as endpoint:
            result = PiAdapter().run(
                self.fixture.capsule,
                self.fixture.stage,
                endpoint=endpoint.endpoint,
                model="fake-qwen",
                max_turns=2,
                max_tool_calls=2,
                emit=lambda _event: None,
            )
            self.assertEqual(endpoint.requests, 1)
        self.assertEqual(result["stop"], "scope_violation")
        self.assertFalse(outside.exists())
        self.assertEqual(
            (self.fixture.source / "src" / "value.txt").read_text(encoding="utf-8"), "old\n"
        )


if __name__ == "__main__":
    unittest.main()
