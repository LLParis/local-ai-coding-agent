"""Shared, fail-closed contract for harness qualification adapters."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

ADAPTER_SCHEMA_VERSION = 1
ALLOWED_TOOLS = ("list", "read", "search", "edit", "test")
GENERATED_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".build",
        ".gradle",
        ".swiftpm",
        "build",
        "dist",
        "node_modules",
        "target",
    }
)
GENERATED_SUFFIXES = frozenset({".pyc", ".pyo"})


class AdapterContractError(ValueError):
    """The capsule or runtime boundary is unsafe or malformed."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_relative_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise AdapterContractError("task paths must be non-empty strings")
    text = raw.replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or path.drive or any(part in ("", ".", "..") for part in path.parts):
        raise AdapterContractError(f"unsafe relative path: {raw!r}")
    return path.as_posix()


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _overlaps(left: Path, right: Path) -> bool:
    left_text = _normalized_path(left)
    right_text = _normalized_path(right)
    try:
        common = os.path.commonpath((left_text, right_text))
    except ValueError:
        return False
    return common in (left_text, right_text)


def _is_reparse_point(path: Path) -> bool:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def assert_no_reparse_path(root: Path, target: Path) -> None:
    root = root.resolve()
    target = target.absolute()
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise AdapterContractError(f"path escapes stage: {target}") from error
    current = root
    if _is_reparse_point(root):
        raise AdapterContractError(f"stage root is a reparse point: {root}")
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            raise AdapterContractError(f"reparse points are forbidden in a stage: {current}")


def validate_loopback_endpoint(raw: str) -> str:
    try:
        parsed = urlsplit(raw)
    except ValueError as error:
        raise AdapterContractError(f"invalid endpoint: {raw!r}") from error
    if parsed.scheme != "http":
        raise AdapterContractError("local adapter endpoint must use plain HTTP on loopback")
    if parsed.hostname not in ("127.0.0.1", "::1"):
        raise AdapterContractError("local adapter endpoint must use literal 127.0.0.1 or ::1")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AdapterContractError(
            "endpoint credentials, query strings, and fragments are forbidden"
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise AdapterContractError("endpoint port is invalid") from error
    if port is None or not (1 <= port <= 65535):
        raise AdapterContractError("endpoint must include a valid explicit port")
    path = parsed.path.rstrip("/")
    if path != "/v1":
        raise AdapterContractError("endpoint path must be exactly /v1")
    netloc = (
        f"[{parsed.hostname}]:{port}" if parsed.hostname == "::1" else f"{parsed.hostname}:{port}"
    )
    return urlunsplit(("http", netloc, "/v1", "", ""))


@dataclass(frozen=True)
class TaskCapsule:
    schema_version: int
    backend: str
    workspace: Path
    objective: str
    mutable: tuple[str, ...]
    context: tuple[str, ...]
    verify_context: tuple[str, ...]
    test_command: tuple[str, ...]
    timeout: int

    @classmethod
    def load(cls, path: Path | str) -> TaskCapsule:
        capsule_path = Path(path).resolve()
        try:
            raw = json.loads(capsule_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AdapterContractError(
                f"cannot load task capsule {capsule_path}: {error}"
            ) from error
        if not isinstance(raw, dict):
            raise AdapterContractError("task capsule must be a JSON object")
        required = {
            "schema_version",
            "backend",
            "workspace",
            "objective",
            "mutable",
            "context",
            "verify_context",
            "test_command",
            "timeout",
        }
        missing = sorted(required - raw.keys())
        if missing:
            raise AdapterContractError(f"task capsule missing fields: {', '.join(missing)}")
        if raw["schema_version"] != 1:
            raise AdapterContractError("only task capsule schema_version 1 is supported")
        workspace = Path(raw["workspace"]).expanduser().resolve()
        if not workspace.is_dir():
            raise AdapterContractError(f"source workspace does not exist: {workspace}")
        objective = raw["objective"]
        if not isinstance(objective, str) or not objective.strip():
            raise AdapterContractError("objective must be a non-empty string")

        def paths(name: str) -> tuple[str, ...]:
            value = raw[name]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise AdapterContractError(f"{name} must be an array of relative paths")
            normalized = tuple(dict.fromkeys(normalize_relative_path(item) for item in value))
            return normalized

        mutable = paths("mutable")
        context = paths("context")
        verify_context = paths("verify_context")
        if not mutable:
            raise AdapterContractError("mutable must name at least one staged path")
        if any(
            left == right
            or left.startswith(right.rstrip("/") + "/")
            or right.startswith(left.rstrip("/") + "/")
            for left in (*context, *mutable)
            for right in verify_context
        ):
            raise AdapterContractError("verify_context must remain outside model-readable context")
        command = raw["test_command"]
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item and "\x00" not in item for item in command)
        ):
            raise AdapterContractError("test_command must be a non-empty argv array")
        timeout = raw["timeout"]
        if not isinstance(timeout, int) or not 10 <= timeout <= 1800:
            raise AdapterContractError("timeout must be an integer from 10 through 1800 seconds")
        backend = raw["backend"]
        if not isinstance(backend, str) or not backend:
            raise AdapterContractError("backend must be a non-empty string")
        return cls(
            schema_version=1,
            backend=backend,
            workspace=workspace,
            objective=objective.strip(),
            mutable=mutable,
            context=context,
            verify_context=verify_context,
            test_command=tuple(command),
            timeout=timeout,
        )

    @property
    def readable(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.context, *self.mutable)))

    @property
    def scoped_source_paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.context, *self.mutable, *self.verify_context)))

    def validate_stage(self, stage: Path | str) -> Path:
        target = Path(stage).expanduser().resolve()
        if not target.is_dir():
            raise AdapterContractError(f"stage does not exist: {target}")
        if _overlaps(target, self.workspace):
            raise AdapterContractError("stage and source workspace must be disjoint")
        assert_no_reparse_path(target, target)
        for relative in self.scoped_source_paths:
            candidate = target / Path(relative)
            assert_no_reparse_path(target, candidate)
        for relative in (*self.context, *self.verify_context):
            if not (target / Path(relative)).exists():
                raise AdapterContractError(f"staged required path is missing: {relative}")
        return target


