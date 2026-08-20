"""Deterministic contracts for Memory v1 retention planning and execution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

RETENTION_PLAN_SCHEMA = "coding-intelligence-memory-retention-plan/v1"
RETENTION_RESULT_SCHEMA = "coding-intelligence-memory-retention-result/v1"
RETENTION_TOMBSTONE_SCHEMA = "coding-intelligence-memory-blob-tombstone/v1"
BLOB_REHYDRATION_SCHEMA = "coding-intelligence-memory-blob-rehydration/v1"

SCRATCH_HOT_SECONDS = 24 * 60 * 60
BULK_HOT_SECONDS = 14 * 24 * 60 * 60
BULK_ARCHIVE_SECONDS = 90 * 24 * 60 * 60
EVIDENCE_HOT_SECONDS = 180 * 24 * 60 * 60
GC_PRESSURE_BASIS_POINTS = 8_000
BULK_REFUSAL_BASIS_POINTS = 9_500
EXCERPT_CHARACTERS = 256

_ACTIONS = {"keep", "archive", "evict"}
_AVAILABILITY = {"hot", "archived", "evicted"}
_RETENTION_CLASSES = {"core", "evidence", "bulk", "scratch"}
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class RetentionContractError(ValueError):
    """A retention plan/result does not satisfy its deterministic contract."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RetentionContractError(f"value is not canonical JSON: {error}") from error


def sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class RetentionDecision:
    """One deterministic decision about one content-addressed blob."""

    blob_sha256: str
    retention_class: str
    availability_before: str
    action: str
    reason: str
    size_bytes: int
    hot_locator: str | None
    created_at: str
    task_terminal_at: str | None
    expires_at: str | None
    source_event_ids: tuple[str, ...]
    scope_keys: tuple[str, ...]
    pin_reasons: tuple[str, ...]
    cited: bool

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.blob_sha256):
            raise RetentionContractError("invalid blob_sha256")
        if self.retention_class not in _RETENTION_CLASSES:
            raise RetentionContractError("invalid retention class")
        if self.availability_before not in _AVAILABILITY:
            raise RetentionContractError("invalid availability")
        if self.action not in _ACTIONS:
            raise RetentionContractError("invalid retention action")
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool):
            raise RetentionContractError("size_bytes must be an integer")
        if self.size_bytes < 0:
            raise RetentionContractError("size_bytes must be non-negative")
        if not isinstance(self.reason, str) or not self.reason:
            raise RetentionContractError("retention reason must be non-empty text")
        if not isinstance(self.cited, bool):
            raise RetentionContractError("cited must be boolean")
        if tuple(sorted(set(self.source_event_ids))) != self.source_event_ids:
            raise RetentionContractError("source_event_ids must be unique and sorted")
        if tuple(sorted(set(self.scope_keys))) != self.scope_keys:
            raise RetentionContractError("scope_keys must be unique and sorted")
        if tuple(sorted(set(self.pin_reasons))) != self.pin_reasons:
            raise RetentionContractError("pin_reasons must be unique and sorted")

    def as_dict(self) -> dict[str, Any]:
        return {
            "blob_sha256": self.blob_sha256,
            "retention_class": self.retention_class,
            "availability_before": self.availability_before,
            "action": self.action,
            "reason": self.reason,
            "size_bytes": self.size_bytes,
            "hot_locator": self.hot_locator,
            "created_at": self.created_at,
            "task_terminal_at": self.task_terminal_at,
            "expires_at": self.expires_at,
            "source_event_ids": list(self.source_event_ids),
            "scope_keys": list(self.scope_keys),
            "pin_reasons": list(self.pin_reasons),
            "cited": self.cited,
        }

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> RetentionDecision:
        required = {
            "blob_sha256",
            "retention_class",
            "availability_before",
            "action",
            "reason",
            "size_bytes",
            "hot_locator",
            "created_at",
            "task_terminal_at",
            "expires_at",
            "source_event_ids",
            "scope_keys",
            "pin_reasons",
            "cited",
        }
        if set(value) != required:
            raise RetentionContractError("retention decision fields do not match v1")
        return cls(
            blob_sha256=str(value["blob_sha256"]),
            retention_class=str(value["retention_class"]),
            availability_before=str(value["availability_before"]),
            action=str(value["action"]),
            reason=str(value["reason"]),
            size_bytes=value["size_bytes"],
            hot_locator=(
                None if value["hot_locator"] is None else str(value["hot_locator"])
            ),
            created_at=str(value["created_at"]),
            task_terminal_at=(
                None
                if value["task_terminal_at"] is None
                else str(value["task_terminal_at"])
            ),
            expires_at=None if value["expires_at"] is None else str(value["expires_at"]),
            source_event_ids=tuple(str(item) for item in value["source_event_ids"]),
            scope_keys=tuple(str(item) for item in value["scope_keys"]),
            pin_reasons=tuple(str(item) for item in value["pin_reasons"]),
            cited=value["cited"],
        )


