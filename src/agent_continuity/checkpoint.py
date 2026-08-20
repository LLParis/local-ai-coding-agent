from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from . import SCHEMA

MAX_TASK_BYTES = 64 * 1024
MAX_FILES = 512
MAX_HASH_BYTES = 256 * 1024 * 1024
MAX_ITEMS = 32
MAX_TEXT = 2_000

_TASK_KEYS = {
    "objective",
    "state",
    "current_focus",
    "scope",
    "completed",
    "next",
    "constraints",
    "decisions",
    "evidence",
}
_TASK_STATES = {"planned", "in_progress", "blocked", "complete"}
_LIST_KEYS = {"completed", "next", "constraints", "decisions", "evidence"}
_IGNORED_DIRS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    "node_modules",
    "DerivedData",
    ".build",
    ".venv",
    "venv",
}
_SENSITIVE_NAMES = {
    ".env",
    "credentials.json",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
}
_SENSITIVE_SUFFIXES = {".pem", ".p12", ".pfx", ".key"}
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{8,}",
        re.IGNORECASE,
    ),
)


class CheckpointError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, max_bytes: int) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CheckpointError(f"cannot read {path}: {exc}") from exc
    if size > max_bytes:
        raise CheckpointError(f"{path} exceeds the {max_bytes}-byte limit")
    try:
        raw = path.read_text(encoding="utf-8")
        _reject_secrets(raw)
        return json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"invalid JSON in {path}: {exc}") from exc


def _reject_secrets(text: str) -> None:
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise CheckpointError("secret-like material detected; checkpoint creation refused")


