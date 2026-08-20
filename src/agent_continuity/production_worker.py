"""Live autonomous local coding worker built around the official Qwen Code harness."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from adapters.protocol import AdapterContractError, assert_no_reparse_path, canonical_json
from adapters.qwen_code import QwenCodeAdapter

from .memory_store import MemoryScope, MemoryStore
from .run_memory import default_memory_root

_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    "target",
    "runs",
}
_IGNORED_FILES = {
    ".env",
    ".env.local",
    ".env.production",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.json",
}
_IGNORED_SUFFIXES = {".gguf", ".safetensors", ".pt", ".pth", ".ckpt"}


class ProductionWorkerError(RuntimeError):
    pass


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _safe_relative(path: Path) -> str:
    value = PurePosixPath(path.as_posix())
    if value.is_absolute() or ".." in value.parts or value.as_posix() in {"", "."}:
        raise ProductionWorkerError(f"unsafe repository-relative path: {path}")
    return value.as_posix()


def _included_file(path: Path) -> bool:
    name = path.name.casefold()
    return (
        name not in _IGNORED_FILES
        and not name.startswith(".env.")
        and path.suffix.casefold() not in _IGNORED_SUFFIXES
    )


def _files(root: Path, *, maximum: int = 50_000, max_bytes: int = 2 * 1024**3) -> list[Path]:
    output: list[Path] = []
    used = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        assert_no_reparse_path(root, directory)
        for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if child.name in _IGNORED_DIRS:
                continue
            assert_no_reparse_path(root, child)
            if child.is_dir():
                pending.append(child)
            elif child.is_file() and _included_file(child):
                output.append(child)
                used += child.stat().st_size
                if len(output) > maximum or used > max_bytes:
                    raise ProductionWorkerError("repository exceeds the live worker copy budget")
    return output


def _manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in _files(root):
        relative = _safe_relative(path.relative_to(root))
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        result[relative] = "sha256:" + digest.hexdigest()
    return result


def _copy_repository(source: Path, stage: Path) -> dict[str, str]:
    stage.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, str] = {}
    for path in _files(source):
        relative = _safe_relative(path.relative_to(source))
        target = stage / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        manifest[relative] = _sha256_bytes(target.read_bytes())
    return manifest


def _changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def _text(path: Path) -> list[str] | None:
    try:
        return path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeError):
        return None


def _diff(source: Path, stage: Path, changed: list[str]) -> str:
    sections: list[str] = []
    for relative in changed:
        original_path = source / Path(relative)
        staged_path = stage / Path(relative)
        original = _text(original_path) if original_path.is_file() else []
        staged = _text(staged_path) if staged_path.is_file() else []
        if original is None or staged is None:
            sections.append(f"Binary change: {relative}\n")
            continue
        sections.extend(
            difflib.unified_diff(
                original,
                staged,
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
    value = "".join(sections)
    if len(value.encode("utf-8")) > 8 * 1024 * 1024:
        raise ProductionWorkerError("live diff exceeds 8 MiB")
    return value


class Trajectory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, value: dict[str, Any]) -> None:
        encoded = (canonical_json(value) + "\n").encode("utf-8")
        with self.path.open("ab", buffering=0) as handle:
            handle.write(encoded)
            os.fsync(handle.fileno())


@dataclass
class RepairController:
    run_id: str
    trajectory: Trajectory
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    failure_signatures: set[str] = field(default_factory=set)
    pending_failure: str | None = None
    repairs: int = 0
    last_verification_call: dict[str, Any] | None = None
    last_verification_failed: bool = False

    def __call__(self, event: dict[str, Any]) -> None:
        event = dict(event)
        kind = event.get("type")
        if kind == "tool_call":
            call_id = event.get("call_id")
            if isinstance(call_id, str):
                event["effect_id"] = str(uuid.uuid5(uuid.UUID(self.run_id), call_id))
                event["intent"] = (
                    "side_effect"
                    if event.get("tool") in {"edit", "write_file", "run_shell_command"}
                    else "read"
                )
                event["denied"] = False
                self.calls[call_id] = event
            if event.get("tool") in {"edit", "write_file"} and self.pending_failure is not None:
                self.repairs += 1
                if self.repairs > 2:
                    raise AdapterContractError("third repair pass is forbidden")
                self.pending_failure = None
        elif kind == "tool_result" and event.get("tool") == "run_shell_command":
            call = self.calls.get(str(event.get("call_id")), {})
            if call.get("effect_id"):
                event["effect_id"] = call["effect_id"]
            command = canonical_json(call.get("input", {})).casefold()
            if any(word in command for word in _VERIFY_WORDS):
                self.last_verification_call = call
                self.last_verification_failed = bool(event.get("is_error"))
            if bool(event.get("is_error")):
                signature = str(event.get("result_sha256", ""))
                if not signature:
                    raise AdapterContractError("failed command omitted its evidence signature")
                if signature in self.failure_signatures:
                    raise AdapterContractError("identical failure repeated without new evidence")
                self.failure_signatures.add(signature)
                self.pending_failure = signature
            else:
                self.pending_failure = None
        self.trajectory.write(event)


class MemoryJournal:
    def __init__(self, source: Path, objective: str, run_id: str) -> None:
        self.task_id = str(uuid.uuid4())
        self.session_id = str(uuid.uuid4())
        self.host_id = "EXCALIBUR"
        self.parent: str | None = None
        workspace_id = _sha256_text(os.path.normcase(str(source)))
        self.scope = MemoryScope(
            owner_id="local-owner",
            workspace_id=workspace_id,
            task_id=self.task_id,
            agent_role="implementer",
            session_id=self.session_id,
        )
        self.store = MemoryStore(default_memory_root())
        self.append(
            "task/started",
            {
                "run_id": run_id,
                "objective_sha256": _sha256_text(objective),
                "source_sha256": workspace_id,
            },
        )

    def append(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = self.store.append(
            host_id=self.host_id,
            session_id=self.session_id,
            task_id=self.task_id,
            event_type=event_type,
            actor={"kind": "agent", "id": "coding-intelligence-production"},
            scope=self.scope,
            payload=payload,
            parent_event_id=self.parent,
            retention_class="core",
        )
        self.parent = str(event["event_id"])
        return event


def _switch(repository: Path, backend: str) -> dict[str, Any]:
    command = [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(repository / "windows" / "Switch-ExcaliburBackend.ps1"),
        "-Backend",
        backend,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        raise ProductionWorkerError(f"backend switch failed: {result.stderr[-4000:]}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise ProductionWorkerError("backend switch returned no report")
    return json.loads(lines[-1])


def _devstral_review(objective: str, diff: str) -> dict[str, Any]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "reason", "risks"],
        "properties": {
            "verdict": {"type": "string", "enum": ["accepted", "rejected"]},
            "reason": {"type": "string"},
            "risks": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        },
    }
    prompt = (
        "Review this completed local coding diff. Deterministic execution remains final "
        "authority; identify only material objective violations or risks.\n\nOBJECTIVE:\n"
        + objective
        + "\n\nDIFF:\n"
        + diff[:200_000]
    )
    payload = {
        "model": "devstral-small-2:24b",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": schema,
        "options": {"temperature": 0, "num_predict": 1024},
    }
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        document = json.loads(response.read().decode("utf-8"))
    message = document.get("message", {})
    value = json.loads(message.get("content", "{}"))
    if value.get("verdict") not in {"accepted", "rejected"}:
        raise ProductionWorkerError("Devstral returned an invalid review")
    return value


def _apply(source: Path, stage: Path, before: dict[str, str], changed: list[str]) -> None:
    if _manifest(source) != before:
        raise ProductionWorkerError("source changed during the live run; refusing promotion")
    for relative in changed:
        source_path = source / Path(relative)
        stage_path = stage / Path(relative)
        assert_no_reparse_path(source, source_path if source_path.exists() else source_path.parent)
        if stage_path.is_file():
            source_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = source_path.with_name(f".{source_path.name}.{uuid.uuid4().hex}.tmp")
            shutil.copy2(stage_path, temporary)
            os.replace(temporary, source_path)
        elif source_path.is_file():
            source_path.unlink()


def run(repository: Path, objective: str, *, stage_only: bool, stage_root: Path) -> dict[str, Any]:
    source = repository.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ProductionWorkerError("repository must be a directory")
    assert_no_reparse_path(source, source)
    run_id = str(uuid.uuid4())
    run_root = stage_root.expanduser().resolve() / run_id
    stage = run_root / "workspace"
    run_root.mkdir(parents=True, exist_ok=False)
    trajectory = Trajectory(run_root / "trajectory.jsonl")
    source_before = _manifest(source)
    stage_before = _copy_repository(source, stage)
    journal = MemoryJournal(source, objective, run_id)
    controller = RepairController(run_id, trajectory)
    compiled = (
        objective.strip()
        + "\n\nImplement this complete objective now. Inspect before editing. Run the real "
        "repository build, launch, compiler, or operational command needed to make the "
        "result live. Do not create a new test or benchmark project. If a real command "
        "fails, use only materially new "
        "failure evidence for at most two targeted repair passes; stop on an identical "
        "failure. Do not merely explain the patch: finish the working repository result."
    )
    journal.append(
        "model/request",
        {
            "run_id": run_id,
            "harness": "qwen-code-model-aligned",
            "model": "arm-qwen38-q6-text",
            "objective_sha256": _sha256_text(compiled),
            "automatic_retries": 0,
        },
    )
    started = time.monotonic()
    result = QwenCodeAdapter().run(stage=stage, objective=compiled, emit=controller)
    stage_after = _manifest(stage)
    source_after_worker = _manifest(source)
    changed = _changed(stage_before, stage_after)
    diff = _diff(source, stage, changed)
    (run_root / "diff.patch").write_text(diff, encoding="utf-8")
    (run_root / "qwen-code-result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    official = result.get("official") or {}
    tool_records = ((official.get("tool_calls") or {}).get("records") or [])
    verification_calls = [item for item in tool_records if item.get("tool") == "run_shell_command"]
    implementation_ok = (
        result.get("status") == "completed"
        and source_before == source_after_worker
        and bool(changed)
        and bool(verification_calls)
        and not bool(verification_calls[-1].get("is_error"))
        and controller.repairs <= 2
    )
    journal.append(
        "model/response",
        {
            "run_id": run_id,
            "status": result.get("status"),
            "response_sha256": _sha256_text(canonical_json(result)),
            "model_calls": ((official.get("model_calls") or {}).get("total") or 0),
            "tool_calls": ((official.get("tool_calls") or {}).get("total") or 0),
            "repair_passes": controller.repairs,
            "automatic_retries": 0,
        },
    )
    review: dict[str, Any] = {"status": "not_run"}
    restore: dict[str, Any] | None = None
    if implementation_ok:
        try:
            _switch(Path(__file__).resolve().parents[2], "Ollama")
            review = {"status": "completed", **_devstral_review(objective, diff)}
        except Exception as error:  # advisory review never fabricates a result
            review = {"status": "unavailable", "error": str(error)}
        finally:
            restore = _switch(Path(__file__).resolve().parents[2], "Qwen38")
    applied = False
    if implementation_ok and not stage_only:
        _apply(source, stage, source_before, changed)
        applied = True
    status = "verified" if implementation_ok else "failed"
    final = {
        "schema": "coding-intelligence-production-worker/v1",
        "status": status,
        "run_id": run_id,
        "repository": str(source),
        "objective": objective,
        "harness": "qwen-code-model-aligned+deepseek-events+pi-tools+command-center",
        "model": "arm-qwen38-q6-text",
        "stage": str(stage),
        "trajectory": str(trajectory.path),
        "changed_paths": changed,
        "diff_sha256": _sha256_text(diff),
        "repairs": controller.repairs,
        "model_calls": ((official.get("model_calls") or {}).get("total") or 0)
        + (1 if review.get("status") == "completed" else 0),
        "tool_calls": ((official.get("tool_calls") or {}).get("total") or 0),
        "automatic_retries": 0,
        "verification_command": verification_calls[-1].get("input") if verification_calls else None,
        "verification_passed": bool(verification_calls)
        and not bool(verification_calls[-1].get("is_error")),
        "devstral": review,
        "backend_restore": restore,
        "source_unchanged_during_worker": source_before == source_after_worker,
        "applied": applied,
        "stage_only": stage_only,
        "duration_seconds": round(time.monotonic() - started, 3),
        "memory_root": str(default_memory_root()),
    }
    journal.append(
        "verification/result",
        {
            "run_id": run_id,
            "status": status,
            "diff_sha256": final["diff_sha256"],
            "changed_paths_sha256": _sha256_text(canonical_json(changed)),
            "verification_passed": final["verification_passed"],
            "devstral_status": review.get("status"),
            "applied": applied,
        },
    )
    journal.append(
        "task/completed" if status == "verified" else "task/failed",
        {
            "run_id": run_id,
            "status": status,
            "model_calls": final["model_calls"],
            "tool_calls": final["tool_calls"],
            "repair_passes": controller.repairs,
            "automatic_retries": 0,
        },
    )
    (run_root / "result.json").write_text(
        json.dumps(final, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return final


def _default_stage_root() -> Path:
    return Path(r"D:\11_CS\00_REPOS\_coding-intelligence-stages")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("objective", nargs="+")
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--stage-root", type=Path, default=_default_stage_root())
    args = parser.parse_args(argv)
    try:
        value = run(
            args.repository,
            " ".join(args.objective),
            stage_only=args.stage_only,
            stage_root=args.stage_root,
        )
    except Exception as error:
        print(
            json.dumps(
                {
                    "schema": "coding-intelligence-production-worker/v1",
                    "status": "failed",
                    "error": str(error),
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(value, sort_keys=True))
    return 0 if value["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
