"""Deterministic task-state and loss-checked compaction validation.

The model may propose a compacted state, but this module is the admission
boundary.  It performs no inference and never mutates the event log.  Accepted
results are deliberately marked as awaiting an independent semantic verifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

TASK_STATE_SCHEMA = "coding-intelligence-task-state/v1"
VALIDATION_SCHEMA = "coding-intelligence-compaction-validation/v1"
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 4_096
MAX_TEXT = 16_384

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")

_TOP_LEVEL_KEYS = {
    "schema",
    "task_id",
    "objective",
    "success_criteria",
    "constraints",
    "decisions",
    "completed",
    "current_action",
    "next_actions",
    "blockers",
    "open_tool_calls",
    "artifacts",
    "disputes",
    "through_event_id",
    "previous_state_sha256",
}


class TaskStateError(ValueError):
    """A task-state value does not conform to the v1 wire contract."""


class CitationResolver(Protocol):
    """Read-only authority used by deterministic compaction admission.

    Implementations may resolve against JSONL indexes, SQLite projections, or
    frozen test data.  A missing value is represented by ``None``/``False``.
    """

    def event_record_sha256(self, event_id: str) -> str | None: ...

    def evidence_exists(self, evidence_id: str) -> bool: ...

    def artifact_sha256(self, path: str) -> str | None: ...


@dataclass(frozen=True)
class FrozenSourceRange:
    """Hash-pinned source range summarized by a compaction proposal."""

    first_event_id: str
    first_record_sha256: str
    last_event_id: str
    last_record_sha256: str
    retained_tail_event_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "first_event_id": self.first_event_id,
            "first_record_sha256": self.first_record_sha256,
            "last_event_id": self.last_event_id,
            "last_record_sha256": self.last_record_sha256,
            "retained_tail_event_id": self.retained_tail_event_id,
        }


@dataclass(frozen=True, order=True)
class ValidationIssue:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class CompactionValidationResult:
    """Machine-readable deterministic result and semantic-verifier handoff."""

    accepted: bool
    previous_state_sha256: str | None
    candidate_state_sha256: str | None
    source_range: FrozenSourceRange
    issues: tuple[ValidationIssue, ...]
    preserved: Mapping[str, tuple[str, ...]]

    @property
    def verifier_ready(self) -> bool:
        return self.accepted

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": VALIDATION_SCHEMA,
            "accepted": self.accepted,
            "deterministic_checks": "passed" if self.accepted else "rejected",
            "previous_state_sha256": self.previous_state_sha256,
            "candidate_state_sha256": self.candidate_state_sha256,
            "source_range": self.source_range.as_dict(),
            "issues": [issue.as_dict() for issue in self.issues],
            "preserved": {key: list(self.preserved[key]) for key in sorted(self.preserved)},
            "semantic_verifier": {
                "eligible": self.accepted,
                "required": True,
                "status": "pending" if self.accepted else "not_run",
                "checks": ["semantic_omissions", "semantic_contradictions"],
            },
        }


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskStateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: Any, path: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskStateError(f"{path} must be an object")
    missing = sorted(keys - set(value))
    unknown = sorted(set(value) - keys)
    if missing:
        raise TaskStateError(f"{path} is missing required keys: {', '.join(missing)}")
    if unknown:
        raise TaskStateError(f"{path} has unknown keys: {', '.join(unknown)}")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TaskStateError(f"{path} must be a non-empty, trimmed string")
    if len(value) > MAX_TEXT:
        raise TaskStateError(f"{path} exceeds {MAX_TEXT} characters")
    return value


def _identifier(value: Any, path: str) -> str:
    text = _text(value, path)
    if not _ID_RE.fullmatch(text):
        raise TaskStateError(f"{path} must be a stable identifier")
    return text


def _sha256(value: Any, path: str) -> str:
    text = _text(value, path)
    if not _SHA256_RE.fullmatch(text):
        raise TaskStateError(f"{path} must be a canonical sha256 digest")
    return text


def _utc(value: Any, path: str) -> str:
    text = _text(value, path)
    if not _UTC_RE.fullmatch(text):
        raise TaskStateError(f"{path} must be an RFC3339 UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise TaskStateError(f"{path} must be a valid RFC3339 UTC timestamp") from exc
    return text


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise TaskStateError(f"{path} must be a list")
    if len(value) > MAX_ITEMS:
        raise TaskStateError(f"{path} exceeds {MAX_ITEMS} items")
    return value


def _id_list(value: Any, path: str) -> list[str]:
    result = [
        _identifier(item, f"{path}[{index}]")
        for index, item in enumerate(_list(value, path))
    ]
    if len(result) != len(set(result)):
        raise TaskStateError(f"{path} contains duplicate identifiers")
    return result


def _items(
    value: Any,
    path: str,
    normalizer: Any,
    *,
    identity: str = "id",
) -> list[dict[str, Any]]:
    result = [normalizer(item, f"{path}[{index}]") for index, item in enumerate(_list(value, path))]
    identities = [item[identity] for item in result]
    if len(identities) != len(set(identities)):
        raise TaskStateError(f"{path} contains duplicate {identity} values")
    return result


def _objective(value: Any, path: str) -> dict[str, str]:
    item = _object(value, path, {"text", "source_event_id"})
    return {
        "text": _text(item["text"], f"{path}.text"),
        "source_event_id": _identifier(item["source_event_id"], f"{path}.source_event_id"),
    }


def _criterion(value: Any, path: str) -> dict[str, Any]:
    item = _object(value, path, {"id", "text", "status", "evidence_ids"})
    status = _text(item["status"], f"{path}.status")
    if status not in {"open", "met"}:
        raise TaskStateError(f"{path}.status must be open or met")
    evidence_ids = _id_list(item["evidence_ids"], f"{path}.evidence_ids")
    if status == "met" and not evidence_ids:
        raise TaskStateError(f"{path}.evidence_ids must prove a met criterion")
    return {
        "id": _identifier(item["id"], f"{path}.id"),
        "text": _text(item["text"], f"{path}.text"),
        "status": status,
        "evidence_ids": evidence_ids,
    }


def _constraint(value: Any, path: str) -> dict[str, str]:
    item = _object(value, path, {"id", "text", "status", "source_event_id"})
    status = _text(item["status"], f"{path}.status")
    if status not in {"active", "retired"}:
        raise TaskStateError(f"{path}.status must be active or retired")
    return {
        "id": _identifier(item["id"], f"{path}.id"),
        "text": _text(item["text"], f"{path}.text"),
        "status": status,
        "source_event_id": _identifier(item["source_event_id"], f"{path}.source_event_id"),
    }


def _decision(value: Any, path: str) -> dict[str, Any]:
    item = _object(value, path, {"id", "text", "rationale", "source_event_ids"})
    source_event_ids = _id_list(item["source_event_ids"], f"{path}.source_event_ids")
    if not source_event_ids:
        raise TaskStateError(f"{path}.source_event_ids must cite at least one event")
    return {
        "id": _identifier(item["id"], f"{path}.id"),
        "text": _text(item["text"], f"{path}.text"),
        "rationale": _text(item["rationale"], f"{path}.rationale"),
        "source_event_ids": source_event_ids,
    }


def _completed(value: Any, path: str) -> dict[str, Any]:
    item = _object(value, path, {"id", "text", "evidence_ids", "completed_at"})
    evidence_ids = _id_list(item["evidence_ids"], f"{path}.evidence_ids")
    if not evidence_ids:
        raise TaskStateError(f"{path}.evidence_ids must prove completed work")
    return {
        "id": _identifier(item["id"], f"{path}.id"),
        "text": _text(item["text"], f"{path}.text"),
        "evidence_ids": evidence_ids,
        "completed_at": _utc(item["completed_at"], f"{path}.completed_at"),
    }


def _current_action(value: Any, path: str) -> dict[str, str]:
    item = _object(value, path, {"text", "owner", "started_at"})
    return {
        "text": _text(item["text"], f"{path}.text"),
        "owner": _identifier(item["owner"], f"{path}.owner"),
        "started_at": _utc(item["started_at"], f"{path}.started_at"),
    }


def _next_action(value: Any, path: str) -> dict[str, Any]:
    item = _object(value, path, {"id", "order", "text", "depends_on"})
    order = item["order"]
    if isinstance(order, bool) or not isinstance(order, int) or order < 1:
        raise TaskStateError(f"{path}.order must be a positive integer")
    return {
        "id": _identifier(item["id"], f"{path}.id"),
        "order": order,
        "text": _text(item["text"], f"{path}.text"),
        "depends_on": _id_list(item["depends_on"], f"{path}.depends_on"),
    }


def _blocker(value: Any, path: str) -> dict[str, Any]:
    item = _object(value, path, {"id", "text", "status", "evidence_ids"})
    status = _text(item["status"], f"{path}.status")
    if status not in {"active", "cleared"}:
        raise TaskStateError(f"{path}.status must be active or cleared")
    evidence_ids = _id_list(item["evidence_ids"], f"{path}.evidence_ids")
    if status == "cleared" and not evidence_ids:
        raise TaskStateError(f"{path}.evidence_ids must prove a cleared blocker")
    return {
        "id": _identifier(item["id"], f"{path}.id"),
        "text": _text(item["text"], f"{path}.text"),
        "status": status,
        "evidence_ids": evidence_ids,
    }


def _tool_call(value: Any, path: str) -> dict[str, str]:
    item = _object(value, path, {"call_id", "tool", "outcome"})
    outcome = _text(item["outcome"], f"{path}.outcome")
    if outcome not in {"not_started", "unknown"}:
        raise TaskStateError(f"{path}.outcome must be not_started or unknown")
    return {
        "call_id": _identifier(item["call_id"], f"{path}.call_id"),
        "tool": _identifier(item["tool"], f"{path}.tool"),
        "outcome": outcome,
    }


def _artifact(value: Any, path: str) -> dict[str, str]:
    item = _object(value, path, {"path", "sha256", "evidence_id"})
    return {
        "path": _text(item["path"], f"{path}.path"),
        "sha256": _sha256(item["sha256"], f"{path}.sha256"),
        "evidence_id": _identifier(item["evidence_id"], f"{path}.evidence_id"),
    }


def _dispute(value: Any, path: str) -> dict[str, Any]:
    item = _object(value, path, {"id", "text", "status", "evidence_ids"})
    status = _text(item["status"], f"{path}.status")
    if status not in {"unresolved", "resolved"}:
        raise TaskStateError(f"{path}.status must be unresolved or resolved")
    evidence_ids = _id_list(item["evidence_ids"], f"{path}.evidence_ids")
    if not evidence_ids:
        raise TaskStateError(f"{path}.evidence_ids must cite the dispute")
    return {
        "id": _identifier(item["id"], f"{path}.id"),
        "text": _text(item["text"], f"{path}.text"),
        "status": status,
        "evidence_ids": evidence_ids,
    }


def validate_task_state(value: Any) -> dict[str, Any]:
    """Validate and return the canonical v1 task-state projection."""

    state = _object(value, "state", _TOP_LEVEL_KEYS)
    if state["schema"] != TASK_STATE_SCHEMA:
        raise TaskStateError(f"state.schema must be {TASK_STATE_SCHEMA}")

    previous_hash = state["previous_state_sha256"]
    if previous_hash is not None:
        previous_hash = _sha256(previous_hash, "state.previous_state_sha256")

    normalized: dict[str, Any] = {
        "schema": TASK_STATE_SCHEMA,
        "task_id": _identifier(state["task_id"], "state.task_id"),
        "objective": _objective(state["objective"], "state.objective"),
        "success_criteria": _items(
            state["success_criteria"], "state.success_criteria", _criterion
        ),
        "constraints": _items(state["constraints"], "state.constraints", _constraint),
        "decisions": _items(state["decisions"], "state.decisions", _decision),
        "completed": _items(state["completed"], "state.completed", _completed),
        "current_action": _current_action(state["current_action"], "state.current_action"),
        "next_actions": _items(state["next_actions"], "state.next_actions", _next_action),
        "blockers": _items(state["blockers"], "state.blockers", _blocker),
        "open_tool_calls": _items(
            state["open_tool_calls"],
            "state.open_tool_calls",
            _tool_call,
            identity="call_id",
        ),
        "artifacts": _items(
            state["artifacts"], "state.artifacts", _artifact, identity="path"
        ),
        "disputes": _items(state["disputes"], "state.disputes", _dispute),
        "through_event_id": _identifier(
            state["through_event_id"], "state.through_event_id"
        ),
        "previous_state_sha256": previous_hash,
    }

    orders = [item["order"] for item in normalized["next_actions"]]
    if len(orders) != len(set(orders)):
        raise TaskStateError("state.next_actions contains duplicate order values")
    action_ids = {item["id"] for item in normalized["next_actions"]}
    for index, item in enumerate(normalized["next_actions"]):
        missing = sorted(set(item["depends_on"]) - action_ids)
        if missing:
            raise TaskStateError(
                f"state.next_actions[{index}].depends_on names unknown actions: "
                + ", ".join(missing)
            )
        if item["id"] in item["depends_on"]:
            raise TaskStateError(f"state.next_actions[{index}] cannot depend on itself")
    return normalized


def serialize_task_state(value: Any) -> bytes:
    """Return stable canonical JSON bytes for a validated task state."""

    return _canonical(validate_task_state(value))


def task_state_sha256(value: Any) -> str:
    """Hash the complete canonical state, including its previous-state link."""

    return "sha256:" + hashlib.sha256(serialize_task_state(value)).hexdigest()


def load_task_state(path: Path) -> dict[str, Any]:
    """Load a state revision without trusting JSON duplicate-key behavior."""

    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            raise TaskStateError(f"state exceeds {MAX_STATE_BYTES} bytes")
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except TaskStateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskStateError(f"cannot load task state: {exc}") from exc
    return validate_task_state(value)


def write_task_state(path: Path, value: Any) -> None:
    """Durably create one immutable state revision."""

    encoded = serialize_task_state(value)
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_file() and path.read_bytes() == encoded:
            return
        raise TaskStateError(f"refusing to overwrite state revision: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise TaskStateError(f"refusing to overwrite state revision: {path}") from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _indexed(
    items: Sequence[Mapping[str, Any]], identity: str = "id"
) -> dict[str, Mapping[str, Any]]:
    return {str(item[identity]): item for item in items}


def _issue(issues: list[ValidationIssue], code: str, path: str, message: str) -> None:
    issues.append(ValidationIssue(code=code, path=path, message=message))


def _check_references(
    state: Mapping[str, Any], resolver: CitationResolver, issues: list[ValidationIssue]
) -> None:
    event_paths: list[tuple[str, str]] = [
        ("objective.source_event_id", state["objective"]["source_event_id"]),
        ("through_event_id", state["through_event_id"]),
    ]
    for index, item in enumerate(state["constraints"]):
        event_paths.append((f"constraints[{index}].source_event_id", item["source_event_id"]))
    for index, item in enumerate(state["decisions"]):
        event_paths.extend(
            (f"decisions[{index}].source_event_ids[{subindex}]", event_id)
            for subindex, event_id in enumerate(item["source_event_ids"])
        )

    evidence_paths: list[tuple[str, str]] = []
    for field in ("success_criteria", "completed", "blockers", "disputes"):
        for index, item in enumerate(state[field]):
            evidence_paths.extend(
                (f"{field}[{index}].evidence_ids[{subindex}]", evidence_id)
                for subindex, evidence_id in enumerate(item["evidence_ids"])
            )
    evidence_paths.extend(
        (f"artifacts[{index}].evidence_id", item["evidence_id"])
        for index, item in enumerate(state["artifacts"])
    )

    for path, event_id in event_paths:
        try:
            exists = resolver.event_record_sha256(event_id) is not None
        except Exception as exc:  # resolver is an external read-only boundary
            _issue(issues, "resolver_error", path, f"event resolver failed: {exc}")
            continue
        if not exists:
            _issue(issues, "missing_event", path, f"event does not exist: {event_id}")

    for path, evidence_id in evidence_paths:
        try:
            exists = resolver.evidence_exists(evidence_id)
        except Exception as exc:  # resolver is an external read-only boundary
            _issue(issues, "resolver_error", path, f"evidence resolver failed: {exc}")
            continue
        if not exists:
            _issue(issues, "missing_evidence", path, f"evidence does not exist: {evidence_id}")

    for index, item in enumerate(state["artifacts"]):
        try:
            actual = resolver.artifact_sha256(item["path"])
        except Exception as exc:  # resolver is an external read-only boundary
            _issue(
                issues,
                "resolver_error",
                f"artifacts[{index}]",
                f"artifact resolver failed: {exc}",
            )
            continue
        if actual is None:
            _issue(
                issues,
                "missing_artifact",
                f"artifacts[{index}].path",
                f"artifact cannot be observed: {item['path']}",
            )
        elif actual != item["sha256"]:
            _issue(
                issues,
                "stale_artifact",
                f"artifacts[{index}].sha256",
                f"recorded {item['sha256']} but observed {actual}",
            )


def _check_source_range(
    candidate: Mapping[str, Any],
    source_range: FrozenSourceRange,
    resolver: CitationResolver,
    issues: list[ValidationIssue],
) -> None:
    try:
        first_id = _identifier(source_range.first_event_id, "source_range.first_event_id")
        last_id = _identifier(source_range.last_event_id, "source_range.last_event_id")
        tail_id = _identifier(
            source_range.retained_tail_event_id, "source_range.retained_tail_event_id"
        )
        first_hash = _sha256(
            source_range.first_record_sha256, "source_range.first_record_sha256"
        )
        last_hash = _sha256(
            source_range.last_record_sha256, "source_range.last_record_sha256"
        )
    except TaskStateError as exc:
        _issue(issues, "invalid_source_range", "source_range", str(exc))
        return

    for label, event_id, expected in (
        ("first", first_id, first_hash),
        ("last", last_id, last_hash),
    ):
        try:
            observed = resolver.event_record_sha256(event_id)
        except Exception as exc:  # resolver is an external read-only boundary
            _issue(
                issues,
                "resolver_error",
                f"source_range.{label}_event_id",
                f"event resolver failed: {exc}",
            )
            continue
        if observed is None:
            _issue(
                issues,
                "missing_event",
                f"source_range.{label}_event_id",
                f"event does not exist: {event_id}",
            )
        elif observed != expected:
            _issue(
                issues,
                "source_hash_mismatch",
                f"source_range.{label}_record_sha256",
                f"recorded {expected} but observed {observed}",
            )

    try:
        tail_exists = resolver.event_record_sha256(tail_id) is not None
    except Exception as exc:  # resolver is an external read-only boundary
        _issue(
            issues,
            "resolver_error",
            "source_range.retained_tail_event_id",
            f"event resolver failed: {exc}",
        )
    else:
        if not tail_exists:
            _issue(
                issues,
                "missing_event",
                "source_range.retained_tail_event_id",
                f"event does not exist: {tail_id}",
            )

    if candidate["through_event_id"] != last_id:
        _issue(
            issues,
            "stale_through_event",
            "through_event_id",
            "candidate must advance through the frozen source range's last event",
        )


def _check_invariants(
    previous: Mapping[str, Any],
    candidate: Mapping[str, Any],
    issues: list[ValidationIssue],
) -> dict[str, tuple[str, ...]]:
    preserved: dict[str, tuple[str, ...]] = {}
    if previous["task_id"] != candidate["task_id"]:
        _issue(issues, "task_changed", "task_id", "compaction cannot change task_id")
    if previous["objective"] != candidate["objective"]:
        _issue(issues, "objective_changed", "objective", "objective must survive exactly")

    exact_fields = {
        "constraints": "id",
        "decisions": "id",
        "completed": "id",
        "next_actions": "id",
        "open_tool_calls": "call_id",
        "artifacts": "path",
        "disputes": "id",
    }
    for field, identity in exact_fields.items():
        old = _indexed(previous[field], identity)
        new = _indexed(candidate[field], identity)
        preserved[field] = tuple(sorted(old))
        for item_id, old_item in old.items():
            new_item = new.get(item_id)
            path = f"{field}[{item_id}]"
            if new_item is None:
                _issue(issues, "invariant_omitted", path, f"required {field} item was omitted")
            elif old_item != new_item:
                _issue(issues, "invariant_changed", path, f"required {field} item changed")

    old_criteria = _indexed(previous["success_criteria"])
    new_criteria = _indexed(candidate["success_criteria"])
    preserved["success_criteria"] = tuple(sorted(old_criteria))
    for item_id, old_item in old_criteria.items():
        new_item = new_criteria.get(item_id)
        path = f"success_criteria[{item_id}]"
        if new_item is None:
            _issue(issues, "invariant_omitted", path, "success criterion was omitted")
            continue
        if old_item["text"] != new_item["text"]:
            _issue(issues, "invariant_changed", path, "success criterion text changed")
        if not set(old_item["evidence_ids"]).issubset(new_item["evidence_ids"]):
            _issue(issues, "evidence_omitted", path, "success criterion evidence was omitted")
        if old_item["status"] == "met" and new_item["status"] != "met":
            _issue(issues, "completion_reversed", path, "met criterion returned to open")
        newly_met_without_evidence = (
            old_item["status"] == "open"
            and new_item["status"] == "met"
            and not (set(new_item["evidence_ids"]) - set(old_item["evidence_ids"]))
        )
        if newly_met_without_evidence:
            _issue(
                issues,
                "unproven_completion",
                path,
                "newly met criterion lacks new completion evidence",
            )

    old_blockers = _indexed(previous["blockers"])
    new_blockers = _indexed(candidate["blockers"])
    preserved["blockers"] = tuple(sorted(old_blockers))
    for item_id, old_item in old_blockers.items():
        new_item = new_blockers.get(item_id)
        path = f"blockers[{item_id}]"
        if new_item is None:
            _issue(issues, "invariant_omitted", path, "blocker was omitted")
            continue
        if old_item["text"] != new_item["text"]:
            _issue(issues, "invariant_changed", path, "blocker text changed")
        if not set(old_item["evidence_ids"]).issubset(new_item["evidence_ids"]):
            _issue(issues, "evidence_omitted", path, "blocker evidence was omitted")
        if old_item["status"] == "cleared" and new_item["status"] != "cleared":
            _issue(issues, "blocker_reopened", path, "cleared blocker returned to active")
        newly_cleared_without_evidence = (
            old_item["status"] == "active"
            and new_item["status"] == "cleared"
            and not (set(new_item["evidence_ids"]) - set(old_item["evidence_ids"]))
        )
        if newly_cleared_without_evidence:
            _issue(
                issues,
                "unproven_clearance",
                path,
                "cleared blocker lacks new clearance evidence",
            )

    previous_completed = set(_indexed(previous["completed"]))
    for item_id, item in _indexed(candidate["completed"]).items():
        if item_id not in previous_completed and not item["evidence_ids"]:
            _issue(
                issues,
                "unproven_completion",
                f"completed[{item_id}]",
                "newly completed item lacks evidence",
            )

    if previous["current_action"] != candidate["current_action"]:
        _issue(
            issues,
            "current_action_changed",
            "current_action",
            "compaction cannot change the active action without a prior state transition",
        )
    preserved["objective"] = (previous["objective"]["source_event_id"],)
    return preserved


def validate_compaction_proposal(
    previous_state: Any,
    candidate_state: Any,
    source_range: FrozenSourceRange,
    resolver: CitationResolver,
) -> CompactionValidationResult:
    """Validate a proposed state without invoking or trusting a model.

    Passing this function is necessary but not sufficient for commit.  The
    returned object explicitly hands the proposal to an independent semantic
    verifier for omission/contradiction review.
    """

    issues: list[ValidationIssue] = []
    try:
        previous = validate_task_state(previous_state)
        previous_hash = task_state_sha256(previous)
    except TaskStateError as exc:
        _issue(issues, "invalid_previous_state", "previous_state", str(exc))
        previous = None
        previous_hash = None

    try:
        candidate = validate_task_state(candidate_state)
        candidate_hash = task_state_sha256(candidate)
    except TaskStateError as exc:
        _issue(issues, "invalid_candidate_state", "candidate_state", str(exc))
        candidate = None
        candidate_hash = None

    preserved: dict[str, tuple[str, ...]] = {}
    if candidate is not None:
        _check_source_range(candidate, source_range, resolver, issues)
        _check_references(candidate, resolver, issues)
    if previous is not None and candidate is not None and previous_hash is not None:
        if candidate["previous_state_sha256"] != previous_hash:
            _issue(
                issues,
                "previous_state_hash_mismatch",
                "previous_state_sha256",
                f"expected {previous_hash}",
            )
        if candidate["through_event_id"] == previous["through_event_id"]:
            _issue(
                issues,
                "stale_through_event",
                "through_event_id",
                "candidate does not advance beyond the previous state",
            )
        preserved = _check_invariants(previous, candidate, issues)

    ordered = tuple(sorted(set(issues)))
    return CompactionValidationResult(
        accepted=not ordered,
        previous_state_sha256=previous_hash,
        candidate_state_sha256=candidate_hash,
        source_range=source_range,
        issues=ordered,
        preserved=preserved,
    )


def validate_state_chain(states: Sequence[Any]) -> tuple[str, ...]:
    """Validate an ordered immutable state-hash chain and return its digests."""

    if not states:
        raise TaskStateError("state chain must contain at least one revision")
    normalized = [validate_task_state(state) for state in states]
    if normalized[0]["previous_state_sha256"] is not None:
        raise TaskStateError("first state revision must have a null previous_state_sha256")
    task_id = normalized[0]["task_id"]
    digests = [task_state_sha256(normalized[0])]
    for index, state in enumerate(normalized[1:], start=1):
        if state["task_id"] != task_id:
            raise TaskStateError(f"state chain task_id changed at revision {index}")
        if state["previous_state_sha256"] != digests[-1]:
            raise TaskStateError(f"state chain is broken at revision {index}")
        digests.append(task_state_sha256(state))
    return tuple(digests)