def _text(value: Any, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise CheckpointError(f"task.{field} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise CheckpointError(f"task.{field} must not be empty")
    if len(normalized) > MAX_TEXT:
        raise CheckpointError(f"task.{field} exceeds {MAX_TEXT} characters")
    return normalized


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CheckpointError("task.scope entries must be non-empty strings")
    raw = value.strip()
    if re.match(r"^[A-Za-z]:", raw):
        raise CheckpointError(f"task.scope path must stay inside the workspace: {value}")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise CheckpointError(f"task.scope path must stay inside the workspace: {value}")
    if raw.startswith("/") or raw.startswith("\\"):
        raise CheckpointError(f"task.scope path must stay inside the workspace: {value}")
    posix = PurePosixPath(raw.replace("\\", "/"))
    if ".." in posix.parts:
        raise CheckpointError(f"task.scope path must stay inside the workspace: {value}")
    normalized = posix.as_posix().removeprefix("./")
    if normalized in {"", "."}:
        raise CheckpointError("task.scope may not name the entire workspace; choose bounded paths")
    return normalized


def _normalize_task(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise CheckpointError("task context must be a JSON object")
    unknown = sorted(set(data) - _TASK_KEYS)
    if unknown:
        raise CheckpointError(f"unknown task context keys: {', '.join(unknown)}")

    task: dict[str, Any] = {
        "objective": _text(data.get("objective"), "objective", required=True),
        "state": _text(data.get("state"), "state", required=True),
        "current_focus": _text(data.get("current_focus"), "current_focus", required=True),
    }
    if task["state"] not in _TASK_STATES:
        raise CheckpointError(f"task.state must be one of: {', '.join(sorted(_TASK_STATES))}")

    scope_value = data.get("scope")
    if not isinstance(scope_value, list) or not scope_value:
        raise CheckpointError(
            "task.scope must contain at least one bounded workspace-relative path"
        )
    if len(scope_value) > 64:
        raise CheckpointError("task.scope exceeds 64 paths")
    task["scope"] = sorted(set(_relative_path(item) for item in scope_value))

    for key in sorted(_LIST_KEYS):
        value = data.get(key, [])
        if not isinstance(value, list) or len(value) > MAX_ITEMS:
            raise CheckpointError(f"task.{key} must be a list of at most {MAX_ITEMS} strings")
        task[key] = [_text(item, key, required=True) for item in value]

    _reject_secrets(_canonical(task).decode())
    return task


def load_task(path: Path) -> dict[str, Any]:
    return _normalize_task(_load_json(path, MAX_TASK_BYTES))


def _sensitive_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    for part in parts:
        lowered = part.lower()
        if lowered in _SENSITIVE_NAMES or lowered.startswith(".env."):
            return True
        if PurePosixPath(lowered).suffix in _SENSITIVE_SUFFIXES:
            return True
    return False


def _sha256_file(path: Path, budget: list[int]) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise CheckpointError(f"not a regular file: {path}")
        if budget[0] + before.st_size > MAX_HASH_BYTES:
            raise CheckpointError(
                f"scoped files exceed the {MAX_HASH_BYTES}-byte hashing budget; narrow task.scope"
            )
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ino,
    ):
        raise CheckpointError(f"file changed while checkpointing: {path}")
    budget[0] += after.st_size
    return digest.hexdigest(), after.st_size, after.st_mtime_ns


def _iter_scope_files(workspace: Path, scope: Iterable[str]) -> tuple[list[tuple[str, Path]], int]:
    found: dict[str, Path] = {}
    ignored_count = 0
    for relative in scope:
        candidate = workspace / relative
        if not candidate.exists() and not candidate.is_symlink():
            raise CheckpointError(f"task.scope path does not exist: {relative}")
        if _sensitive_path(relative):
            raise CheckpointError(f"task.scope includes a sensitive path: {relative}")
        if not candidate.is_symlink():
            try:
                candidate.resolve(strict=True).relative_to(workspace)
            except (OSError, ValueError) as exc:
                raise CheckpointError(
                    f"task.scope resolves outside the workspace: {relative}"
                ) from exc
        if candidate.is_symlink() or candidate.is_file():
            found[relative] = candidate
            continue
        if not candidate.is_dir():
            raise CheckpointError(f"unsupported task.scope path type: {relative}")
        for root, dirs, files in os.walk(candidate, followlinks=False):
            root_path = Path(root)
            kept_dirs: list[str] = []
            for name in sorted(dirs):
                child = root_path / name
                child_rel = child.relative_to(workspace).as_posix()
                if name in _IGNORED_DIRS:
                    ignored_count += 1
                elif child.is_symlink():
                    found[child_rel] = child
                else:
                    kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in sorted(files):
                child = root_path / name
                child_rel = child.relative_to(workspace).as_posix()
                if _sensitive_path(child_rel):
                    raise CheckpointError(f"scoped tree contains a sensitive path: {child_rel}")
                found[child_rel] = child
                if len(found) > MAX_FILES:
                    raise CheckpointError(
                        f"scoped tree exceeds {MAX_FILES} files; narrow task.scope"
                    )
    return sorted(found.items()), ignored_count


def workspace_manifest(workspace: Path, scope: Iterable[str]) -> dict[str, Any]:
    budget = [0]
    entries: list[dict[str, Any]] = []
    files, ignored_count = _iter_scope_files(workspace, scope)
    for relative, path in files:
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            entries.append(
                {
                    "kind": "symlink",
                    "mode": f"{mode:04o}",
                    "mtime_ns": info.st_mtime_ns,
                    "path": relative,
                    "sha256": hashlib.sha256(target.encode()).hexdigest(),
                    "size": len(target.encode()),
                }
            )
            continue
        digest, size, mtime_ns = _sha256_file(path, budget)
        entries.append(
            {
                "kind": "file",
                "mode": f"{mode:04o}",
                "mtime_ns": mtime_ns,
                "path": relative,
                "sha256": digest,
                "size": size,
            }
        )
    return {
        "entries": entries,
        "file_count": len(entries),
        "hash_bytes": budget[0],
        "ignored_directory_count": ignored_count,
        "limits": {"max_files": MAX_FILES, "max_hash_bytes": MAX_HASH_BYTES},
    }


def _git(workspace: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    result = subprocess.run(
        ["git", "-C", str(workspace), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
        env=env,
    )
    if check and result.returncode:
        raise CheckpointError(f"git {' '.join(args)} failed")
    return result


def git_context(workspace: Path) -> dict[str, Any]:
    inside = _git(workspace, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode or inside.stdout.strip() != b"true":
        return {"kind": "none"}
    root = _git(workspace, "rev-parse", "--show-toplevel").stdout.decode().strip()
    head_result = _git(workspace, "rev-parse", "HEAD", check=False)
    branch_result = _git(workspace, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    upstream_result = _git(
        workspace, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False
    )
    raw = _git(
        workspace,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        ".",
    ).stdout
    tokens = raw.split(b"\0")
    changes: list[dict[str, str]] = []
    redacted = 0
    index = 0
    while index < len(tokens) and tokens[index]:
        token = tokens[index].decode("utf-8", "surrogateescape")
        if len(token) < 4:
            raise CheckpointError("unexpected git status record")
        code, path = token[:2], token[3:]
        item = {"code": code, "path": path}
        if "R" in code or "C" in code:
            index += 1
            if index >= len(tokens) or not tokens[index]:
                raise CheckpointError("incomplete git rename/copy status record")
            item["original_path"] = tokens[index].decode("utf-8", "surrogateescape")
        if any(_sensitive_path(value) for key, value in item.items() if key.endswith("path")):
            redacted += 1
        else:
            changes.append(item)
        if len(changes) + redacted > MAX_FILES:
            raise CheckpointError(f"git status exceeds {MAX_FILES} paths; narrow the workspace")
        index += 1
    changes.sort(key=lambda item: (item["path"], item["code"], item.get("original_path", "")))
    return {
        "branch": branch_result.stdout.decode().strip() if branch_result.returncode == 0 else None,
        "changes": changes,
        "head": head_result.stdout.decode().strip() if head_result.returncode == 0 else None,
        "kind": "git",
        "redacted_sensitive_path_count": redacted,
        "root": str(Path(root).resolve()),
        "upstream": upstream_result.stdout.decode().strip()
        if upstream_result.returncode == 0
        else None,
    }


def build_payload(workspace: Path, task: dict[str, Any]) -> dict[str, Any]:
    resolved = workspace.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise CheckpointError(f"workspace is not a directory: {resolved}")
    return {
        "schema": SCHEMA,
        "task": task,
        "workspace": {
            "manifest": workspace_manifest(resolved, task["scope"]),
            "root": str(resolved),
            "vcs": git_context(resolved),
        },
    }


def envelope(payload: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256(_canonical(payload)).hexdigest()
    return {"digest": f"sha256:{digest}", "payload": payload}


def write_checkpoint(output: Path, value: dict[str, Any]) -> None:
    encoded = _canonical(value)
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if output.is_file() and output.read_bytes() == encoded:
            return
        raise CheckpointError(f"refusing to overwrite existing checkpoint: {output}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError as exc:
            raise CheckpointError(f"refusing to overwrite existing checkpoint: {output}") from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def create_checkpoint(workspace: Path, task_path: Path, output: Path) -> dict[str, Any]:
    value = envelope(build_payload(workspace, load_task(task_path)))
    _reject_secrets(_canonical(value).decode())
    write_checkpoint(output, value)
    return value


def load_checkpoint(path: Path) -> dict[str, Any]:
    value = _load_json(path.expanduser(), 2 * 1024 * 1024)
    if not isinstance(value, dict) or set(value) != {"digest", "payload"}:
        raise CheckpointError("checkpoint envelope must contain only digest and payload")
    payload = value["payload"]
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "task", "workspace"}
        or payload.get("schema") != SCHEMA
    ):
        raise CheckpointError("unsupported checkpoint schema")
    if not isinstance(payload.get("task"), dict):
        raise CheckpointError("checkpoint task context is malformed")
    normalized_task = _normalize_task(payload["task"])
    if normalized_task != payload["task"]:
        raise CheckpointError("checkpoint task context is not canonical")
    workspace = payload.get("workspace")
    if not isinstance(workspace, dict) or set(workspace) != {"manifest", "root", "vcs"}:
        raise CheckpointError("checkpoint workspace context is malformed")
    if not isinstance(workspace["root"], str) or not isinstance(workspace["manifest"], dict):
        raise CheckpointError("checkpoint workspace context is malformed")
    if not isinstance(workspace["vcs"], dict) or workspace["vcs"].get("kind") not in {
        "git",
        "none",
    }:
        raise CheckpointError("checkpoint VCS context is malformed")
    expected = "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()
    if value["digest"] != expected:
        raise CheckpointError("checkpoint digest mismatch")
    _reject_secrets(_canonical(value).decode())
    return value


def validate_checkpoint(
    path: Path, workspace: Path | None = None, require_current: bool = False
) -> dict[str, Any]:
    value = load_checkpoint(path)
    if require_current:
        if workspace is None:
            raise CheckpointError("--require-current requires --workspace")
        current = build_payload(workspace, value["payload"]["task"])
        if current["workspace"] != value["payload"]["workspace"]:
            raise CheckpointError("checkpoint is stale: scoped workspace state has changed")
    return value
