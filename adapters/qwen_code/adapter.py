"""Production Qwen Code harness adapter for the live local coding worker."""

from __future__ import annotations

import hashlib
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from ..protocol import AdapterContractError, assert_no_reparse_path, canonical_json
from ..runner import _OwnedProcess
from .events import QwenCodeEventAccumulator, QwenCodeEventError, strict_json_loads
from .runtime import QWEN_CODE_VERSION, QwenCodeRuntime

HARNESS_ID = "qwen-code-model-aligned"
MODEL_ID = "arm-qwen38-q6-text"
ENDPOINT = "http://127.0.0.1:8818/v1"
CONTEXT_TOKENS = 32768
OUTPUT_TOKENS = 8192
ALLOWED_TOOLS = frozenset(
    {
        "edit",
        "write_file",
        "read_file",
        "grep_search",
        "glob",
        "run_shell_command",
        "list_directory",
        "lsp",
    }
)
MAX_STREAM_BYTES = 64 * 1024 * 1024

EventEmitter = Callable[[dict[str, Any]], None]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AdapterContractError(f"{name} must be a non-empty string without NUL")
    return value.strip()


def _stderr_reader(stream: TextIO, chunks: list[str], digest: Any) -> None:
    retained = 0
    for chunk in iter(lambda: stream.read(8192), ""):
        encoded = chunk.encode("utf-8", errors="replace")
        digest.update(encoded)
        if retained < 262_144:
            remaining = 262_144 - retained
            chunks.append(chunk[:remaining])
            retained += min(len(chunk), remaining)


