#!/usr/bin/env python3
"""Bounded, fail-closed maintenance for completed Codex subagent sessions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

SCHEMA = "coding-intelligence.codex-catalog-maintenance/v1"
MINIMUM_AGE_SECONDS = 24 * 60 * 60
MAX_ARCHIVES_PER_RUN = 100
MAX_CANDIDATES_SCANNED = 500
MAX_RUN_SECONDS = 600
ARCHIVE_TIMEOUT_SECONDS = 60
TAIL_BYTES = 1024 * 1024
LOG_LIMIT_BYTES = 256 * 1024
LOG_GENERATIONS = 2
LABEL = "com.ggen5.coding-intelligence.codex-catalog-maintenance"


class MaintenanceError(RuntimeError):
    pass


class AlreadyRunningError(MaintenanceError):
    pass


def acquire_instance_lock(path: Path):
    """Return an open, non-blockingly locked file on macOS or Windows."""
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        handle.close()
        raise AlreadyRunningError from exc
    return handle


def subprocess_platform_options() -> dict[str, int]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=MAX_ARCHIVES_PER_RUN)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= MAX_ARCHIVES_PER_RUN:
        parser.error(f"--limit must be between 1 and {MAX_ARCHIVES_PER_RUN}")
    return args


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError(f"state path is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"cannot read state file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MaintenanceError(f"state file is not a JSON object: {path}")
    return value


def rotate_log(path: Path) -> None:
    if not path.exists() or path.stat().st_size < LOG_LIMIT_BYTES:
        return
    previous = path.with_suffix(path.suffix + ".1")
    if previous.exists():
        previous.unlink()
    path.replace(previous)


def log_event(path: Path, message: str) -> None:
    rotate_log(path)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def find_state_db(codex_home: Path) -> Path:
    matches: list[tuple[int, Path]] = []
    for candidate in codex_home.glob("state_*.sqlite"):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            generation = int(candidate.stem.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        matches.append((generation, candidate.resolve()))
    if not matches:
        raise MaintenanceError(f"no Codex state database found under {codex_home}")
    return max(matches)[1]


def find_codex_bin(home: Path, codex_home: Path) -> Path:
    candidates: list[Path] = []
    override = os.environ.get("CODEX_BIN")
    if override:
        candidates.append(Path(override).expanduser())
    if os.name == "nt":
        local_app_data = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        candidates.extend(
            (
                local_app_data / "Programs/OpenAI/Codex/bin/codex.exe",
                codex_home / "packages/standalone/current/bin/codex.exe",
                codex_home / "packages/standalone/current/codex.exe",
            )
        )
    else:
        candidates.extend(
            (
                home / ".local/bin/codex",
                codex_home / "packages/standalone/current/bin/codex",
                codex_home / "packages/standalone/current/codex",
            )
        )
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(Path(discovered))

    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.absolute()
        except OSError:
            continue
    raise MaintenanceError("no executable standalone Codex CLI was found")


def open_read_only(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
    required = {"id", "source", "updated_at", "rollout_path", "archived"}
    if not required.issubset(columns):
        connection.close()
        raise MaintenanceError("Codex threads schema is missing required columns")
    return connection


def counts(connection: sqlite3.Connection) -> dict[str, int]:
    result = {"active": 0, "archived": 0, "active_subagents": 0, "active_vscode": 0}
    for row in connection.execute("SELECT archived, source FROM threads"):
        archived = int(row["archived"]) == 1
        result["archived" if archived else "active"] += 1
        if not archived and exact_subagent_source(row["source"]):
            result["active_subagents"] += 1
        if not archived and row["source"] == "vscode":
            result["active_vscode"] += 1
    return result


def exact_subagent_source(raw_source: str) -> bool:
    try:
        value = json.loads(raw_source)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(value, dict)
        and set(value) == {"subagent"}
        and isinstance(value["subagent"], dict)
    )


def terminal_task_complete(
    rollout: Path, sessions_root: Path, thread_id: str, cutoff: int
) -> tuple[bool, str]:
    try:
        if rollout.is_symlink() or not rollout.is_file():
            return False, "rollout_not_regular"
        resolved = rollout.resolve(strict=True)
        common = os.path.commonpath((str(resolved), str(sessions_root)))
        if os.path.normcase(common) != os.path.normcase(str(sessions_root)):
            return False, "rollout_outside_active_sessions"
        if not resolved.name.endswith(f"-{thread_id}.jsonl"):
            return False, "rollout_identity_mismatch"
        stat = resolved.stat()
        if int(stat.st_mtime) >= cutoff:
            return False, "rollout_recently_modified"
        with resolved.open("rb") as handle:
            handle.seek(max(0, stat.st_size - TAIL_BYTES))
            tail = handle.read(TAIL_BYTES)
        lines = [line for line in tail.splitlines() if line.strip()]
        if not lines:
            return False, "rollout_tail_empty"
        record = json.loads(lines[-1].decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False, "rollout_terminal_record_invalid"
    if record.get("type") != "event_msg":
        return False, "terminal_record_not_event"
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "task_complete":
        return False, "terminal_event_not_task_complete"
    return True, "terminal_task_complete"


def candidate_rows(connection: sqlite3.Connection, cutoff: int) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT id, source, updated_at, rollout_path
            FROM threads
            WHERE archived = 0
              AND updated_at < ?
              AND source LIKE '{"subagent":%'
            ORDER BY updated_at ASC, id ASC
            LIMIT ?
            """,
            (cutoff, MAX_CANDIDATES_SCANNED),
        )
    )


