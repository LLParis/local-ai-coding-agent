"""Bounded, pressure-gated autonomous maintenance for sovereign Memory v1.

This module is deliberately separate from the interactive CLI. It performs no
network or model calls and accepts no model-authored policy. A scheduled run
always obtains and records a deterministic retention dry run; physical GC is
possible only when the measured pressure reaches 80%, ``apply`` is explicit,
and the configured item, byte, and wall limits still permit the next action.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from agent_continuity.memory_store import MemoryScope, MemoryStore, MemoryStoreError
from agent_continuity.retention import GC_PRESSURE_BASIS_POINTS, RETENTION_RESULT_SCHEMA

MAINTENANCE_REPORT_SCHEMA = "coding-intelligence-memory-maintenance-report/v1"
DEFAULT_MAX_ITEMS = 25
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_WALL_SECONDS = 30.0
DEFAULT_LOCK_TIMEOUT_SECONDS = 1.0
MAX_ITEMS = 100
MAX_BYTES = 1024 * 1024 * 1024 * 1024
MAX_WALL_SECONDS = 300.0
MAX_HOT_BLOB_BUDGET_BYTES = 100 * 1024 * 1024 * 1024 * 1024
MAX_LOCK_TIMEOUT_SECONDS = 30.0
MAX_EVIDENCE_BYTES = 1024 * 1024
_LOCK_POLL_SECONDS = 0.025
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class MaintenanceBusy(MemoryStoreError):
    """Another exact maintenance owner already holds the run lock."""


@dataclass(frozen=True)
class DiskPressure:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    basis_points: int
    source: str = "volume"

    def __post_init__(self) -> None:
        for name in ("total_bytes", "used_bytes", "free_bytes", "basis_points"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.total_bytes < 1:
            raise ValueError("total_bytes must be positive")
        if self.used_bytes + self.free_bytes > self.total_bytes:
            raise ValueError("disk pressure byte counts are inconsistent")
        if self.basis_points > 10_000:
            raise ValueError("basis_points cannot exceed 10000")
        if self.basis_points != (self.used_bytes * 10_000) // self.total_bytes:
            raise ValueError("basis_points must equal the reported byte ratio")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("pressure source must be non-empty text")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "basis_points": self.basis_points,
        }


@dataclass(frozen=True)
class MaintenanceLimits:
    max_items: int = DEFAULT_MAX_ITEMS
    max_bytes: int = DEFAULT_MAX_BYTES
    max_wall_seconds: float = DEFAULT_MAX_WALL_SECONDS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_items, int)
            or isinstance(self.max_items, bool)
            or not 1 <= self.max_items <= MAX_ITEMS
        ):
            raise ValueError(f"max_items must be between 1 and {MAX_ITEMS}")
        if (
            not isinstance(self.max_bytes, int)
            or isinstance(self.max_bytes, bool)
            or not 1 <= self.max_bytes <= MAX_BYTES
        ):
            raise ValueError(f"max_bytes must be between 1 and {MAX_BYTES}")
        if (
            isinstance(self.max_wall_seconds, bool)
            or not isinstance(self.max_wall_seconds, (int, float))
            or not math.isfinite(self.max_wall_seconds)
            or not 0 < self.max_wall_seconds <= MAX_WALL_SECONDS
        ):
            raise ValueError(f"max_wall_seconds must be finite and at most {MAX_WALL_SECONDS:g}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
            "max_wall_seconds": float(self.max_wall_seconds),
        }


def probe_disk_pressure(root: Path) -> DiskPressure:
    usage = shutil.disk_usage(root)
    basis_points = (int(usage.used) * 10_000) // int(usage.total)
    return DiskPressure(
        total_bytes=int(usage.total),
        used_bytes=int(usage.used),
        free_bytes=int(usage.free),
        basis_points=basis_points,
    )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _contained_path(root: Path, path: Path, label: str) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=path.exists())
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise MemoryStoreError(f"{label} escapes the configured memory root") from error
    return resolved


def _utc_text(value: str | datetime | None) -> str:
    if value is None:
        moment = datetime.now(UTC)
    elif isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("now must be RFC3339 text, datetime, or null")
    if moment.tzinfo is None or moment.utcoffset() != UTC.utcoffset(moment):
        raise ValueError("now must be UTC")
    return (
        moment.astimezone(UTC)
        .isoformat(timespec="microseconds" if moment.microsecond else "seconds")
        .replace("+00:00", "Z")
    )


@contextmanager
def _maintenance_lock(path: Path, timeout: float) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "r+b", buffering=0)
    locked = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MaintenanceBusy("memory maintenance is already running") from error
                time.sleep(min(_LOCK_POLL_SECONDS, remaining))
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _write_evidence(root: Path, report: dict[str, Any]) -> Path:
    unsigned = dict(report)
    unsigned.pop("report_sha256", None)
    report["report_sha256"] = "sha256:" + hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
    encoded = _canonical_bytes(report)
    directory = _contained_path(root, root / "maintenance" / "runs", "evidence root")
    directory.mkdir(parents=True, exist_ok=True)
    directory = _contained_path(root, directory, "evidence root")
    path = _contained_path(root, directory / f"{report['run_id']}.json", "maintenance evidence")
    if path.exists():
        if path.read_bytes() != encoded:
            raise MemoryStoreError(f"conflicting maintenance evidence at {path}")
        return path
    temporary = _contained_path(
        root,
        directory / f".{path.name}.{uuid.uuid4().hex}.tmp",
        "maintenance evidence temporary",
    )
    try:
        with temporary.open("xb", buffering=0) as stream:
            if stream.write(encoded) != len(encoded):
                raise MemoryStoreError("short maintenance evidence write")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return path


_REPORT_FIELDS = {
    "schema",
    "run_id",
    "observed_at",
    "apply_requested",
    "limits",
    "evidence_locator",
    "memory_root",
    "root_resolution",
    "disk_pressure",
    "hot_blob_pressure_basis_points",
    "effective_pressure_basis_points",
    "pressure_threshold_basis_points",
    "plan_sha256",
    "dry_run",
    "exact_scope_keys",
    "execution",
    "error_type",
    "error",
    "status",
    "elapsed_seconds",
    "report_sha256",
}
_SUCCESS_STATUSES = {
    "below_pressure_dry_run",
    "pressure_dry_run",
    "pressure_no_eligible_items",
    "pressure_applied",
    "pressure_bounded_noop",
}


def _validate_dry_run(value: Any, plan_sha256: str) -> dict[str, Any]:
    fields = {
        "schema",
        "status",
        "plan_sha256",
        "actions_planned",
        "actions_applied",
        "bytes_released",
        "events",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise MemoryStoreError("maintenance dry-run evidence fields do not match v1")
    if (
        value["schema"] != RETENTION_RESULT_SCHEMA
        or value["status"] != "dry_run"
        or value["plan_sha256"] != plan_sha256
        or value["actions_applied"] != 0
        or value["bytes_released"] != 0
        or value["events"] != []
    ):
        raise MemoryStoreError("maintenance dry-run evidence is inconsistent")
    if (
        not isinstance(value["actions_planned"], int)
        or isinstance(value["actions_planned"], bool)
        or value["actions_planned"] < 0
    ):
        raise MemoryStoreError("maintenance dry-run action count is invalid")
    return value


def _validate_execution(
    value: Any,
    *,
    plan_sha256: str,
    dry_actions: int,
    limits: MaintenanceLimits,
) -> dict[str, Any]:
    fields = {
        "schema",
        "status",
        "plan_sha256",
        "actions_planned",
        "actions_eligible",
        "actions_applied",
        "bytes_released",
        "stop_reason",
        "events",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise MemoryStoreError("maintenance execution evidence fields do not match v1")
    if (
        value["schema"] != RETENTION_RESULT_SCHEMA
        or value["status"] != "applied"
        or value["plan_sha256"] != plan_sha256
    ):
        raise MemoryStoreError("maintenance execution identity is inconsistent")
    for field in (
        "actions_planned",
        "actions_eligible",
        "actions_applied",
        "bytes_released",
    ):
        item = value[field]
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise MemoryStoreError(f"maintenance execution {field} is invalid")
    events = value["events"]
    if not isinstance(events, list) or len(events) != value["actions_applied"]:
        raise MemoryStoreError("maintenance execution event count is inconsistent")
    if (
        value["actions_planned"] != value["actions_applied"]
        or value["actions_eligible"] != dry_actions
        or value["actions_applied"] > limits.max_items
        or value["bytes_released"] > limits.max_bytes
    ):
        raise MemoryStoreError("maintenance execution exceeds or contradicts its limits")
    if value["stop_reason"] not in {
        "all_eligible_actions_selected",
        "max_actions",
        "max_bytes",
        "wall_deadline",
    }:
        raise MemoryStoreError("maintenance execution stop reason is invalid")
    for event in events:
        if not isinstance(event, dict) or set(event) != {
            "event_id",
            "type",
            "blob_sha256",
        }:
            raise MemoryStoreError("maintenance execution event fields do not match v1")
        try:
            parsed = uuid.UUID(str(event["event_id"]))
        except ValueError as error:
            raise MemoryStoreError("maintenance execution event id is invalid") from error
        if (
            str(parsed) != event["event_id"]
            or event["type"]
            not in {
                "blob/archived",
                "blob/evicted",
            }
            or not _SHA256_RE.fullmatch(str(event["blob_sha256"]))
        ):
            raise MemoryStoreError("maintenance execution event identity is invalid")
    return value


def load_maintenance_evidence(
    path: str | os.PathLike[str],
    *,
    expected_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Load and independently validate one bounded canonical maintenance report."""

    configured_path = Path(os.path.abspath(os.fspath(path)))
    resolved_path = configured_path.resolve(strict=True)
    if os.path.normcase(str(configured_path)) != os.path.normcase(str(resolved_path)):
        raise MemoryStoreError("maintenance evidence path uses a symlink or junction")
    metadata = os.lstat(resolved_path)
    if metadata.st_nlink != 1 or not resolved_path.is_file():
        raise MemoryStoreError("maintenance evidence must be one regular owned file")
    with resolved_path.open("rb") as stream:
        raw = stream.read(MAX_EVIDENCE_BYTES + 1)
    if not raw or len(raw) > MAX_EVIDENCE_BYTES:
        raise MemoryStoreError("maintenance evidence exceeds its bounded size")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MemoryStoreError("maintenance evidence is malformed JSON") from error
    if not isinstance(value, dict) or set(value) != _REPORT_FIELDS:
        raise MemoryStoreError("maintenance evidence fields do not match v1")
    if _canonical_bytes(value) != raw:
        raise MemoryStoreError("maintenance evidence is not canonical JSON")
    if value["schema"] != MAINTENANCE_REPORT_SCHEMA:
        raise MemoryStoreError("maintenance evidence schema is invalid")
    try:
        run_uuid = uuid.UUID(str(value["run_id"]))
    except ValueError as error:
        raise MemoryStoreError("maintenance evidence run_id is invalid") from error
    if str(run_uuid) != value["run_id"]:
        raise MemoryStoreError("maintenance evidence run_id is not canonical")
    root = Path(os.path.abspath(str(value["memory_root"])))
    resolved_root = root.resolve(strict=True)
    if os.path.normcase(str(root)) != os.path.normcase(str(resolved_root)):
        raise MemoryStoreError("maintenance evidence memory root uses a symlink or junction")
    if expected_root is not None:
        expected = Path(os.path.abspath(os.fspath(expected_root))).resolve(strict=True)
        if os.path.normcase(str(expected)) != os.path.normcase(str(resolved_root)):
            raise MemoryStoreError("maintenance evidence belongs to a different memory root")
    expected_locator = f"maintenance/runs/{run_uuid}.json"
    if value["evidence_locator"] != expected_locator:
        raise MemoryStoreError("maintenance evidence locator does not match run_id")
    expected_path = (resolved_root / Path(expected_locator)).resolve(strict=True)
    if os.path.normcase(str(expected_path)) != os.path.normcase(str(resolved_path)):
        raise MemoryStoreError("maintenance evidence path does not match its root and locator")
    if value["root_resolution"] != "lexical_equals_resolved":
        raise MemoryStoreError("maintenance evidence root-resolution contract is invalid")
    supplied_digest = value["report_sha256"]
    if not isinstance(supplied_digest, str) or not _SHA256_RE.fullmatch(supplied_digest):
        raise MemoryStoreError("maintenance evidence digest is invalid")
    unsigned = dict(value)
    unsigned.pop("report_sha256")
    expected_digest = "sha256:" + hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
    if supplied_digest != expected_digest:
        raise MemoryStoreError("maintenance evidence digest mismatch")
    if type(value["apply_requested"]) is not bool:
        raise MemoryStoreError("maintenance evidence apply_requested is not boolean")
    limits_value = value["limits"]
    if not isinstance(limits_value, dict) or set(limits_value) != {
        "max_items",
        "max_bytes",
        "max_wall_seconds",
    }:
        raise MemoryStoreError("maintenance evidence limits fields do not match v1")
    limits = MaintenanceLimits(**limits_value)
    if limits.as_dict() != limits_value:
        raise MemoryStoreError("maintenance evidence limits are not canonical")
    if value["pressure_threshold_basis_points"] != GC_PRESSURE_BASIS_POINTS:
        raise MemoryStoreError("maintenance pressure threshold is not canonical")
    elapsed = value["elapsed_seconds"]
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise MemoryStoreError("maintenance elapsed time is invalid")
    if _utc_text(value["observed_at"]) != value["observed_at"]:
        raise MemoryStoreError("maintenance observed_at is not canonical UTC")
    scope_keys = value["exact_scope_keys"]
    if not isinstance(scope_keys, list) or scope_keys != sorted(set(scope_keys)):
        raise MemoryStoreError("maintenance exact scope keys are invalid")
    for key in scope_keys:
        if not isinstance(key, str):
            raise MemoryStoreError("maintenance exact scope key is not text")
        try:
            parts = json.loads(key)
        except json.JSONDecodeError as error:
            raise MemoryStoreError("maintenance exact scope key is malformed") from error
        if not isinstance(parts, list) or len(parts) != 5:
            raise MemoryStoreError("maintenance exact scope key does not match v1")
    status = value["status"]
    if status == "failed":
        if not isinstance(value["error_type"], str) or not value["error_type"].strip():
            raise MemoryStoreError("failed maintenance evidence lacks error_type")
        if not isinstance(value["error"], str) or not value["error"].strip():
            raise MemoryStoreError("failed maintenance evidence lacks error text")
        return value
    if status not in _SUCCESS_STATUSES:
        raise MemoryStoreError("maintenance evidence status is invalid")
    if value["error_type"] is not None or value["error"] is not None:
        raise MemoryStoreError("successful maintenance evidence cannot contain an error")
    disk_value = value["disk_pressure"]
    if not isinstance(disk_value, dict) or set(disk_value) != {
        "source",
        "total_bytes",
        "used_bytes",
        "free_bytes",
        "basis_points",
    }:
        raise MemoryStoreError("maintenance disk pressure fields do not match v1")
    disk = DiskPressure(**disk_value)
    hot = value["hot_blob_pressure_basis_points"]
    effective = value["effective_pressure_basis_points"]
    if (
        not isinstance(hot, int)
        or isinstance(hot, bool)
        or hot < 0
        or not isinstance(effective, int)
        or isinstance(effective, bool)
        or effective != max(disk.basis_points, hot)
    ):
        raise MemoryStoreError("maintenance effective pressure is inconsistent")
    plan_sha256 = value["plan_sha256"]
    if not isinstance(plan_sha256, str) or not _SHA256_RE.fullmatch(plan_sha256):
        raise MemoryStoreError("maintenance plan digest is invalid")
    dry_run = _validate_dry_run(value["dry_run"], plan_sha256)
    execution = value["execution"]
    if execution is not None:
        execution = _validate_execution(
            execution,
            plan_sha256=plan_sha256,
            dry_actions=dry_run["actions_planned"],
            limits=limits,
        )
    if status == "below_pressure_dry_run":
        consistent = effective < GC_PRESSURE_BASIS_POINTS and execution is None
    elif status == "pressure_dry_run":
        consistent = (
            effective >= GC_PRESSURE_BASIS_POINTS
            and value["apply_requested"] is False
            and execution is None
        )
    elif status == "pressure_no_eligible_items":
        consistent = (
            effective >= GC_PRESSURE_BASIS_POINTS
            and value["apply_requested"] is True
            and dry_run["actions_planned"] == 0
            and execution is None
        )
    elif status == "pressure_applied":
        consistent = (
            effective >= GC_PRESSURE_BASIS_POINTS
            and value["apply_requested"] is True
            and execution is not None
            and execution["actions_applied"] > 0
        )
    else:
        consistent = (
            effective >= GC_PRESSURE_BASIS_POINTS
            and value["apply_requested"] is True
            and execution is not None
            and execution["actions_applied"] == 0
        )
    if not consistent:
        raise MemoryStoreError("maintenance status contradicts its recorded execution")
    return value