def _walk_files(root: Path) -> Iterable[Path]:
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in sorted(directory.iterdir(), key=lambda value: value.name.casefold()):
            if child.name in GENERATED_DIRECTORIES:
                continue
            if _is_reparse_point(child):
                raise AdapterContractError(f"reparse points are forbidden in a stage: {child}")
            if child.is_dir():
                pending.append(child)
            elif child.is_file() and child.suffix.casefold() not in GENERATED_SUFFIXES:
                yield child


def snapshot_tree(root: Path, *, max_files: int = 20_000) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for index, path in enumerate(_walk_files(root), start=1):
        if index > max_files:
            raise AdapterContractError(f"stage exceeds {max_files} files")
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = sha256_file(path)
    return snapshot


def snapshot_selected(root: Path, relative_paths: Iterable[str]) -> dict[str, str | None]:
    snapshot: dict[str, str | None] = {}
    for relative in relative_paths:
        target = root / Path(relative)
        if target.is_file():
            snapshot[relative] = sha256_file(target)
        elif target.is_dir():
            nested = snapshot_tree(target)
            for child, digest in nested.items():
                snapshot[f"{relative.rstrip('/')}/{child}"] = digest
        else:
            snapshot[relative] = None
    return snapshot


def path_allowed(relative: str, allowed: Iterable[str]) -> bool:
    try:
        normalized = normalize_relative_path(relative)
    except AdapterContractError:
        return False
    return any(
        normalized == root or normalized.startswith(root.rstrip("/") + "/") for root in allowed
    )


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