def row_is_still_eligible(
    connection: sqlite3.Connection,
    original: sqlite3.Row,
    sessions_root: Path,
    cutoff: int,
) -> tuple[bool, str]:
    current = connection.execute(
        "SELECT id, source, updated_at, rollout_path, archived FROM threads WHERE id = ?",
        (original["id"],),
    ).fetchone()
    if current is None or int(current["archived"]) != 0:
        return False, "row_missing_or_archived"
    if (
        current["source"] != original["source"]
        or int(current["updated_at"]) != int(original["updated_at"])
        or current["rollout_path"] != original["rollout_path"]
    ):
        return False, "row_changed_since_selection"
    if int(current["updated_at"]) >= cutoff or not exact_subagent_source(current["source"]):
        return False, "source_or_age_changed"
    return terminal_task_complete(
        Path(current["rollout_path"]), sessions_root, current["id"], cutoff
    )


def sanitized_process_result(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    combined = f"{result.stdout}\n{result.stderr}".strip()
    return {
        "exit_code": result.returncode,
        "output_sha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        "output_excerpt": combined[:512],
    }


def archive_one(codex_bin: Path, thread_id: str) -> tuple[dict[str, Any], bool]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            (str(codex_bin), "archive", thread_id),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=ARCHIVE_TIMEOUT_SECONDS,
            check=False,
            **subprocess_platform_options(),
        )
        report = sanitized_process_result(result)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return report, result.returncode == 0
    except subprocess.TimeoutExpired as exc:
        combined = f"{exc.stdout or ''}\n{exc.stderr or ''}".strip()
        return (
            {
                "exit_code": 124,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "output_sha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
                "output_excerpt": combined[:512],
                "timeout": True,
            },
            False,
        )


def verify_archived(db_path: Path, thread_id: str) -> tuple[bool, dict[str, Any]]:
    with closing(open_read_only(db_path)) as connection:
        row = connection.execute(
            "SELECT archived, archived_at, rollout_path FROM threads WHERE id = ?", (thread_id,)
        ).fetchone()
    if row is None:
        return False, {"reason": "thread_missing_after_archive"}
    rollout = Path(row["rollout_path"])
    archived = int(row["archived"]) == 1
    path_ok = (
        not rollout.is_symlink()
        and rollout.is_file()
        and rollout.parent.resolve() == (rollout.parents[1] / "archived_sessions").resolve()
    )
    return archived and path_ok, {
        "archived": archived,
        "archived_at": row["archived_at"],
        "rollout_path": str(rollout),
        "rollout_exists": rollout.is_file(),
    }


