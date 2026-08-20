"""Owned child lifecycle, event normalization, scope checks, and verification."""

from __future__ import annotations

import ctypes
import difflib
import hashlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .protocol import (
    ADAPTER_SCHEMA_VERSION,
    ALLOWED_TOOLS,
    AdapterContractError,
    TaskCapsule,
    canonical_json,
    changed_paths,
    path_allowed,
    sha256_bytes,
    snapshot_selected,
    snapshot_tree,
)

Emitter = Callable[[dict[str, Any]], None]


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise AdapterContractError(f"duplicate JSON key in harness event: {key!r}")
        output[key] = value
    return output


def stdout_emitter(stream: TextIO | None = None) -> Emitter:
    target = stream or sys.stdout

    def emit(event: dict[str, Any]) -> None:
        target.write(canonical_json(event) + "\n")
        target.flush()

    return emit


class _WindowsJob:
    """Windows Job Object with kill-on-close for exact descendant ownership."""

    def __init__(self) -> None:
        self.handle: int | None = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            return
        self.handle = handle

    def assign(self, pid: int) -> bool:
        if self.handle is None or os.name != "nt":
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        process = kernel32.OpenProcess(0x0001 | 0x0100, False, pid)  # TERMINATE | SET_QUOTA
        if not process:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(self.handle, process))
        finally:
            kernel32.CloseHandle(process)

    def terminate(self) -> bool:
        if self.handle is None or os.name != "nt":
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        return bool(kernel32.TerminateJobObject(self.handle, 1))

    def close(self) -> None:
        if self.handle is not None and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(self.handle)
            self.handle = None


class _OwnedProcess:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.job = _WindowsJob()
        self.job_assigned = self.job.assign(process.pid)

    def terminate_tree(self) -> None:
        if self.process.poll() is not None:
            return
        if self.job.terminate():
            return
        if os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        else:
            self.process.terminate()
        try:
            self.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                self.process.kill()

    def close(self) -> None:
        self.job.close()


@dataclass(frozen=True)
class HarnessProcessSpec:
    harness: str
    model: str
    provider: str
    command: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path
    max_turns: int = 8
    max_tool_calls: int = 12
    timeout_seconds: float = 300.0