@dataclass(frozen=True)
class RetentionPlan:
    """A dry-run snapshot that must be revalidated before any physical mutation."""

    observed_at: str
    hot_budget_bytes: int
    hot_bytes: int
    pressure_basis_points: int
    exact_scope_keys: tuple[str, ...]
    cited_event_ids: tuple[str, ...]
    decisions: tuple[RetentionDecision, ...]
    plan_sha256: str
    schema: str = RETENTION_PLAN_SCHEMA

    @property
    def actionable(self) -> tuple[RetentionDecision, ...]:
        return tuple(item for item in self.decisions if item.action in {"archive", "evict"})

    def _unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "observed_at": self.observed_at,
            "hot_budget_bytes": self.hot_budget_bytes,
            "hot_bytes": self.hot_bytes,
            "pressure_basis_points": self.pressure_basis_points,
            "exact_scope_keys": list(self.exact_scope_keys),
            "cited_event_ids": list(self.cited_event_ids),
            "decisions": [item.as_dict() for item in self.decisions],
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._unsigned_dict(), "plan_sha256": self.plan_sha256}

    def verify(self) -> RetentionPlan:
        if self.schema != RETENTION_PLAN_SCHEMA:
            raise RetentionContractError("unknown retention plan schema")
        for name, value in (
            ("hot_budget_bytes", self.hot_budget_bytes),
            ("hot_bytes", self.hot_bytes),
            ("pressure_basis_points", self.pressure_basis_points),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RetentionContractError(f"{name} must be a non-negative integer")
        if self.hot_budget_bytes < 1:
            raise RetentionContractError("hot_budget_bytes must be positive")
        if not _SHA256_RE.fullmatch(self.plan_sha256):
            raise RetentionContractError("invalid plan_sha256")
        if tuple(sorted(set(self.exact_scope_keys))) != self.exact_scope_keys:
            raise RetentionContractError("exact_scope_keys must be unique and sorted")
        if tuple(sorted(set(self.cited_event_ids))) != self.cited_event_ids:
            raise RetentionContractError("cited_event_ids must be unique and sorted")
        order = tuple(
            (
                {"scratch": 0, "bulk": 1, "evidence": 2, "core": 3}[item.retention_class],
                item.expires_at or "9999-12-31T23:59:59Z",
                item.blob_sha256,
            )
            for item in self.decisions
        )
        if order != tuple(sorted(order)):
            raise RetentionContractError("retention decisions are not deterministically ordered")
        expected = sha256(canonical_bytes(self._unsigned_dict()))
        if self.plan_sha256 != expected:
            raise RetentionContractError("retention plan digest mismatch")
        return self

    @classmethod
    def create(
        cls,
        *,
        observed_at: str,
        hot_budget_bytes: int,
        hot_bytes: int,
        pressure_basis_points: int,
        exact_scope_keys: Sequence[str],
        cited_event_ids: Sequence[str],
        decisions: Sequence[RetentionDecision],
    ) -> RetentionPlan:
        provisional = cls(
            observed_at=observed_at,
            hot_budget_bytes=hot_budget_bytes,
            hot_bytes=hot_bytes,
            pressure_basis_points=pressure_basis_points,
            exact_scope_keys=tuple(exact_scope_keys),
            cited_event_ids=tuple(cited_event_ids),
            decisions=tuple(decisions),
            plan_sha256="",
        )
        digest = sha256(canonical_bytes(provisional._unsigned_dict()))
        return cls(**{**provisional.__dict__, "plan_sha256": digest}).verify()

    @classmethod
    def from_value(cls, value: RetentionPlan | Mapping[str, Any]) -> RetentionPlan:
        if isinstance(value, cls):
            return value.verify()
        required = {
            "schema",
            "observed_at",
            "hot_budget_bytes",
            "hot_bytes",
            "pressure_basis_points",
            "exact_scope_keys",
            "cited_event_ids",
            "decisions",
            "plan_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise RetentionContractError("retention plan fields do not match v1")
        return cls(
            schema=str(value["schema"]),
            observed_at=str(value["observed_at"]),
            hot_budget_bytes=value["hot_budget_bytes"],
            hot_bytes=value["hot_bytes"],
            pressure_basis_points=value["pressure_basis_points"],
            exact_scope_keys=tuple(str(item) for item in value["exact_scope_keys"]),
            cited_event_ids=tuple(str(item) for item in value["cited_event_ids"]),
            decisions=tuple(RetentionDecision.from_value(item) for item in value["decisions"]),
            plan_sha256=str(value["plan_sha256"]),
        ).verify()