def _stdout_reader(stream: TextIO, output: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            output.put(line)
    finally:
        output.put(None)


def _language_files(stage: Path, *, maximum: int = 20_000) -> set[str]:
    ignored = {
        ".git",
        ".hg",
        ".svn",
        ".qwen",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "target",
    }
    suffixes: set[str] = set()
    pending = [stage]
    inspected = 0
    while pending and inspected < maximum:
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            continue
        for child in children:
            if child.name in ignored:
                continue
            if child.is_dir():
                pending.append(child)
            elif child.is_file():
                inspected += 1
                suffixes.add(child.suffix.casefold())
                if inspected >= maximum:
                    break
    return suffixes


@dataclass(frozen=True)
class LspProvision:
    path: Path | None
    remove_after: bool
    languages: tuple[str, ...]
    mode: str


def _provision_lsp(stage: Path, runtime: QwenCodeRuntime) -> LspProvision:
    path = stage / ".lsp.json"
    if path.is_file():
        return LspProvision(path=path, remove_after=False, languages=("project",), mode="project")
    suffixes = _language_files(stage)
    node = str(runtime.node)
    configuration: dict[str, Any] = {}
    languages: list[str] = []
    if suffixes.intersection({".py", ".pyi"}):
        languages.append("python")
        configuration["python"] = {
            "command": node,
            "args": [
                str(runtime.runtime / "node_modules" / "basedpyright" / "langserver.index.js"),
                "--stdio",
            ],
            "extensionToLanguage": {".py": "python", ".pyi": "python"},
            "startupTimeout": 15000,
            "shutdownTimeout": 5000,
            "restartOnCrash": False,
            "maxRestarts": 0,
            "trustRequired": False,
        }
    if suffixes.intersection({".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"}):
        languages.append("typescript")
        configuration["typescript"] = {
            "command": node,
            "args": [
                str(
                    runtime.runtime
                    / "node_modules"
                    / "typescript-language-server"
                    / "lib"
                    / "cli.mjs"
                ),
                "--stdio",
            ],
            "extensionToLanguage": {
                ".ts": "typescript",
                ".tsx": "typescriptreact",
                ".js": "javascript",
                ".jsx": "javascriptreact",
                ".mts": "typescript",
                ".cts": "typescript",
            },
            "startupTimeout": 15000,
            "shutdownTimeout": 5000,
            "restartOnCrash": False,
            "maxRestarts": 0,
            "trustRequired": False,
        }
    if not configuration:
        return LspProvision(
            path=None, remove_after=False, languages=(), mode="no-matching-language"
        )
    path.write_text(canonical_json(configuration) + "\n", encoding="utf-8")
    return LspProvision(
        path=path,
        remove_after=True,
        languages=tuple(languages),
        mode="pinned-injected",
    )


@dataclass(frozen=True)
class QwenCodeInvocation:
    command: tuple[str, ...]
    environment: Mapping[str, str]
    cwd: Path
    prompt: str
    runtime_state: Path


class QwenCodeAdapter:
    """Run the official model-aligned harness in an isolated repository stage."""

    def __init__(self, runtime: QwenCodeRuntime | None = None) -> None:
        self.runtime = runtime or QwenCodeRuntime.default()

    @staticmethod
    def _system_prompt(stage: Path) -> str:
        return " ".join(
            (
                "You are the local implementation worker inside an isolated repository stage.",
                f"The only writable project root is {stage}.",
                "Inspect the repository before editing, using list/glob/grep/read and LSP",
                "when useful. Implement the complete requested outcome, then run the repository's",
                "real compiler,",
                "tests, static checks, or focused execution needed to establish whether it works.",
                "Use edit/write_file for file changes and run_shell_command for repository",
                "commands. Do not access paths outside the stage, install packages or extensions,",
                "create agents,",
                "commit, push, or claim a command passed without reading its result.",
                "Finish with the diagnosis, changed files, commands and outcomes, and any",
                "real blocker.",
            )
        )

    def build_invocation(
        self,
        *,
        stage: Path | str,
        objective: str,
        runtime_state: Path,
        max_session_turns: int = 48,
        max_tool_calls: int = 80,
        max_wall_time_seconds: int = 900,
    ) -> QwenCodeInvocation:
        stage_path = Path(stage).expanduser().resolve()
        if not stage_path.is_dir():
            raise AdapterContractError(f"Qwen Code stage does not exist: {stage_path}")
        assert_no_reparse_path(stage_path, stage_path)
        objective = _validate_text(objective, "objective")
        if not 1 <= max_session_turns <= 200:
            raise AdapterContractError("max_session_turns must be from 1 through 200")
        if not 1 <= max_tool_calls <= 500:
            raise AdapterContractError("max_tool_calls must be from 1 through 500")
        if not 30 <= max_wall_time_seconds <= 7200:
            raise AdapterContractError("max_wall_time_seconds must be from 30 through 7200")
        self.runtime.validate_deployed()
        command = (
            str(self.runtime.node),
            str(self.runtime.cli),
            "--model",
            MODEL_ID,
            "--output-format",
            "stream-json",
            "--approval-mode",
            "yolo",
            "--extensions",
            "none",
            "--experimental-lsp",
            "--max-session-turns",
            str(max_session_turns),
            "--max-tool-calls",
            str(max_tool_calls),
            "--max-subagent-depth",
            "1",
            "--max-wall-time",
            f"{max_wall_time_seconds}s",
            "--append-system-prompt",
            self._system_prompt(stage_path),
        )
        return QwenCodeInvocation(
            command=command,
            environment=self.runtime.environment(runtime_state),
            cwd=stage_path,
            prompt=objective + "\n",
            runtime_state=runtime_state,
        )

    def run(
        self,
        *,
        stage: Path | str,
        objective: str,
        max_session_turns: int = 48,
        max_tool_calls: int = 80,
        max_wall_time_seconds: int = 900,
        emit: EventEmitter | None = None,
    ) -> dict[str, Any]:
        self.runtime.validate_deployed()
        self.runtime.state.mkdir(parents=True, exist_ok=True)
        stage_path = Path(stage).expanduser().resolve()
        start = time.monotonic()
        output = emit or (lambda _event: None)
        stderr_chunks: list[str] = []
        stderr_digest = hashlib.sha256()
        stdout_digest = hashlib.sha256()
        raw_stdout_bytes = 0
        protocol_error: str | None = None
        timed_out = False
        accumulator = QwenCodeEventAccumulator()
        lsp = _provision_lsp(stage_path, self.runtime)
        process: subprocess.Popen[str] | None = None
        owner: _OwnedProcess | None = None
        job_assigned = False
        exit_code: int | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="run-", dir=self.runtime.state) as temporary:
                invocation = self.build_invocation(
                    stage=stage_path,
                    objective=objective,
                    runtime_state=Path(temporary),
                    max_session_turns=max_session_turns,
                    max_tool_calls=max_tool_calls,
                    max_wall_time_seconds=max_wall_time_seconds,
                )
                output(
                    {
                        "type": "harness_start",
                        "harness": HARNESS_ID,
                        "harness_version": QWEN_CODE_VERSION,
                        "model": MODEL_ID,
                        "endpoint": ENDPOINT,
                        "context_tokens": CONTEXT_TOKENS,
                        "output_tokens": OUTPUT_TOKENS,
                        "transport_retry_limit": 0,
                        "lsp": {"mode": lsp.mode, "languages": list(lsp.languages)},
                    }
                )
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                process = subprocess.Popen(
                    list(invocation.command),
                    cwd=invocation.cwd,
                    env=dict(invocation.environment),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags,
                    start_new_session=os.name != "nt",
                )
                owner = _OwnedProcess(process)
                job_assigned = owner.job_assigned
                if process.stdin is None or process.stdout is None or process.stderr is None:
                    raise AdapterContractError("Qwen Code child streams were not created")
                stdout_lines: queue.Queue[str | None] = queue.Queue()
                stdout_thread = threading.Thread(
                    target=_stdout_reader, args=(process.stdout, stdout_lines), daemon=True
                )
                stderr_thread = threading.Thread(
                    target=_stderr_reader,
                    args=(process.stderr, stderr_chunks, stderr_digest),
                    daemon=True,
                )
                stdout_thread.start()
                stderr_thread.start()
                process.stdin.write(invocation.prompt)
                process.stdin.close()
                stdout_done = False
                deadline = start + max_wall_time_seconds + 30
                while not stdout_done or process.poll() is None:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        protocol_error = "Qwen Code exceeded its outer cleanup deadline"
                        owner.terminate_tree()
                        break
                    try:
                        line = stdout_lines.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if line is None:
                        stdout_done = True
                        continue
                    encoded = line.encode("utf-8", errors="replace")
                    raw_stdout_bytes += len(encoded)
                    stdout_digest.update(encoded)
                    if raw_stdout_bytes > MAX_STREAM_BYTES:
                        protocol_error = "Qwen Code headless stream exceeded 64 MiB"
                        owner.terminate_tree()
                        break
                    if not line.strip():
                        continue
                    try:
                        raw_event = strict_json_loads(line)
                        normalized = accumulator.accept(raw_event)
                    except QwenCodeEventError as error:
                        protocol_error = str(error)
                        owner.terminate_tree()
                        break
                    for event in normalized:
                        if (
                            event.get("type") == "tool_call"
                            and event.get("tool") not in ALLOWED_TOOLS
                        ):
                            protocol_error = (
                                f"Qwen Code exposed a disabled production tool: {event.get('tool')}"
                            )
                            owner.terminate_tree()
                            break
                        output({"harness": HARNESS_ID, **event})
                    if protocol_error is not None:
                        break
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    owner.terminate_tree()
                    process.wait(timeout=10)
                exit_code = process.returncode
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)
                # Close the kill-on-close Job before the ephemeral runtime directory is
                # removed. This deterministically retires any LSP or shell descendant
                # that outlived the Qwen Code parent.
                owner.close()
                if protocol_error is None and not timed_out and exit_code == 0:
                    try:
                        official = accumulator.finish()
                    except QwenCodeEventError as error:
                        protocol_error = str(error)
                else:
                    official = None
        except (OSError, ValueError, AdapterContractError) as error:
            protocol_error = str(error)
            official = None
            if owner is not None:
                owner.terminate_tree()
        finally:
            if owner is not None:
                owner.close()
            if process is not None:
                exit_code = process.poll()
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            if lsp.remove_after and lsp.path is not None:
                lsp.path.unlink(missing_ok=True)

        stderr_text = "".join(stderr_chunks)
        passed = (
            protocol_error is None
            and not timed_out
            and exit_code == 0
            and official is not None
            and not official["is_error"]
            and not official.get("permission_denials")
        )
        result = {
            "schema_version": 1,
            "harness": HARNESS_ID,
            "harness_version": QWEN_CODE_VERSION,
            "status": "completed" if passed else "failed",
            "model": MODEL_ID,
            "endpoint": ENDPOINT,
            "context_tokens": CONTEXT_TOKENS,
            "output_tokens": OUTPUT_TOKENS,
            "transport_retry_limit": 0,
            "lsp": {"mode": lsp.mode, "languages": list(lsp.languages)},
            "official": official,
            "telemetry": {
                "duration_ms": round((time.monotonic() - start) * 1000, 3),
                "child_exit_code": exit_code,
                "child_cleaned": process is None or process.poll() is not None,
                "job_assigned": job_assigned,
                "timed_out": timed_out,
                "stdout_bytes": raw_stdout_bytes,
                "stdout_sha256": stdout_digest.hexdigest(),
                "stderr_sha256": stderr_digest.hexdigest(),
                "stderr_tail": stderr_text[-16_384:],
            },
            "error": protocol_error,
        }
        output({"type": "harness_terminal", **result})
        return result


def json_emitter(event: dict[str, Any]) -> None:
    print(canonical_json(event), flush=True)
