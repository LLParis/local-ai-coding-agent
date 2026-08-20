#!/usr/bin/env python3
"""Pinned, single-use macOS Swift verifier executed through one SSH session."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

SCHEMA = "coding-intelligence.mac-swift-remote-result/v1"
MANIFEST_SCHEMA = "coding-intelligence.mac-swift-transfer/v1"
APPROVED_ROOT = Path("/private/tmp/coding-intelligence-swift-verifier")
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_FILES = 4096


class VerificationError(RuntimeError):
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
            raise VerificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationError("transfer manifest is not unique-key UTF-8 JSON") from exc


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise VerificationError("manifest path must be a bounded string")
    if "\\" in value or "\x00" in value or "\n" in value or "\r" in value:
        raise VerificationError("manifest path contains a forbidden character")
    path = PurePosixPath(value)
    if path.is_absolute() or value in {".", ".."} or ".." in path.parts:
        raise VerificationError("manifest path escapes the transferred stage")
    if any(part in {"", "."} for part in path.parts):
        raise VerificationError("manifest path is not canonical")
    return path.as_posix()


def _bounded_probe(argv: list[str]) -> tuple[int, bytes, bytes]:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationError(f"identity probe failed: {argv[0]}") from exc
    if len(result.stdout) > 16_384 or len(result.stderr) > 16_384:
        raise VerificationError("identity probe output exceeded its bound")
    return result.returncode, result.stdout, result.stderr


def _probe_text(argv: list[str]) -> str:
    exit_code, stdout, stderr = _bounded_probe(argv)
    if exit_code != 0 or stderr:
        raise VerificationError(f"identity probe failed closed: {argv[0]}")
    try:
        return stdout.decode("utf-8").strip()
    except UnicodeError as exc:
        raise VerificationError("identity probe returned non-UTF-8 output") from exc


def _identity(expected: dict[str, object]) -> tuple[dict[str, object], str]:
    required = {
        "hostname",
        "user",
        "uid",
        "architecture",
        "os_version",
        "developer_dir",
        "swift_path",
        "swift_stdout_sha256",
        "swift_stderr_sha256",
    }
    if set(expected) != required:
        raise VerificationError("expected Mac identity fields do not match the contract")

    developer_dir = _probe_text(["/usr/bin/xcode-select", "-p"])
    swift_path = _probe_text(["/usr/bin/xcrun", "--find", "swift"])
    version_exit, version_stdout, version_stderr = _bounded_probe(
        ["/usr/bin/xcrun", "swift", "--version"]
    )
    observed: dict[str, object] = {
        "hostname": socket.gethostname(),
        "user": _probe_text(["/usr/bin/id", "-un"]),
        "uid": os.getuid(),
        "architecture": platform.machine(),
        "os_version": _probe_text(["/usr/bin/sw_vers", "-productVersion"]),
        "developer_dir": developer_dir,
        "swift_path": swift_path,
        "swift_version_exit": version_exit,
        "swift_stdout_sha256": _sha256(version_stdout),
        "swift_stderr_sha256": _sha256(version_stderr),
        "swift_stdout": version_stdout.decode("utf-8", "replace"),
        "swift_stderr": version_stderr.decode("utf-8", "replace"),
    }
    comparisons = {key: observed[key] == expected[key] for key in required}
    matched = version_exit == 0 and all(comparisons.values())
    observed["comparisons"] = comparisons
    observed["matched"] = matched
    if not matched:
        raise VerificationError("Mac identity or pinned Xcode/Swift toolchain differs")
    return observed, swift_path


def _prepare_root() -> Path:
    APPROVED_ROOT.parent.mkdir(parents=True, exist_ok=True)
    if APPROVED_ROOT.exists() or APPROVED_ROOT.is_symlink():
        info = APPROVED_ROOT.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise VerificationError("approved Mac temp root is not a real directory")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise VerificationError("approved Mac temp root ownership or mode differs")
    else:
        APPROVED_ROOT.mkdir(mode=0o700)
    if APPROVED_ROOT.resolve(strict=True) != APPROVED_ROOT:
        raise VerificationError("approved Mac temp root does not resolve exactly")
    return APPROVED_ROOT


def _safe_cleanup(path: Path | None, identity: tuple[int, int] | None) -> dict[str, object]:
    report: dict[str, object] = {
        "attempted": path is not None,
        "owned_temp": str(path) if path is not None else None,
        "succeeded": path is None,
        "post_exists": False,
        "error": None,
    }
    if path is None:
        return report
    try:
        parent = path.parent.resolve(strict=True)
        if parent != APPROVED_ROOT:
            raise VerificationError("cleanup target left the approved Mac temp root")
        if path.exists() or path.is_symlink():
            current = path.lstat()
            if identity is not None and (current.st_dev, current.st_ino) != identity:
                if stat.S_ISLNK(current.st_mode):
                    path.unlink()
                else:
                    raise VerificationError("owned Mac temp identity changed before cleanup")
            elif stat.S_ISLNK(current.st_mode):
                path.unlink()
            else:
                shutil.rmtree(path)
        report["post_exists"] = path.exists() or path.is_symlink()
        report["succeeded"] = not report["post_exists"]
    except Exception as exc:  # cleanup must be returned, never hidden
        report["post_exists"] = path.exists() or path.is_symlink()
        report["succeeded"] = False
        report["error"] = str(exc)[:2000]
    return report


def _extract(archive: bytes, destination: Path) -> tuple[dict[str, object], str]:
    try:
        bundle = zipfile.ZipFile(io.BytesIO(archive), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise VerificationError("transfer is not a valid ZIP archive") from exc
    with bundle:
        infos = bundle.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise VerificationError("transfer contains duplicate ZIP members")
        if len(names) > MAX_FILES + 2:
            raise VerificationError("transfer file count exceeds the bound")
        if "manifest.json" not in names or "manifest.sha256" not in names:
            raise VerificationError("transfer is missing its manifest binding")
        manifest_raw = bundle.read("manifest.json")
        if len(manifest_raw) > 4 * 1024 * 1024:
            raise VerificationError("transfer manifest exceeds its byte bound")
        manifest_digest = bundle.read("manifest.sha256").decode("ascii", "strict")
        if manifest_digest != _sha256(manifest_raw):
            raise VerificationError("transfer manifest hash differs")
        manifest_value = _load_json(manifest_raw)
        if not isinstance(manifest_value, dict):
            raise VerificationError("transfer manifest must be one object")
        manifest = manifest_value
        required = {
            "schema",
            "task_sha256",
            "stage_manifest_sha256",
            "source_snapshot_sha256",
            "files",
            "command",
            "limits",
            "expected_mac",
        }
        if set(manifest) != required or manifest.get("schema") != MANIFEST_SCHEMA:
            raise VerificationError("transfer manifest shape differs")
        files = manifest.get("files")
        if not isinstance(files, list) or not files or len(files) > MAX_FILES:
            raise VerificationError("transfer manifest file list is invalid")
        expected_members = {"manifest.json", "manifest.sha256"}
        seen_paths: set[str] = set()
        declared_total = 0
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "role", "sha256", "size_bytes"}:
                raise VerificationError("transfer file entry shape differs")
            relative = _relative(item["path"])
            if relative in seen_paths:
                raise VerificationError("transfer manifest repeats a path")
            seen_paths.add(relative)
            if item["role"] not in {"stage", "hidden_verifier"}:
                raise VerificationError("transfer file role differs")
            if not isinstance(item["size_bytes"], int) or item["size_bytes"] < 0:
                raise VerificationError("transfer file size is invalid")
            declared_total += item["size_bytes"]
            if item["size_bytes"] > 4 * 1024 * 1024 or declared_total > 48 * 1024 * 1024:
                raise VerificationError("transfer file sizes exceed the byte contract")
            expected_members.add("payload/" + relative)
        if set(names) != expected_members:
            raise VerificationError("ZIP members differ from the explicit manifest")
        if not any(item["role"] == "hidden_verifier" for item in files):
            raise VerificationError("transfer contains no hidden verifier file")
        stage_material = {"task_sha256": manifest["task_sha256"], "files": files}
        if manifest["stage_manifest_sha256"] != _sha256(_canonical(stage_material)):
            raise VerificationError("stage manifest hash does not bind the transferred files")

        payload = destination / "payload"
        payload.mkdir(mode=0o700)
        for item in files:
            relative = _relative(item["path"])
            member = bundle.getinfo("payload/" + relative)
            if member.file_size != item["size_bytes"]:
                raise VerificationError(f"ZIP metadata size differs: {relative}")
            raw = bundle.read("payload/" + relative)
            if len(raw) != item["size_bytes"] or _sha256(raw) != item["sha256"]:
                raise VerificationError(f"transferred file hash differs: {relative}")
            target = payload.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if (
                target.parent.resolve(strict=True) == payload
                or payload in target.parent.resolve(strict=True).parents
            ):
                pass
            else:
                raise VerificationError("transferred file parent escaped the payload")
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        return manifest, manifest_digest


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _terminate_group(process: subprocess.Popen[bytes], lock: threading.Lock) -> None:
    with lock:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        time.sleep(0.25)
        if _group_exists(process.pid):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    stdout_limit: int,
    stderr_limit: int,
) -> dict[str, object]:
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    lock = threading.Lock()
    overflow = threading.Event()
    data: dict[str, dict[str, object]] = {}

    def reader(name: str, stream: object, limit: int) -> None:
        digest = hashlib.sha256()
        retained = bytearray()
        total = 0
        while True:
            chunk = stream.read(65_536)  # type: ignore[attr-defined]
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            remaining = max(0, limit - len(retained))
            if remaining:
                retained.extend(chunk[:remaining])
            if total > limit:
                overflow.set()
                _terminate_group(process, lock)
        data[name] = {
            "bytes": total,
            "sha256": "sha256:" + digest.hexdigest(),
            "text": bytes(retained).decode("utf-8", "replace"),
            "truncated": total > limit,
        }

    threads = [
        threading.Thread(target=reader, args=("stdout", process.stdout, stdout_limit)),
        threading.Thread(target=reader, args=("stderr", process.stderr, stderr_limit)),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(process, lock)
        process.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)
    owned_process_group_residual = any(thread.is_alive() for thread in threads)
    if any(thread.is_alive() for thread in threads):
        _terminate_group(process, lock)
        for thread in threads:
            thread.join(timeout=2)
    if _group_exists(process.pid):
        owned_process_group_residual = True
        _terminate_group(process, lock)
    if any(thread.is_alive() for thread in threads) or _group_exists(process.pid):
        raise VerificationError("Swift owned process group did not terminate")
    return {
        "argv": argv,
        "exit_code": process.returncode,
        "seconds": round(time.monotonic() - started, 3),
        "timed_out": timed_out,
        "output_limit_exceeded": overflow.is_set(),
        "owned_process_group_residual": owned_process_group_residual,
        "stdout": data["stdout"],
        "stderr": data["stderr"],
    }


def main() -> int:
    report: dict[str, object] = {
        "schema": SCHEMA,
        "status": "failed",
        "automatic_retries": 0,
        "remote_temp": None,
        "archive_sha256": None,
        "manifest_sha256": None,
        "stage_manifest_sha256": None,
        "source_snapshot_sha256": None,
        "task_sha256": None,
        "identity": None,
        "test": None,
        "cleanup": None,
        "error": None,
    }
    owned_temp: Path | None = None
    owned_identity: tuple[int, int] | None = None
    try:
        archive = sys.stdin.buffer.read(MAX_ARCHIVE_BYTES + 1)
        if len(archive) > MAX_ARCHIVE_BYTES:
            raise VerificationError("transfer archive exceeds the byte bound")
        report["archive_sha256"] = _sha256(archive)
        root = _prepare_root()
        owned_temp = Path(tempfile.mkdtemp(prefix="run.", dir=root))
        os.chmod(owned_temp, 0o700)
        info = owned_temp.lstat()
        owned_identity = (info.st_dev, info.st_ino)
        report["remote_temp"] = str(owned_temp)

        manifest, manifest_digest = _extract(archive, owned_temp)
        report["manifest_sha256"] = manifest_digest
        report["stage_manifest_sha256"] = manifest["stage_manifest_sha256"]
        report["source_snapshot_sha256"] = manifest["source_snapshot_sha256"]
        report["task_sha256"] = manifest["task_sha256"]
        expected_mac = manifest["expected_mac"]
        if not isinstance(expected_mac, dict):
            raise VerificationError("expected Mac identity must be one object")
        identity, swift_path = _identity(expected_mac)
        report["identity"] = identity

        command = manifest["command"]
        if (
            not isinstance(command, list)
            or len(command) < 2
            or command[0:2] != ["swift", "test"]
            or any(not isinstance(arg, str) or not arg or len(arg) > 4096 for arg in command)
        ):
            raise VerificationError("remote command is not an argv-safe swift test")
        limits = manifest["limits"]
        if not isinstance(limits, dict) or set(limits) != {
            "timeout_seconds",
            "stdout_bytes",
            "stderr_bytes",
        }:
            raise VerificationError("remote limits shape differs")
        timeout_seconds = limits["timeout_seconds"]
        stdout_limit = limits["stdout_bytes"]
        stderr_limit = limits["stderr_bytes"]
        if (
            not isinstance(timeout_seconds, int)
            or not 10 <= timeout_seconds <= 1800
            or not isinstance(stdout_limit, int)
            or not 4096 <= stdout_limit <= 1_048_576
            or not isinstance(stderr_limit, int)
            or not 4096 <= stderr_limit <= 1_048_576
        ):
            raise VerificationError("remote limits are outside the contract")

        home = owned_temp / "home"
        scratch = owned_temp / "tmp"
        home.mkdir(mode=0o700)
        scratch.mkdir(mode=0o700)
        environment = {
            "DEVELOPER_DIR": str(expected_mac["developer_dir"]),
            "HOME": str(home),
            "TMPDIR": str(scratch) + "/",
            "PATH": str(Path(swift_path).parent) + ":/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
        }
        test = _run_bounded(
            [swift_path, *command[1:]],
            cwd=owned_temp / "payload",
            environment=environment,
            timeout_seconds=timeout_seconds,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
        )
        report["test"] = test
        if (
            test["exit_code"] == 0
            and not test["timed_out"]
            and not test["output_limit_exceeded"]
            and not test["owned_process_group_residual"]
        ):
            report["status"] = "verified"
        else:
            report["error"] = "Swift authoritative test did not pass within bounds"
    except Exception as exc:
        report["error"] = str(exc)[:2000]
    finally:
        report["cleanup"] = _safe_cleanup(owned_temp, owned_identity)
        cleanup = report["cleanup"]
        if not isinstance(cleanup, dict) or not cleanup.get("succeeded"):
            report["status"] = "failed"
            if report["error"] is None:
                report["error"] = "owned Mac verifier temp cleanup failed"

    sys.stdout.write(_canonical(report).decode("ascii") + "\n")
    sys.stdout.flush()
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
