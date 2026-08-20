#!/usr/bin/env python3
"""Send one hash-bound Swift stage to the trusted Mac verifier over one SSH session."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from agent_continuity.checkpoint import CheckpointError, _reject_secrets  # noqa: E402
from agent_continuity.run_memory import RunMemoryError, ensure_secret_free  # noqa: E402

SNAPSHOT_SCHEMA = "coding-intelligence.mac-swift-source-snapshot/v1"
TRANSFER_SCHEMA = "coding-intelligence.mac-swift-transfer/v1"
REMOTE_SCHEMA = "coding-intelligence.mac-swift-remote-result/v1"
RESULT_SCHEMA = "coding-intelligence.mac-swift-verifier-result/v1"
VERIFIER_ID = "mac-swift"
SSH_ALIAS = "coding-intelligence-mac"
SSH_HOST = "100.122.229.30"
SSH_USER = "pro"
SSH_CLIENT_KEY_FINGERPRINT = "SHA256:cuVjPoOydWy3SeNQCTq0SGIA5XO5GCZIbx6xIl7hfuA"
SSH_SERVER_KEY_FINGERPRINT = "SHA256:iq2K0aLXv0H5xGTnFtAbEM2oUMMf6uzPZ3+p0G0Nb1w"
EXPECTED_MAC: dict[str, object] = {
    "hostname": "Londons-MacBook-Pro.local",
    "user": "pro",
    "uid": 501,
    "architecture": "x86_64",
    "os_version": "26.5.2",
    "developer_dir": "/Applications/Xcode.app/Contents/Developer",
    "swift_path": (
        "/Applications/Xcode.app/Contents/Developer/Toolchains/"
        "XcodeDefault.xctoolchain/usr/bin/swift"
    ),
    "swift_stdout_sha256": (
        "sha256:94c4c327428d284bdb4a4e7a36c320e96012abee788ce0e295a0bbda865dde42"
    ),
    "swift_stderr_sha256": (
        "sha256:542ca724f46d8a3dffb85c3cd4b6e0d2fdf6c1bd4c484e6978dd9ed2f22cd639"
    ),
}
MAX_FILES = 4096
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 48 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_SSH_STDOUT = 2 * 1024 * 1024
MAX_SSH_STDERR = 128 * 1024
STDOUT_LIMIT = 256 * 1024
STDERR_LIMIT = 256 * 1024
IGNORED_COMPONENTS = {
    ".build",
    ".git",
    ".ssh",
    ".swiftpm",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
}
SENSITIVE_NAMES = {
    ".env",
    "credentials.json",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
}
SENSITIVE_SUFFIXES = {".key", ".mobileprovision", ".p12", ".pem", ".pfx"}
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class MacVerifierError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MacVerifierError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise MacVerifierError(f"{field} is not a canonical SHA-256")
    return value


def _relative(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise MacVerifierError(f"{field} must be a bounded relative path")
    if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        raise MacVerifierError(f"{field} contains a forbidden character")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or value in {".", ".."}
        or ".." in posix.parts
        or any(part in {"", "."} for part in posix.parts)
    ):
        raise MacVerifierError(f"{field} must stay inside the stage")
    return posix.as_posix()


def _is_reparse(info: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & flag)


def _load_task(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    expected_sha256 = _digest(expected_sha256, "expected_task_sha256")
    try:
        candidate = path.expanduser().absolute()
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise MacVerifierError("task manifest must be a real file")
        raw = candidate.resolve(strict=True).read_bytes()
    except OSError as exc:
        raise MacVerifierError("task manifest cannot be read") from exc
    if len(raw) > 1024 * 1024 or _sha256(raw) != expected_sha256:
        raise MacVerifierError("task manifest hash differs from the wrapper checkpoint")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MacVerifierError("task manifest is not unique-key UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise MacVerifierError("task manifest must be one object")
    allowed = {
        "schema_version",
        "backend",
        "workspace",
        "objective",
        "mutable",
        "context",
        "verify_context",
        "test_command",
        "timeout",
        "execution_verifier",
    }
    if set(value) - allowed:
        raise MacVerifierError("task manifest contains fields outside schema v1")
    if value.get("schema_version") != 1 or value.get("execution_verifier") != VERIFIER_ID:
        raise MacVerifierError("task does not select the trusted mac-swift verifier")
    for field in ("mutable", "context", "verify_context", "test_command"):
        if not isinstance(value.get(field), list) or not value[field]:
            raise MacVerifierError(f"task.{field} must be a non-empty array")
    return value, expected_sha256


def _command(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not 2 <= len(value) <= 32
        or value[:2] != ["swift", "test"]
        or any(not isinstance(arg, str) or not arg or len(arg) > 4096 for arg in value)
    ):
        raise MacVerifierError("mac-swift test_command must begin with separate swift, test argv")
    if any("\x00" in arg or "\n" in arg or "\r" in arg for arg in value):
        raise MacVerifierError("mac-swift test_command contains control characters")
    allowed_flags = {
        "--parallel",
        "--no-parallel",
        "--enable-code-coverage",
        "--disable-code-coverage",
        "--verbose",
        "-v",
    }
    pair_flags = {"--configuration", "--filter", "--skip", "--package-path"}
    index = 2
    while index < len(value):
        item = value[index]
        if item in allowed_flags:
            index += 1
            continue
        if item not in pair_flags or index + 1 >= len(value):
            raise MacVerifierError(f"mac-swift test argument is outside the argv contract: {item}")
        argument = value[index + 1]
        if item == "--configuration" and argument not in {"debug", "release"}:
            raise MacVerifierError("Swift configuration must be debug or release")
        if item == "--package-path" and argument != ".":
            raise MacVerifierError("Swift package path must be the transferred stage root")
        if item in {"--filter", "--skip"} and (
            "/" in argument or "\\" in argument or ".." in argument
        ):
            raise MacVerifierError("Swift test filter is not a bounded identifier")
        index += 2
    return list(value)


def _array_paths(task: dict[str, Any], field: str) -> list[str]:
    return sorted({_relative(value, f"task.{field}") for value in task[field]})


def _is_within(relative: str, root: str) -> bool:
    path = PurePosixPath(relative)
    parent = PurePosixPath(root)
    return path == parent or parent in path.parents


def _check_real_component(root: Path, relative: str) -> Path:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise MacVerifierError(f"explicit path is missing: {relative}") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise MacVerifierError(f"explicit path is a symlink or reparse point: {relative}")
    resolved = current.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MacVerifierError(f"explicit path escapes its root: {relative}") from exc
    return current


def _reject_sensitive_path(relative: str) -> None:
    path = PurePosixPath(relative)
    lowered = {part.lower() for part in path.parts}
    if lowered & {item.lower() for item in IGNORED_COMPONENTS | SENSITIVE_NAMES}:
        raise MacVerifierError(f"sensitive or generated path is not transferable: {relative}")
    if path.suffix.lower() in SENSITIVE_SUFFIXES:
        raise MacVerifierError(f"sensitive file suffix is not transferable: {relative}")


def _collect(
    root_value: Path, task: dict[str, Any]
) -> tuple[list[dict[str, object]], dict[str, bytes]]:
    candidate_root = root_value.expanduser().absolute()
    info = candidate_root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise MacVerifierError("collection root must be a real directory")
    root = candidate_root.resolve(strict=True)
    mutable = _array_paths(task, "mutable")
    context = _array_paths(task, "context")
    hidden = _array_paths(task, "verify_context")
    if any(any(_is_within(path, verifier) for verifier in hidden) for path in mutable):
        raise MacVerifierError("hidden verifier scope overlaps a mutable path")

    found: dict[str, Path] = {}
    for relative in sorted(set(mutable + context + hidden)):
        candidate = _check_real_component(root, relative)
        if candidate.is_file():
            found[relative] = candidate
            continue
        if not candidate.is_dir():
            raise MacVerifierError(f"explicit path is not a file or directory: {relative}")
        for directory, directories, files in os.walk(candidate, topdown=True, followlinks=False):
            directory_path = Path(directory)
            safe_directories: list[str] = []
            for name in directories:
                nested = directory_path / name
                nested_info = nested.lstat()
                nested_relative = nested.relative_to(root).as_posix()
                if name in IGNORED_COMPONENTS:
                    continue
                if stat.S_ISLNK(nested_info.st_mode) or _is_reparse(nested_info):
                    raise MacVerifierError(
                        f"explicit tree contains a symlink or reparse point: {nested_relative}"
                    )
                safe_directories.append(name)
            directories[:] = safe_directories
            for name in files:
                path = directory_path / name
                nested_relative = path.relative_to(root).as_posix()
                path_info = path.lstat()
                if stat.S_ISLNK(path_info.st_mode) or _is_reparse(path_info):
                    raise MacVerifierError(
                        f"explicit tree contains a symlink or reparse point: {nested_relative}"
                    )
                found[nested_relative] = path
    if not found or len(found) > MAX_FILES:
        raise MacVerifierError("explicit transfer file count is outside the bound")

    entries: list[dict[str, object]] = []
    payload: dict[str, bytes] = {}
    total = 0
    for relative, path in sorted(found.items()):
        _reject_sensitive_path(relative)
        raw = path.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            raise MacVerifierError(f"explicit file exceeds the per-file bound: {relative}")
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise MacVerifierError("explicit transfer exceeds the total byte bound")
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise MacVerifierError(f"non-UTF-8 file is not transferable: {relative}") from exc
        try:
            _reject_secrets(text)
            ensure_secret_free({"path": relative, "content": text})
        except (CheckpointError, RunMemoryError) as exc:
            raise MacVerifierError(
                f"secret-like material refused before transfer: {relative}"
            ) from exc
        role = (
            "hidden_verifier"
            if any(_is_within(relative, verifier) for verifier in hidden)
            else "stage"
        )
        entries.append(
            {
                "path": relative,
                "role": role,
                "sha256": _sha256(raw),
                "size_bytes": len(raw),
            }
        )
        payload[relative] = raw
    if not any(item["role"] == "hidden_verifier" for item in entries):
        raise MacVerifierError("no hidden verifier file was selected")
    return entries, payload


def _snapshot(root: Path, task: dict[str, Any], task_sha256: str) -> dict[str, object]:
    entries, _ = _collect(root, task)
    material = {
        "task_sha256": task_sha256,
        "files": entries,
    }
    return {
        "schema": SNAPSHOT_SCHEMA,
        "status": "verified",
        "task_sha256": task_sha256,
        "manifest_sha256": _sha256(_canonical(material)),
        "file_count": len(entries),
        "total_bytes": sum(int(item["size_bytes"]) for item in entries),
    }


def _build_archive(
    *,
    task: dict[str, Any],
    task_sha256: str,
    source_snapshot_sha256: str,
    stage: Path,
    timeout_seconds: int,
) -> tuple[bytes, dict[str, object], str]:
    entries, payload = _collect(stage, task)
    stage_material = {"task_sha256": task_sha256, "files": entries}
    stage_manifest_sha256 = _sha256(_canonical(stage_material))
    manifest: dict[str, object] = {
        "schema": TRANSFER_SCHEMA,
        "task_sha256": task_sha256,
        "stage_manifest_sha256": stage_manifest_sha256,
        "source_snapshot_sha256": source_snapshot_sha256,
        "files": entries,
        "command": _command(task["test_command"]),
        "limits": {
            "timeout_seconds": timeout_seconds,
            "stdout_bytes": STDOUT_LIMIT,
            "stderr_bytes": STDERR_LIMIT,
        },
        "expected_mac": EXPECTED_MAC,
    }
    manifest_raw = _canonical(manifest)
    manifest_sha256 = _sha256(manifest_raw)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:

        def write(name: str, raw: bytes) -> None:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            bundle.writestr(info, raw)

        write("manifest.json", manifest_raw)
        write("manifest.sha256", manifest_sha256.encode("ascii"))
        for relative, raw in sorted(payload.items()):
            write("payload/" + relative, raw)
    archive = stream.getvalue()
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise MacVerifierError("compressed transfer exceeds the archive byte bound")
    return archive, manifest, manifest_sha256


def _fingerprint_key_line(line: str) -> str:
    fields = line.split()
    if len(fields) < 2:
        raise MacVerifierError("SSH public key line is malformed")
    try:
        raw = base64.b64decode(fields[1], validate=True)
    except ValueError as exc:
        raise MacVerifierError("SSH public key is not valid base64") from exc
    value = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return "SHA256:" + value


def _ssh_configuration(ssh_executable: str) -> dict[str, object]:
    try:
        result = subprocess.run(
            [ssh_executable, "-G", SSH_ALIAS],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MacVerifierError("trusted SSH configuration cannot be expanded") from exc
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        raise MacVerifierError("trusted SSH configuration expansion failed closed")
    values: dict[str, list[str]] = {}
    for raw_line in result.stdout.decode("utf-8", "strict").splitlines():
        key, _, value = raw_line.partition(" ")
        values.setdefault(key.lower(), []).append(value.strip())
    if values.get("hostname", [None])[0] != SSH_HOST or values.get("user", [None])[0] != SSH_USER:
        raise MacVerifierError("SSH alias no longer resolves to the pinned Mac identity")
    if values.get("identitiesonly", [None])[0] != "yes":
        raise MacVerifierError("SSH alias must use only its dedicated identity")
    if values.get("proxycommand", ["none"])[0] not in {"none", ""}:
        raise MacVerifierError("SSH alias unexpectedly uses a proxy command")
    if values.get("proxyjump", ["none"])[0] not in {"none", ""}:
        raise MacVerifierError("SSH alias unexpectedly uses a proxy jump")
    if values.get("permitlocalcommand", ["no"])[0] != "no":
        raise MacVerifierError("SSH alias unexpectedly permits a local command")
    if values.get("localcommand", ["none"])[0] not in {"none", ""}:
        raise MacVerifierError("SSH alias unexpectedly defines a local command")
    if values.get("remotecommand", ["none"])[0] not in {"none", ""}:
        raise MacVerifierError("SSH alias unexpectedly defines a remote command")
    identity_values = values.get("identityfile", [])
    if len(identity_values) != 1:
        raise MacVerifierError("SSH alias must resolve to one dedicated identity file")
    identity_path = Path(os.path.expandvars(os.path.expanduser(identity_values[0]))).resolve()
    public_path = Path(str(identity_path) + ".pub")
    try:
        public_line = public_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise MacVerifierError("dedicated SSH public key cannot be read") from exc
    if _fingerprint_key_line(public_line) != SSH_CLIENT_KEY_FINGERPRINT:
        raise MacVerifierError("dedicated SSH client identity fingerprint differs")

    known_values = values.get("userknownhostsfile", [])
    known_paths = [
        Path(os.path.expandvars(os.path.expanduser(item))).resolve()
        for value in known_values
        for item in value.split()
    ]
    server_matches = []
    for known_path in known_paths:
        if not known_path.is_file():
            continue
        for line in known_path.read_text(encoding="utf-8", errors="strict").splitlines():
            fields = line.split()
            if len(fields) >= 3 and SSH_HOST in fields[0].split(",") and fields[1] == "ssh-ed25519":
                server_matches.append(_fingerprint_key_line(" ".join(fields[1:3])))
    if server_matches != [SSH_SERVER_KEY_FINGERPRINT]:
        raise MacVerifierError("pinned Mac SSH host key fingerprint differs or is ambiguous")
    return {
        "alias": SSH_ALIAS,
        "host": SSH_HOST,
        "user": SSH_USER,
        "identity_public_fingerprint": SSH_CLIENT_KEY_FINGERPRINT,
        "server_host_key_fingerprint": SSH_SERVER_KEY_FINGERPRINT,
        "identity_count": 1,
    }


def _invoke_ssh(archive: bytes, timeout_seconds: int) -> dict[str, object]:
    ssh_executable = shutil.which("ssh")
    if ssh_executable is None:
        raise MacVerifierError("Windows OpenSSH client is unavailable")
    resolved_ssh = str(Path(ssh_executable).resolve())
    if os.name == "nt":
        expected = (
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
        )
        if os.path.normcase(resolved_ssh) != os.path.normcase(str(expected.resolve())):
            raise MacVerifierError("trusted verifier requires the Windows OpenSSH client")
    configuration = _ssh_configuration(resolved_ssh)
    remote_program = (PACKAGE / "mac" / "swift-verifier-remote.py").read_bytes()
    remote_sha256 = _sha256(remote_program)
    encoded = base64.b64encode(remote_program).decode("ascii")
    digest = remote_sha256.removeprefix("sha256:")
    loader = (
        "import base64,hashlib;"
        f"d=base64.b64decode('{encoded}');"
        f"assert hashlib.sha256(d).hexdigest()=='{digest}';"
        "exec(compile(d,'<coding-intelligence-mac-swift-verifier>','exec'))"
    )
    remote_command = f'/usr/bin/python3 -c "{loader}"'
    command = [
        resolved_ssh,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "HostKeyAlgorithms=ssh-ed25519",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ControlPersist=no",
        "-o",
        "RequestTTY=no",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "LogLevel=ERROR",
        "-T",
        SSH_ALIAS,
        remote_command,
    ]
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(archive, timeout=timeout_seconds + 20)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        raise MacVerifierError(
            "single Mac SSH verification session exceeded its wall bound"
        ) from exc
    if len(stdout) > MAX_SSH_STDOUT or len(stderr) > MAX_SSH_STDERR:
        raise MacVerifierError("single Mac SSH verification session exceeded its output bound")
    if stderr:
        raise MacVerifierError("single Mac SSH verification session returned transport stderr")
    try:
        remote = json.loads(stdout.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MacVerifierError("Mac SSH verifier returned invalid unique-key JSON") from exc
    if not isinstance(remote, dict):
        raise MacVerifierError("Mac SSH verifier result must be one object")
    return {
        "configuration": configuration,
        "remote_program_sha256": remote_sha256,
        "ssh_sessions": 1,
        "connection_attempts": 1,
        "automatic_retries": 0,
        "exit_code": process.returncode,
        "seconds": round(time.monotonic() - started, 3),
        "stdout_bytes": len(stdout),
        "stdout_sha256": _sha256(stdout),
        "stderr_bytes": len(stderr),
        "stderr_sha256": _sha256(stderr),
        "remote": remote,
    }


def _validate_remote(
    transport: dict[str, object],
    *,
    manifest: dict[str, object],
    manifest_sha256: str,
    archive_sha256: str,
) -> dict[str, object]:
    remote = transport["remote"]
    if not isinstance(remote, dict) or remote.get("schema") != REMOTE_SCHEMA:
        raise MacVerifierError("Mac result schema differs")
    if remote.get("automatic_retries") != 0:
        raise MacVerifierError("Mac result reported a retry")
    for field, expected in (
        ("archive_sha256", archive_sha256),
        ("manifest_sha256", manifest_sha256),
        ("stage_manifest_sha256", manifest["stage_manifest_sha256"]),
        ("source_snapshot_sha256", manifest["source_snapshot_sha256"]),
        ("task_sha256", manifest["task_sha256"]),
    ):
        if remote.get(field) != expected:
            raise MacVerifierError(f"Mac result {field} differs")
    identity = remote.get("identity")
    cleanup = remote.get("cleanup")
    test = remote.get("test")
    if (
        not isinstance(cleanup, dict)
        or cleanup.get("succeeded") is not True
        or cleanup.get("post_exists") is not False
    ):
        raise MacVerifierError("Mac verifier did not prove exact temp cleanup")
    if remote.get("status") == "verified":
        if transport["exit_code"] != 0:
            raise MacVerifierError("verified Mac result had a failing SSH process exit")
        if not isinstance(identity, dict) or identity.get("matched") is not True:
            raise MacVerifierError("verified Mac result did not match the pinned identity")
        if not isinstance(test, dict):
            raise MacVerifierError("verified Mac result omitted the Swift test")
        if (
            test.get("exit_code") != 0
            or test.get("timed_out") is not False
            or test.get("output_limit_exceeded") is not False
            or test.get("owned_process_group_residual") is not False
        ):
            raise MacVerifierError("verified Mac result did not pass its bounded Swift test")
        if test.get("argv") != [EXPECTED_MAC["swift_path"], *manifest["command"][1:]]:
            raise MacVerifierError("verified Mac result command differs")
        for channel in ("stdout", "stderr"):
            evidence = test.get(channel)
            if not isinstance(evidence, dict):
                raise MacVerifierError(f"verified Mac result omitted {channel}")
            _digest(evidence.get("sha256"), f"test.{channel}.sha256")
            if not isinstance(evidence.get("bytes"), int) or evidence.get("truncated") is not False:
                raise MacVerifierError(f"verified Mac result {channel} evidence differs")
    elif remote.get("status") != "failed":
        raise MacVerifierError("Mac result status differs")
    return remote


def run_verification(
    *,
    task_path: Path,
    expected_task_sha256: str,
    stage: Path,
    expected_source_snapshot_sha256: str,
    timeout_seconds: int,
) -> dict[str, object]:
    if not 10 <= timeout_seconds <= 1800:
        raise MacVerifierError("Mac verifier timeout must be between 10 and 1800 seconds")
    task, task_sha256 = _load_task(task_path, expected_task_sha256)
    _command(task["test_command"])
    workspace = Path(str(task.get("workspace", ""))).resolve(strict=True)
    source_before = _snapshot(workspace, task, task_sha256)
    expected_source_snapshot_sha256 = _digest(
        expected_source_snapshot_sha256, "expected_source_snapshot_sha256"
    )
    if source_before["manifest_sha256"] != expected_source_snapshot_sha256:
        raise MacVerifierError("source workspace changed after the pre-model checkpoint")
    archive, manifest, manifest_sha256 = _build_archive(
        task=task,
        task_sha256=task_sha256,
        source_snapshot_sha256=expected_source_snapshot_sha256,
        stage=stage,
        timeout_seconds=timeout_seconds,
    )
    intent = {
        "host_alias": SSH_ALIAS,
        "manifest_sha256": manifest_sha256,
        "archive_sha256": _sha256(archive),
        "remote_command_sha256": _sha256(_canonical(manifest["command"])),
        "timeout_seconds": timeout_seconds,
        "automatic_retries": 0,
    }
    intent["intent_sha256"] = _sha256(_canonical(intent))
    transport = _invoke_ssh(archive, timeout_seconds)
    remote = _validate_remote(
        transport,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        archive_sha256=intent["archive_sha256"],
    )
    stage_after = _snapshot(stage, task, task_sha256)
    if stage_after["manifest_sha256"] != manifest["stage_manifest_sha256"]:
        raise MacVerifierError("local staged files mutated during Mac verification")
    source_after = _snapshot(workspace, task, task_sha256)
    if source_after["manifest_sha256"] != expected_source_snapshot_sha256:
        raise MacVerifierError("source workspace mutated during Mac verification")
    status = "verified" if remote["status"] == "verified" else "failed"
    return {
        "schema": RESULT_SCHEMA,
        "status": status,
        "captured_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "checkpoint": {
            "task_sha256": task_sha256,
            "source_snapshot_before_sha256": source_before["manifest_sha256"],
            "source_snapshot_after_sha256": source_after["manifest_sha256"],
            "source_workspace_mutated": False,
            "stage_manifest_sha256": manifest["stage_manifest_sha256"],
            "stage_snapshot_after_sha256": stage_after["manifest_sha256"],
            "transfer_manifest_sha256": manifest_sha256,
            "archive_sha256": intent["archive_sha256"],
            "file_count": len(manifest["files"]),
            "total_bytes": sum(int(item["size_bytes"]) for item in manifest["files"]),
            "hidden_verifier_files": sum(
                1 for item in manifest["files"] if item["role"] == "hidden_verifier"
            ),
        },
        "intent": intent,
        "transport": {key: value for key, value in transport.items() if key != "remote"},
        "result": remote,
        "model_calls": 0,
        "automatic_retries": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--task", required=True, type=Path)
    snapshot.add_argument("--expected-task-sha256", required=True)
    snapshot.add_argument("--root", required=True, type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--task", required=True, type=Path)
    verify.add_argument("--expected-task-sha256", required=True)
    verify.add_argument("--stage", required=True, type=Path)
    verify.add_argument("--expected-source-snapshot-sha256", required=True)
    verify.add_argument("--timeout", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            task, task_sha256 = _load_task(args.task, args.expected_task_sha256)
            _command(task["test_command"])
            result = _snapshot(args.root, task, task_sha256)
        else:
            result = run_verification(
                task_path=args.task,
                expected_task_sha256=args.expected_task_sha256,
                stage=args.stage,
                expected_source_snapshot_sha256=args.expected_source_snapshot_sha256,
                timeout_seconds=args.timeout,
            )
    except (MacVerifierError, OSError) as exc:
        result = {
            "schema": RESULT_SCHEMA,
            "status": "failed",
            "terminal_outcome": "runtime_blocked",
            "error": str(exc)[:2000],
            "model_calls": 0,
            "automatic_retries": 0,
        }
    sys.stdout.write(_canonical(result).decode("ascii") + "\n")
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