def run_maintenance(
    root: str | os.PathLike[str],
    *,
    apply: bool,
    now: str | datetime | None = None,
    scopes: Sequence[MemoryScope] | None = None,
    limits: MaintenanceLimits = MaintenanceLimits(),
    pressure_probe: Callable[[Path], DiskPressure] = probe_disk_pressure,
    monotonic: Callable[[], float] = time.monotonic,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    hot_blob_budget_bytes: int | None = None,
) -> dict[str, Any]:
    """Plan every run and apply bounded GC only at or above 80% pressure."""

    if type(apply) is not bool:
        raise ValueError("apply must be an exact boolean")
    if type(limits) is not MaintenanceLimits:
        raise ValueError("limits must be an exact MaintenanceLimits value")
    if not callable(pressure_probe) or not callable(monotonic):
        raise ValueError("pressure_probe and monotonic must be callable")
    if (
        isinstance(lock_timeout, bool)
        or not isinstance(lock_timeout, (int, float))
        or not math.isfinite(lock_timeout)
        or not 0 < lock_timeout <= MAX_LOCK_TIMEOUT_SECONDS
    ):
        raise ValueError(f"lock_timeout must be finite and at most {MAX_LOCK_TIMEOUT_SECONDS:g}")
    if hot_blob_budget_bytes is not None and (
        not isinstance(hot_blob_budget_bytes, int)
        or isinstance(hot_blob_budget_bytes, bool)
        or not 1 <= hot_blob_budget_bytes <= MAX_HOT_BLOB_BUDGET_BYTES
    ):
        raise ValueError(
            "hot_blob_budget_bytes must be a positive integer no greater than "
            f"{MAX_HOT_BLOB_BUDGET_BYTES}"
        )
    configured_root = Path(os.path.abspath(os.fspath(root)))
    root_path = configured_root.resolve()
    if os.path.normcase(str(configured_root)) != os.path.normcase(str(root_path)):
        raise ValueError("memory maintenance root cannot be a symlink or junction path")
    if root_path == Path(root_path.anchor):
        raise ValueError("memory maintenance root cannot be a filesystem root")
    root_path.mkdir(parents=True, exist_ok=True)
    observed_at = _utc_text(now)
    run_id = str(uuid.uuid4())
    started = monotonic()
    report: dict[str, Any] = {
        "schema": MAINTENANCE_REPORT_SCHEMA,
        "run_id": run_id,
        "observed_at": observed_at,
        "apply_requested": bool(apply),
        "limits": limits.as_dict(),
        "evidence_locator": f"maintenance/runs/{run_id}.json",
        "memory_root": str(root_path),
        "root_resolution": "lexical_equals_resolved",
        "disk_pressure": None,
        "hot_blob_pressure_basis_points": None,
        "effective_pressure_basis_points": None,
        "pressure_threshold_basis_points": GC_PRESSURE_BASIS_POINTS,
        "plan_sha256": None,
        "dry_run": None,
        "exact_scope_keys": [],
        "execution": None,
        "error_type": None,
        "error": None,
    }
    lock_path = _contained_path(
        root_path,
        root_path / "maintenance" / ".run.lock",
        "maintenance run lock",
    )
    with _maintenance_lock(lock_path, float(lock_timeout)):
        try:
            store_kwargs: dict[str, Any] = {}
            if hot_blob_budget_bytes is not None:
                store_kwargs["hot_blob_budget_bytes"] = hot_blob_budget_bytes
            store = MemoryStore(root_path, **store_kwargs)
            plan = store.plan_retention(now=observed_at, scopes=scopes)
            dry_run = store.run_gc(plan)
            disk = pressure_probe(root_path)
            effective_pressure = max(disk.basis_points, plan.pressure_basis_points)
            report.update(
                {
                    "disk_pressure": disk.as_dict(),
                    "hot_blob_pressure_basis_points": plan.pressure_basis_points,
                    "effective_pressure_basis_points": effective_pressure,
                    "plan_sha256": plan.plan_sha256,
                    "dry_run": dry_run,
                    "exact_scope_keys": list(plan.exact_scope_keys),
                }
            )
            if effective_pressure < GC_PRESSURE_BASIS_POINTS:
                report["status"] = "below_pressure_dry_run"
                report["execution"] = None
            elif not apply:
                report["status"] = "pressure_dry_run"
                report["execution"] = None
            elif not plan.actionable:
                report["status"] = "pressure_no_eligible_items"
                report["execution"] = None
            else:
                deadline = started + float(limits.max_wall_seconds)
                execution = store.run_gc(
                    plan,
                    apply=True,
                    max_actions=limits.max_items,
                    max_bytes=limits.max_bytes,
                    deadline_monotonic=deadline,
                    monotonic=monotonic,
                )
                report["status"] = (
                    "pressure_applied"
                    if execution["actions_applied"] > 0
                    else "pressure_bounded_noop"
                )
                report["execution"] = execution
            report["elapsed_seconds"] = max(0.0, monotonic() - started)
        except Exception as error:
            report.update(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "elapsed_seconds": max(0.0, monotonic() - started),
                }
            )
        evidence_path = _write_evidence(root_path, report)
        report["evidence_path"] = str(evidence_path)
        return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-wall-seconds", type=float, default=DEFAULT_MAX_WALL_SECONDS)
    parser.add_argument("--hot-blob-budget-bytes", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_maintenance(
            args.root,
            apply=args.apply,
            limits=MaintenanceLimits(
                max_items=args.max_items,
                max_bytes=args.max_bytes,
                max_wall_seconds=args.max_wall_seconds,
            ),
            hot_blob_budget_bytes=args.hot_blob_budget_bytes,
        )
    except Exception as error:
        print(
            json.dumps(
                {"status": "failed", "error_type": type(error).__name__, "error": str(error)},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