def run_maintenance(
    args: argparse.Namespace,
    codex_home: Path,
    codex_bin: Path,
    state_root: Path,
    db_path: Path,
    log_path: Path,
) -> dict[str, Any]:
    started_wall = int(time.time())
    started = time.monotonic()
    cutoff = started_wall - MINIMUM_AGE_SECONDS
    sessions_root = (codex_home / "sessions").resolve()
    attempted_path = state_root / "attempted-ids.json"
    attempted = load_json_object(attempted_path)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "mode": "dry_run" if args.dry_run else "archive",
        "started_at": started_wall,
        "pid": os.getpid(),
        "policy": {
            "source": "exact structured subagent source only",
            "minimum_age_seconds": MINIMUM_AGE_SECONDS,
            "terminal_record": "event_msg.payload.type=task_complete",
            "tail_bytes": TAIL_BYTES,
            "archive_limit": args.limit,
            "candidate_scan_limit": MAX_CANDIDATES_SCANNED,
            "wall_time_limit_seconds": MAX_RUN_SECONDS,
            "per_archive_timeout_seconds": ARCHIVE_TIMEOUT_SECONDS,
            "mutation_command": "codex archive <UUID>",
            "automatic_retries_per_id": 0,
        },
        "selected": [],
        "archived": [],
        "skipped": {},
        "failure": None,
    }
    with closing(open_read_only(db_path)) as connection:
        report["counts_before"] = counts(connection)
        rows = candidate_rows(connection, cutoff)
        for row in rows:
            if len(report["selected"]) >= args.limit:
                break
            thread_id = row["id"]
            if thread_id in attempted:
                reason = "previously_attempted_no_retry"
                report["skipped"][reason] = report["skipped"].get(reason, 0) + 1
                continue
            if not exact_subagent_source(row["source"]):
                reason = "source_not_exact_subagent"
                report["skipped"][reason] = report["skipped"].get(reason, 0) + 1
                continue
            eligible, reason = terminal_task_complete(
                Path(row["rollout_path"]), sessions_root, thread_id, cutoff
            )
            if not eligible:
                report["skipped"][reason] = report["skipped"].get(reason, 0) + 1
                continue
            report["selected"].append(thread_id)

        if args.dry_run:
            report["counts_after"] = report["counts_before"]
            report["completed_at"] = int(time.time())
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            return report

        for thread_id in report["selected"]:
            if time.monotonic() - started >= MAX_RUN_SECONDS:
                report["skipped"]["wall_time_limit"] = 1
                break
            original = next(row for row in rows if row["id"] == thread_id)
            eligible, reason = row_is_still_eligible(connection, original, sessions_root, cutoff)
            if not eligible:
                report["skipped"][reason] = report["skipped"].get(reason, 0) + 1
                continue

            attempt = {
                "status": "attempting",
                "recorded_at": int(time.time()),
                "source": "subagent",
                "updated_at": int(original["updated_at"]),
                "rollout_path": original["rollout_path"],
                "archive_command": [str(codex_bin), "archive", thread_id],
                "unarchive_command": [str(codex_bin), "unarchive", thread_id],
            }
            attempted[thread_id] = attempt
            atomic_json(attempted_path, attempted)

            process_report, command_ok = archive_one(codex_bin, thread_id)
            verified, verification = verify_archived(db_path, thread_id)
            attempt.update(process_report)
            attempt["verification"] = verification
            if verified:
                attempt["status"] = "archived"
                attempt["command_reported_success"] = command_ok
                report["archived"].append(thread_id)
                atomic_json(attempted_path, attempted)
                log_event(
                    log_path,
                    f"archived id={thread_id} seconds={process_report['elapsed_seconds']}",
                )
                continue

            attempt["status"] = "failed_no_automatic_retry"
            attempt["failure_recorded_at"] = int(time.time())
            attempted[thread_id] = attempt
            atomic_json(attempted_path, attempted)
            report["failure"] = {"thread_id": thread_id, **attempt}
            log_event(
                log_path,
                f"failed_no_retry id={thread_id} exit={process_report['exit_code']}",
            )
            break

    with closing(open_read_only(db_path)) as connection:
        report["counts_after"] = counts(connection)
    report["completed_at"] = int(time.time())
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def probe_thread_list(codex_bin: Path) -> dict[str, Any]:
    initialize = {
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "catalog-maintenance-probe", "version": "1.0.0"},
            "capabilities": {},
        },
    }
    initialized = {"method": "initialized"}
    thread_list = {
        "id": 2,
        "method": "thread/list",
        "params": {
            "archived": False,
            "limit": 50,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "useStateDbOnly": True,
        },
    }
    started = time.monotonic()
    process = subprocess.Popen(
        (str(codex_bin), "app-server", "--stdio"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **subprocess_platform_options(),
    )
    observed: list[str] = []
    output_lines: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        if process.stdout is None:
            output_lines.put(None)
            return
        for line in process.stdout:
            output_lines.put(line)
        output_lines.put(None)

    reader = threading.Thread(target=read_output, name="codex-thread-list-probe", daemon=True)
    reader.start()

    def send(message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise MaintenanceError("thread/list probe stdin is unavailable")
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def response(request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                line = output_lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            observed.append(line[:1000])
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                return message
        raise MaintenanceError(f"thread/list probe timed out waiting for id={request_id}")

    try:
        send(initialize)
        initialize_response = response(1)
        if "error" in initialize_response:
            raise MaintenanceError(f"initialize failed: {initialize_response['error']!r}")
        send(initialized)
        send(thread_list)
        message = response(2)
        elapsed = round(time.monotonic() - started, 3)
        if "error" not in message:
            result = message.get("result")
            if isinstance(result, dict) and isinstance(result.get("data"), list):
                return {
                    "status": "ok",
                    "elapsed_seconds": elapsed,
                    "page_count": len(result["data"]),
                    "has_next_cursor": result.get("nextCursor") is not None,
                    "method": "thread/list",
                    "transport": "fresh stdio app-server",
                    "use_state_db_only": True,
                }
        raise MaintenanceError(f"thread/list probe failed response={message!r}")
    except MaintenanceError as exc:
        raise MaintenanceError(f"{exc}; observed={''.join(observed)[:1000]!r}") from exc
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def probe_doctor(codex_bin: Path) -> dict[str, Any]:
    started = time.monotonic()
    process = subprocess.run(
        (str(codex_bin), "doctor", "--json"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
        **subprocess_platform_options(),
    )
    elapsed = round(time.monotonic() - started, 3)
    try:
        report = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise MaintenanceError(f"doctor returned invalid JSON: {exc}") from exc
    return {
        "status": report.get("overallStatus"),
        "process_exit_code": process.returncode,
        "elapsed_seconds": elapsed,
        "codex_version": report.get("codexVersion"),
        "app_server_status": report.get("checks", {}).get("app_server.status", {}).get("status"),
        "auth_status": report.get("checks", {}).get("auth.credentials", {}).get("status"),
        "rollout_db_parity_status": report.get("checks", {})
        .get("state.rollout_db_parity", {})
        .get("status"),
        "state_paths_status": report.get("checks", {}).get("state.paths", {}).get("status"),
    }


def self_check(codex_home: Path, codex_bin: Path, db_path: Path) -> dict[str, Any]:
    if codex_bin.is_symlink() and not codex_bin.resolve().is_file():
        raise MaintenanceError(f"Codex symlink is broken: {codex_bin}")
    if not codex_bin.exists() or not os.access(codex_bin, os.X_OK):
        raise MaintenanceError(f"Codex executable is unavailable: {codex_bin}")
    with closing(open_read_only(db_path)) as connection:
        current_counts = counts(connection)
    version = subprocess.run(
        (str(codex_bin), "--version"),
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        **subprocess_platform_options(),
    ).stdout.strip()
    return {
        "status": "ok",
        "codex_home": str(codex_home),
        "state_db": str(db_path),
        "state_db_access": "mode=ro, query_only=ON",
        "codex": version,
        "codex_path": str(codex_bin),
        "counts": current_counts,
        "policy": "subagent + older_than_24h + terminal_task_complete",
    }


def main() -> int:
    os.umask(0o077)
    args = parse_args()
    home = Path.home().resolve()
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser().resolve()
    codex_bin = find_codex_bin(home, codex_home)
    state_root = codex_home / "coding-intelligence/catalog-maintenance"
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_root.chmod(0o700)
    for name in (
        "attempted-ids.json",
        "last-probe.json",
        "last-run.json",
        "maintenance.lock",
        "maintenance.log",
    ):
        existing = state_root / name
        if existing.is_file() and not existing.is_symlink():
            existing.chmod(0o600)
    log_path = state_root / "maintenance.log"
    lock_path = state_root / "maintenance.lock"
    db_path = find_state_db(codex_home)

    try:
        lock = acquire_instance_lock(lock_path)
    except AlreadyRunningError:
        print(json.dumps({"status": "already_running", "label": LABEL}))
        return 0
    with lock:
        try:
            if args.self_check:
                print(json.dumps(self_check(codex_home, codex_bin, db_path), sort_keys=True))
                return 0
            if args.probe_only:
                with closing(open_read_only(db_path)) as connection:
                    active = counts(connection)
                report = {
                    "schema": SCHEMA,
                    "mode": "probe",
                    "recorded_at": int(time.time()),
                    "counts": active,
                    "thread_list": probe_thread_list(codex_bin),
                    "doctor": probe_doctor(codex_bin),
                }
                atomic_json(state_root / "last-probe.json", report)
                print(json.dumps(report, sort_keys=True))
                return 0

            report = run_maintenance(args, codex_home, codex_bin, state_root, db_path, log_path)
            atomic_json(state_root / "last-run.json", report)
            log_event(
                log_path,
                f"run_complete mode={report['mode']} selected={len(report['selected'])} "
                f"archived={len(report['archived'])} failure={report['failure'] is not None} "
                f"seconds={report['elapsed_seconds']}",
            )
            print(json.dumps(report, sort_keys=True))
            return 1 if report["failure"] else 0
        except (MaintenanceError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            failure = {
                "schema": SCHEMA,
                "status": "failed_closed",
                "recorded_at": int(time.time()),
                "error": str(exc),
            }
            atomic_json(state_root / "last-run.json", failure)
            log_event(log_path, f"failed_closed error={str(exc)[:300]}")
            print(json.dumps(failure, sort_keys=True), file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
