from __future__ import annotations

import difflib
import http.client
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit


class LocalEditError(RuntimeError):
    pass


_IGNORED = {".git", "__pycache__", "node_modules", ".venv", "venv"}


def _relative(value: Path) -> str:
    raw = value.as_posix()
    windows_path = PureWindowsPath(str(value))
    path = PurePosixPath(raw.replace("\\", "/"))
    if (
        value.is_absolute()
        or bool(value.anchor)
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() in {"", "."}
    ):
        raise LocalEditError(f"path must stay inside the workspace: {value}")
    return path.as_posix()


def _copy_path(workspace: Path, stage: Path, relative: str) -> None:
    source = workspace / relative
    destination = stage / relative
    if source.is_symlink() or not source.exists():
        raise LocalEditError(f"context path is missing or a symlink: {relative}")
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return
    shutil.copytree(
        source,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*_IGNORED),
    )


def _is_within(relative: str, root: str) -> bool:
    path = PurePosixPath(relative)
    parent = PurePosixPath(root)
    return path == parent or parent in path.parents


def _text_files(
    stage: Path,
    roots: list[str],
    *,
    excluded_roots: list[str] | None = None,
    max_bytes: int = 512 * 1024,
) -> str:
    excluded = excluded_roots or []

    def visible(relative: str) -> bool:
        return not any(_is_within(relative, root) for root in excluded)

    files: dict[str, Path] = {}
    for relative in roots:
        candidate = stage / relative
        if candidate.is_file() and visible(relative):
            files[relative] = candidate
        elif candidate.is_dir():
            for path in candidate.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    nested = path.relative_to(stage).as_posix()
                    if visible(nested):
                        files[nested] = path
    sections: list[str] = []
    used = 0
    for relative, path in sorted(files.items()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        used += len(text.encode("utf-8"))
        if used > max_bytes:
            raise LocalEditError("context exceeds the 512 KiB thin-run limit")
        sections.append(f"\n===== {relative} =====\n{text}")
    if not sections:
        raise LocalEditError("no UTF-8 context files were supplied")
    return "".join(sections)


def _response_text(response: dict[str, object]) -> str:
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
    direct = response.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    outputs = response.get("output")
    if isinstance(outputs, list):
        for output in outputs:
            if not isinstance(output, dict):
                continue
            content = output.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    return item["text"]
    raise LocalEditError("local model response contained no output text")


def run_local_edit(
    *,
    workspace: Path,
    objective: str,
    mutable: list[Path],
    context: list[Path],
    test_command: list[str],
    base_url: str,
    model: str,
    timeout: float,
    verify_context: list[Path] | None = None,
) -> dict[str, object]:
    workspace = workspace.expanduser().resolve(strict=True)
    mutable_paths = sorted({_relative(path) for path in mutable})
    context_paths = sorted({_relative(path) for path in context} | set(mutable_paths))
    verify_paths = sorted({_relative(path) for path in (verify_context or [])})
    if not objective.strip() or not mutable_paths or not test_command:
        raise LocalEditError("objective, mutable paths, and test command are required")
    for mutable_path in mutable_paths:
        if any(_is_within(mutable_path, hidden_root) for hidden_root in verify_paths):
            raise LocalEditError("verify context may not contain a mutable path")

    stage = Path(tempfile.mkdtemp(prefix="continuity-local-"))
    for relative in sorted(set(context_paths) | set(verify_paths)):
        _copy_path(workspace, stage, relative)
    root_instructions = workspace / "AGENTS.md"
    if root_instructions.is_file() and "AGENTS.md" not in context_paths:
        _copy_path(workspace, stage, "AGENTS.md")
        context_paths.append("AGENTS.md")

    context_text = _text_files(stage, context_paths, excluded_roots=verify_paths)
    prompt = (
        f"Objective: {objective.strip()}\n"
        f"Only these files may change: {json.dumps(mutable_paths)}\n"
        "Inspect the supplied repository files and return the smallest complete correction. "
        "For each edit, old_text must be copied verbatim and occur exactly once. Preserve "
        "unrelated behavior and do not change tests.\n"
        f"{context_text}"
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "diagnosis": {"type": "string"},
            "edits": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "enum": mutable_paths},
                        "old_text": {"type": "string", "minLength": 1},
                        "new_text": {"type": "string", "minLength": 1},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
        },
        "required": ["diagnosis", "edits"],
    }
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return only the requested structured coding edit."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 4096,
        "stream": False,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "local_edit", "strict": True, "schema": schema},
        },
    }
    parsed_url = urlsplit(base_url)
    if parsed_url.scheme != "http" or parsed_url.hostname not in {"127.0.0.1", "localhost"}:
        raise LocalEditError("base URL must be an HTTP loopback endpoint")
    port = parsed_url.port or 80
    prefix = parsed_url.path.rstrip("/")
    body = json.dumps(request, separators=(",", ":")).encode()
    started = time.monotonic()
    connection = http.client.HTTPConnection(parsed_url.hostname, port, timeout=timeout)
    connection.request("POST", f"{prefix}/chat/completions", body, {"Content-Type": "application/json"})
    response = connection.getresponse()
    raw = response.read(8 * 1024 * 1024)
    connection.close()
    if response.status != 200:
        raise LocalEditError(f"local model HTTP {response.status}: {raw[:1000]!r}")
    try:
        response_document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LocalEditError("local model endpoint returned invalid JSON") from exc
    model_text = _response_text(response_document)
    try:
        candidate = json.loads(model_text)
    except json.JSONDecodeError as exc:
        raise LocalEditError(f"local model returned non-JSON text: {model_text[:2000]!r}") from exc
    inference_seconds = time.monotonic() - started

    if not isinstance(candidate, dict) or set(candidate) != {"diagnosis", "edits"}:
        raise LocalEditError("local model returned an unexpected edit shape")
    diagnosis = candidate["diagnosis"]
    edits = candidate["edits"]
    if not isinstance(diagnosis, str) or not diagnosis.strip():
        raise LocalEditError("local model diagnosis must be a non-empty string")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 4:
        raise LocalEditError("local model must return between 1 and 4 edits")
    before: dict[str, str] = {}
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"path", "old_text", "new_text"}:
            raise LocalEditError("local model returned a malformed edit")
        relative = edit["path"]
        if relative not in mutable_paths:
            raise LocalEditError("local model attempted an out-of-scope edit")
        target = stage / relative
        current = target.read_text(encoding="utf-8")
        old_text, new_text = edit["old_text"], edit["new_text"]
        if not isinstance(old_text, str) or current.count(old_text) != 1:
            raise LocalEditError(f"old_text does not bind exactly once in {relative}")
        if not isinstance(new_text, str) or not new_text or len(new_text) > 64 * 1024:
            raise LocalEditError(f"new_text is invalid for {relative}")
        before.setdefault(relative, current)
        target.write_text(current.replace(old_text, new_text, 1), encoding="utf-8")

    diff_parts: list[str] = []
    for relative, original in sorted(before.items()):
        corrected = (stage / relative).read_text(encoding="utf-8")
        diff_parts.extend(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                corrected.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
    test_started = time.monotonic()
    test = subprocess.run(
        test_command,
        cwd=stage,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    return {
        "status": "verified" if test.returncode == 0 else "failed",
        "model": model,
        "stage": str(stage),
        "diagnosis": diagnosis,
        "diff": "".join(diff_parts),
        "inference_seconds": round(inference_seconds, 3),
        "model_calls": 1,
        "test_seconds": round(time.monotonic() - test_started, 3),
        "test_command": test_command,
        "test_exit": test.returncode,
        "test_output": test.stdout,
    }