def _popen(command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> subprocess.Popen[str]:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(
        list(command),
        cwd=str(cwd),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )


def _stream_reader(stream: TextIO, output: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            output.put(line)
    finally:
        output.put(None)


def _stderr_reader(stream: TextIO, chunks: list[str], digest: Any, cap: int = 131_072) -> None:
    used = 0
    for chunk in iter(lambda: stream.read(8192), ""):
        encoded = chunk.encode("utf-8", errors="replace")
        digest.update(encoded)
        if used < cap:
            take = chunk[: cap - used]
            chunks.append(take)
            used += len(take)


def _event_type(event: dict[str, Any]) -> str:
    value = event.get("type", "")
    return value if isinstance(value, str) else ""


def _tool_fields(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = event.get("toolName", event.get("tool_name", ""))
    args = event.get("args", event.get("input", {}))
    return (name if isinstance(name, str) else "", args if isinstance(args, dict) else {})


def _tool_call_id(event: dict[str, Any]) -> str:
    value = event.get("toolCallId", event.get("tool_call_id", event.get("id")))
    return value if isinstance(value, str) else ""


def _validate_tool_call(capsule: TaskCapsule, name: str, args: dict[str, Any]) -> str | None:
    if name not in ALLOWED_TOOLS:
        return f"tool {name!r} is outside the four-tool allowlist"
    path = args.get("path")
    if name == "edit":
        if not isinstance(path, str) or not path_allowed(path, capsule.mutable):
            return f"edit path is outside mutable scope: {path!r}"
    elif name == "read":
        if not isinstance(path, str) or not path_allowed(path, capsule.readable):
            return f"read path is outside readable scope: {path!r}"
    elif name == "search" and path is not None:
        if not isinstance(path, str) or not path_allowed(path, capsule.readable):
            return f"search path is outside readable scope: {path!r}"
    return None


def _read_text_for_diff(
    root: Path, paths: Sequence[str], cap: int = 512_000
) -> dict[str, list[str] | None]:
    output: dict[str, list[str] | None] = {}
    for relative in paths:
        path = root / Path(relative)
        if not path.exists():
            output[relative] = []
            continue
        if not path.is_file() or path.stat().st_size > cap:
            output[relative] = None
            continue
        try:
            output[relative] = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            output[relative] = None
    return output


def _unified_diff(before: dict[str, list[str] | None], after: dict[str, list[str] | None]) -> str:
    chunks: list[str] = []
    for path in sorted(before.keys() | after.keys()):
        left = before.get(path)
        right = after.get(path)
        if left is None or right is None or left == right:
            continue
        chunks.extend(difflib.unified_diff(left, right, fromfile=f"a/{path}", tofile=f"b/{path}"))
    return "".join(chunks)[:1_000_000]


def run_argv(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    start = time.monotonic()
    process = _popen(command, cwd=cwd, env=env)
    owner = _OwnedProcess(process)
    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            owner.terminate_tree()
            stdout, stderr = process.communicate(timeout=5)
    finally:
        owner.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    return {
        "command": list(command),
        "exit_code": process.returncode,
        "passed": process.returncode == 0 and not timed_out,
        "timed_out": timed_out,
        "duration_ms": round((time.monotonic() - start) * 1000, 3),
        "stdout_tail": stdout[-16_384:],
        "stderr_tail": stderr[-16_384:],
        "stdout_sha256": sha256_bytes(stdout.encode("utf-8", errors="replace")),
        "stderr_sha256": sha256_bytes(stderr.encode("utf-8", errors="replace")),
        "child_cleaned": process.poll() is not None,
        "job_assigned": owner.job_assigned,
    }


def run_harness_process(
    spec: HarnessProcessSpec,
    capsule: TaskCapsule,
    stage: Path,
    *,
    emit: Emitter | None = None,
    run_verifier: bool = True,
) -> dict[str, Any]:
    output = emit or stdout_emitter()
    stage = capsule.validate_stage(stage)
    if not 1 <= spec.max_turns <= 32 or not 1 <= spec.max_tool_calls <= 64:
        raise AdapterContractError("turn and tool budgets are outside qualification bounds")
    start = time.monotonic()
    run_id = str(uuid.uuid4())
    source_before = snapshot_selected(capsule.workspace, capsule.scoped_source_paths)
    stage_before = snapshot_tree(stage)
    mutable_before = _read_text_for_diff(stage, capsule.mutable)
    output(
        {
            "type": "run_start",
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "run_id": run_id,
            "harness": spec.harness,
            "model": spec.model,
            "provider": spec.provider,
            "max_turns": spec.max_turns,
            "max_tool_calls": spec.max_tool_calls,
        }
    )

    process = _popen(spec.command, cwd=spec.cwd, env=spec.env)
    owner = _OwnedProcess(process)
    lines: queue.Queue[str | None] = queue.Queue()
    stderr_chunks: list[str] = []
    stderr_digest = hashlib.sha256()
    stdout_digest = hashlib.sha256()
    stdout_thread = threading.Thread(
        target=_stream_reader, args=(process.stdout, lines), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_stderr_reader, args=(process.stderr, stderr_chunks, stderr_digest), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    turns = 0
    calls = 0
    tool_name_counts: dict[str, int] = {}
    tool_results: list[dict[str, Any]] = []
    pending_tool_calls: dict[str, str] = {}
    completed_tool_call_ids: set[str] = set()
    retry_events = 0
    agent_end = False
    timed_out = False
    protocol_error: str | None = None
    stop = "child_exit"
    deadline = start + min(float(capsule.timeout), float(spec.timeout_seconds))
    stdout_done = False
    raw_stdout_bytes = 0

    try:
        while not stdout_done or process.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                stop = "timeout"
                owner.terminate_tree()
                break
            try:
                line = lines.get(timeout=0.05)
            except queue.Empty:
                continue
            if line is None:
                stdout_done = True
                continue
            encoded = line.encode("utf-8", errors="replace")
            raw_stdout_bytes += len(encoded)
            stdout_digest.update(encoded)
            if raw_stdout_bytes > 4 * 1024 * 1024:
                protocol_error = "child stdout exceeded 4 MiB"
                stop = "protocol_error"
                owner.terminate_tree()
                break
            try:
                event = json.loads(line, object_pairs_hook=_strict_json_object)
            except (json.JSONDecodeError, AdapterContractError):
                protocol_error = "child stdout contained malformed or duplicate-key JSON"
                stop = "protocol_error"
                owner.terminate_tree()
                break
            if not isinstance(event, dict):
                protocol_error = "child stdout event was not an object"
                stop = "protocol_error"
                owner.terminate_tree()
                break
            kind = _event_type(event)
            lower_kind = kind.casefold()
            if "retry" in lower_kind:
                retry_events += 1
                protocol_error = f"automatic retry event forbidden: {kind}"
                stop = "retry_forbidden"
                owner.terminate_tree()
                break
            if "compaction" in lower_kind:
                protocol_error = f"automatic/manual compaction event forbidden: {kind}"
                stop = "compaction_forbidden"
                owner.terminate_tree()
                break
            if kind == "turn_start":
                turns += 1
                output({"type": "turn_start", "run_id": run_id, "turn": turns})
                if turns > spec.max_turns:
                    protocol_error = "turn budget exceeded"
                    stop = "turn_budget"
                    owner.terminate_tree()
                    break
            elif kind == "tool_execution_start":
                calls += 1
                name, args = _tool_fields(event)
                call_id = _tool_call_id(event)
                if (
                    not call_id
                    or call_id in pending_tool_calls
                    or call_id in completed_tool_call_ids
                ):
                    protocol_error = "tool call IDs must be non-empty and globally unique"
                    stop = "protocol_error"
                    owner.terminate_tree()
                    break
                pending_tool_calls[call_id] = name
                tool_name_counts[name] = tool_name_counts.get(name, 0) + 1
                output(
                    {
                        "type": "tool_call",
                        "run_id": run_id,
                        "index": calls,
                        "call_id": call_id,
                        "tool": name,
                    }
                )
                violation = _validate_tool_call(capsule, name, args)
                if violation is not None:
                    protocol_error = violation
                    stop = "scope_violation"
                    owner.terminate_tree()
                    break
                if calls > spec.max_tool_calls:
                    protocol_error = "tool-call budget exceeded"
                    stop = "tool_budget"
                    owner.terminate_tree()
                    break
                if name in ("edit", "test") and tool_name_counts[name] > 1:
                    protocol_error = f"qualification allows at most one {name} call"
                    stop = "repeated_mutation_or_test"
                    owner.terminate_tree()
                    break
            elif kind == "tool_execution_end":
                name, _args = _tool_fields(event)
                call_id = _tool_call_id(event)
                if not call_id or pending_tool_calls.pop(call_id, None) != name:
                    protocol_error = "tool result does not match one pending tool call"
                    stop = "protocol_error"
                    owner.terminate_tree()
                    break
                completed_tool_call_ids.add(call_id)
                raw_result = event.get("result")
                serialized = canonical_json(raw_result) if raw_result is not None else "null"
                summary: dict[str, Any] = {
                    "index": len(tool_results) + 1,
                    "call_id": call_id,
                    "tool": name,
                    "is_error": bool(event.get("isError", event.get("is_error", False))),
                    "result_sha256": sha256_bytes(serialized.encode("utf-8")),
                }
                if summary["is_error"] and isinstance(raw_result, dict):
                    content = raw_result.get("content")
                    if isinstance(content, list):
                        texts = [
                            item.get("text", "")
                            for item in content
                            if isinstance(item, dict) and isinstance(item.get("text"), str)
                        ]
                        summary["error_tail"] = "\n".join(texts)[-2048:]
                if isinstance(raw_result, dict) and isinstance(raw_result.get("details"), dict):
                    details = raw_result["details"]
                    summary["exit_code"] = details.get("exitCode", details.get("exit_code"))
                    summary["path"] = details.get("path")
                tool_results.append(summary)
                output({"type": "tool_result", "run_id": run_id, **summary})
            elif kind in ("message_update", "message_end"):
                message = event.get("message")
                assistant_event = event.get("assistantMessageEvent")
                stop_reason = (
                    message.get("stopReason", message.get("stop_reason"))
                    if isinstance(message, dict)
                    else None
                )
                error_message = (
                    message.get("errorMessage", message.get("error_message"))
                    if isinstance(message, dict)
                    else None
                )
                stream_error = (
                    assistant_event.get("error") or assistant_event.get("errorMessage")
                    if isinstance(assistant_event, dict) and assistant_event.get("type") == "error"
                    else None
                )
                if stop_reason == "error" or error_message or stream_error:
                    protocol_error = str(
                        error_message or stream_error or "provider returned an assistant error"
                    )
                    stop = "provider_error"
                    owner.terminate_tree()
                    break
            elif kind == "agent_end":
                agent_end = True
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            owner.terminate_tree()
            process.wait(timeout=5)
    finally:
        owner.close()
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    stage_after_harness = snapshot_tree(stage)
    harness_changed = changed_paths(stage_before, stage_after_harness)
    out_of_scope = [path for path in harness_changed if not path_allowed(path, capsule.mutable)]
    if out_of_scope and protocol_error is None:
        protocol_error = f"harness changed paths outside mutable scope: {', '.join(out_of_scope)}"
        stop = "scope_violation"
    if process.returncode not in (0, None) and protocol_error is None and not timed_out:
        protocol_error = f"harness child exited with code {process.returncode}"
        stop = "child_exit_nonzero"
    if not agent_end and protocol_error is None and not timed_out:
        protocol_error = "harness stream ended without agent_end"
        stop = "protocol_error"
    if pending_tool_calls and protocol_error is None and not timed_out:
        protocol_error = "harness stream ended with unresolved tool calls"
        stop = "protocol_error"

    verifier: dict[str, Any] = {
        "command": list(capsule.test_command),
        "exit_code": None,
        "passed": False,
        "timed_out": False,
        "not_run": True,
    }
    if protocol_error is None and not timed_out and run_verifier:
        verifier_env = dict(os.environ)
        verifier_env["PYTHONDONTWRITEBYTECODE"] = "1"
        verifier_env["CI_ADAPTER_VERIFIER"] = "1"
        verifier = run_argv(
            capsule.test_command,
            cwd=stage,
            env=verifier_env,
            timeout_seconds=min(float(capsule.timeout), 300.0),
        )
        verifier["not_run"] = False
        if not verifier["passed"]:
            stop = "verification_failed"
    elif protocol_error is None and not timed_out and not run_verifier:
        verifier = {**verifier, "not_run": True, "passed": True}

    source_after = snapshot_selected(capsule.workspace, capsule.scoped_source_paths)
    source_preserved = source_before == source_after
    if not source_preserved:
        protocol_error = "source workspace changed during staged harness execution"
        stop = "source_modified"

    mutable_after = _read_text_for_diff(stage, capsule.mutable)
    unified = _unified_diff(mutable_before, mutable_after)
    diff = {
        "changed": [path for path in harness_changed if path_allowed(path, capsule.mutable)],
        "out_of_scope": out_of_scope,
        "unified": unified,
        "sha256": sha256_bytes(unified.encode("utf-8")),
    }
    stderr_text = "".join(stderr_chunks)
    passed = (
        protocol_error is None
        and not timed_out
        and process.returncode == 0
        and agent_end
        and verifier.get("passed") is True
        and source_preserved
        and not out_of_scope
    )
    if passed:
        stop = "verified"
    terminal = {
        "type": "terminal",
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "run_id": run_id,
        "harness": spec.harness,
        "status": "passed" if passed else "failed",
        "model": spec.model,
        "provider": spec.provider,
        "turns": turns,
        "tool_calls": calls,
        "tool_results": tool_results,
        "automatic_retries": retry_events,
        "stop": stop,
        "diff": diff,
        "test": verifier,
        "telemetry": {
            "duration_ms": round((time.monotonic() - start) * 1000, 3),
            "child_exit_code": process.returncode,
            "timed_out": timed_out,
            "child_cleaned": process.poll() is not None,
            "job_assigned": owner.job_assigned,
            "stdout_sha256": stdout_digest.hexdigest(),
            "stderr_sha256": stderr_digest.hexdigest(),
            "stderr_tail": stderr_text[-16_384:],
            "source_workspace_preserved": source_preserved,
        },
        "error": protocol_error,
    }
    output(terminal)
    return terminal
