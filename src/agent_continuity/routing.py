"""Deterministic, evidence-bound routing for Coding Intelligence.

This module is deliberately a pure control-plane component.  It validates a
versioned manifest, checks an observed runtime snapshot, and returns a route
decision plus complete rejection/fallback telemetry.  It never starts a
backend, calls a model, touches the network, retries work, or mutates model
qualification state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

OUTCOME_STATUSES = frozenset(
    {
        "accepted",
        "rejected",
        "inconclusive",
        "evaluator_invalid",
        "runtime_blocked",
        "not_run",
    }
)
PRODUCTION_ELIGIBLE_STATUSES = frozenset({"accepted"})
ROUTING_SCHEMA_VERSION = 1
AUTOMATIC_RETRIES = 0

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class RoutingManifestError(ValueError):
    """The routing manifest is malformed, contradictory, or has stale evidence."""


class RoutingRequestError(ValueError):
    """A routing request or runtime snapshot is malformed."""


def _json_snapshot(value: Any, label: str) -> dict[str, Any]:
    """Return a detached canonical JSON snapshot of an input object.

    Validation must never retain caller-owned mutable containers.  Taking the
    snapshot up front also guarantees the manifest hash and every normalized
    record are derived from the same bytes.
    """

    try:
        snapshot = json.loads(_canonical_bytes(value))
    except (TypeError, ValueError, RecursionError) as error:
        raise RoutingManifestError(f"{label} is not canonical JSON: {error}") from error
    return _object(snapshot, label)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoutingManifestError(f"{label} must be an object")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"extra={extra}")
        raise RoutingManifestError(f"{label} has unexpected fields: {', '.join(details)}")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise RoutingManifestError(f"{label} must be a lowercase stable identifier")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise RoutingManifestError(f"{label} must be a non-empty string")
    return value.strip()


def _string_tuple(
    value: Any,
    label: str,
    *,
    identifiers: bool = True,
    allow_empty: bool = False,
    allow_wildcard: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RoutingManifestError(f"{label} must be an array of strings")
    if not allow_empty and not value:
        raise RoutingManifestError(f"{label} must not be empty")
    if len(value) != len(set(value)):
        raise RoutingManifestError(f"{label} must not contain duplicates")
    output = tuple(value)
    if identifiers:
        for index, item in enumerate(output):
            if allow_wildcard and item == "*":
                continue
            _identifier(item, f"{label}[{index}]")
    elif any(not item.strip() or "\x00" in item for item in output):
        raise RoutingManifestError(f"{label} contains an empty or invalid string")
    return output


def _status(value: Any, label: str) -> str:
    if value not in OUTCOME_STATUSES:
        raise RoutingManifestError(f"{label} must be one of {', '.join(sorted(OUTCOME_STATUSES))}")
    return str(value)


def _locality(value: Any, label: str) -> Literal["cloud", "local"]:
    if value not in {"cloud", "local"}:
        raise RoutingManifestError(f"{label} must be 'cloud' or 'local'")
    return value


def _optional_context_limit(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1024:
        raise RoutingManifestError(f"{label} must be null or an integer >= 1024")
    return value


def _evidence_ids(
    value: Any,
    label: str,
    evidence: dict[str, EvidenceRecord],
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    ids = _string_tuple(value, label, allow_empty=allow_empty)
    unknown = sorted(set(ids) - set(evidence))
    if unknown:
        raise RoutingManifestError(f"{label} references unknown evidence: {unknown}")
    return ids


@dataclass(frozen=True)
class EvidenceRecord:
    id: str
    path: str
    sha256: str


@dataclass(frozen=True)
class ModelComponent:
    id: str
    name: str
    provider: str
    locality: Literal["cloud", "local"]
    roles: tuple[str, ...]
    languages: tuple[str, ...]
    max_context_tokens: int | None
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class HarnessComponent:
    id: str
    name: str
    locality: Literal["cloud", "local"]
    mode: str
    tools: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class EdgeComponent:
    id: str
    name: str
    locality: Literal["cloud", "local"]
    kind: Literal["inference", "execution", "combined"]
    capabilities: tuple[str, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class VerifierComponent:
    id: str
    name: str
    model_id: str
    harness_id: str
    inference_edge_id: str
    status: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class RouteDefinition:
    id: str
    name: str
    qualification_status: str
    composition: Literal["single", "team"]
    model_ids: tuple[str, ...]
    harness_id: str
    verifier_id: str | None
    inference_edge_id: str
    execution_edge_id: str
    roles: tuple[str, ...]
    capabilities: tuple[str, ...]
    languages: tuple[str, ...]
    tool_needs: tuple[str, ...]
    max_context_tokens: int | None
    priority: int
    required_runtime_tags: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    baseline_route_id: str | None
    team_gain_basis_points: int | None
    comparison_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class RoutingPolicy:
    cloud_primary_route_id: str
    sovereign_route_ids: tuple[str, ...]
    apple_execution_edge_id: str
    production_eligible_statuses: tuple[str, ...]
    automatic_retries: int


@dataclass(frozen=True)
class RoutingManifest:
    schema_version: int
    manifest_id: str
    evidence: Mapping[str, EvidenceRecord]
    models: Mapping[str, ModelComponent]
    harnesses: Mapping[str, HarnessComponent]
    verifiers: Mapping[str, VerifierComponent]
    edges: Mapping[str, EdgeComponent]
    routes: Mapping[str, RouteDefinition]
    policy: RoutingPolicy
    sha256: str


@dataclass(frozen=True)
class RoutingRequest:
    task_id: str
    role: str
    capability: str
    language: str
    context_tokens: int
    tool_needs: tuple[str, ...]
    mode: Literal["production", "qualification"] = "production"
    candidate_route_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not _ID_RE.fullmatch(self.task_id):
            raise RoutingRequestError("task_id must be a lowercase stable identifier")
        for label, value in (
            ("role", self.role),
            ("capability", self.capability),
            ("language", self.language),
        ):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                raise RoutingRequestError(f"{label} must be a lowercase stable identifier")
        if (
            not isinstance(self.context_tokens, int)
            or isinstance(self.context_tokens, bool)
            or self.context_tokens < 1
        ):
            raise RoutingRequestError("context_tokens must be a positive integer")
        if not isinstance(self.tool_needs, tuple) or len(self.tool_needs) != len(
            set(self.tool_needs)
        ):
            raise RoutingRequestError("tool_needs must be a duplicate-free tuple")
        for item in self.tool_needs:
            if not isinstance(item, str) or not _ID_RE.fullmatch(item):
                raise RoutingRequestError("tool_needs must contain stable identifiers")
        if self.mode not in {"production", "qualification"}:
            raise RoutingRequestError("mode must be production or qualification")
        if self.mode == "qualification" and self.candidate_route_id is None:
            raise RoutingRequestError("qualification mode requires candidate_route_id")
        if self.mode == "production" and self.candidate_route_id is not None:
            raise RoutingRequestError("production mode forbids candidate_route_id")
        if self.candidate_route_id is not None and (
            not isinstance(self.candidate_route_id, str)
            or not _ID_RE.fullmatch(self.candidate_route_id)
        ):
            raise RoutingRequestError("candidate_route_id must be a stable identifier")

    def canonical(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "capability": self.capability,
            "language": self.language,
            "context_tokens": self.context_tokens,
            "tool_needs": sorted(self.tool_needs),
            "mode": self.mode,
            "candidate_route_id": self.candidate_route_id,
        }


@dataclass(frozen=True)
class RuntimeState:
    cloud_available: bool
    operable_model_ids: frozenset[str]
    operable_harness_ids: frozenset[str]
    available_edge_ids: frozenset[str]
    satisfied_runtime_tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.cloud_available, bool):
            raise RoutingRequestError("runtime cloud_available must be boolean")
        for label in (
            "operable_model_ids",
            "operable_harness_ids",
            "available_edge_ids",
            "satisfied_runtime_tags",
        ):
            value = getattr(self, label)
            if not isinstance(value, (set, frozenset)):
                raise RoutingRequestError(f"runtime {label} must be a set of stable identifiers")
            normalized = frozenset(value)
            if any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in normalized):
                raise RoutingRequestError(f"runtime {label} must contain stable identifiers")
            object.__setattr__(self, label, normalized)

    def canonical(self) -> dict[str, Any]:
        return {
            "cloud_available": self.cloud_available,
            "operable_model_ids": sorted(self.operable_model_ids),
            "operable_harness_ids": sorted(self.operable_harness_ids),
            "available_edge_ids": sorted(self.available_edge_ids),
            "satisfied_runtime_tags": sorted(self.satisfied_runtime_tags),
        }


@dataclass(frozen=True)
class RouteConsideration:
    route_id: str
    qualification_status: str
    eligible: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class RoutingDecision:
    status: Literal["routed", "unroutable"]
    selected_route_id: str | None
    model_ids: tuple[str, ...]
    harness_id: str | None
    verifier_id: str | None
    inference_edge_id: str | None
    execution_edge_id: str | None
    fallback_from_route_id: str | None
    fallback_reason_code: str | None
    fallback_detail_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    evidence_sha256: Mapping[str, str]
    considered: tuple[RouteConsideration, ...]
    request_sha256: str
    runtime_sha256: str
    manifest_sha256: str
    automatic_retries: int = AUTOMATIC_RETRIES

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "selected_route_id": self.selected_route_id,
            "model_ids": list(self.model_ids),
            "harness_id": self.harness_id,
            "verifier_id": self.verifier_id,
            "inference_edge_id": self.inference_edge_id,
            "execution_edge_id": self.execution_edge_id,
            "fallback_from_route_id": self.fallback_from_route_id,
            "fallback_reason_code": self.fallback_reason_code,
            "fallback_detail_codes": list(self.fallback_detail_codes),
            "evidence_ids": list(self.evidence_ids),
            "evidence_sha256": dict(self.evidence_sha256),
            "considered": [
                {
                    **asdict(item),
                    "reason_codes": list(item.reason_codes),
                }
                for item in self.considered
            ],
            "request_sha256": self.request_sha256,
            "runtime_sha256": self.runtime_sha256,
            "manifest_sha256": self.manifest_sha256,
            "automatic_retries": self.automatic_retries,
        }


def _parse_evidence(
    values: Any,
    *,
    evidence_root: Path | None,
    verify_evidence: bool,
) -> dict[str, EvidenceRecord]:
    if not isinstance(values, list) or not values:
        raise RoutingManifestError("evidence must be a non-empty array")
    output: dict[str, EvidenceRecord] = {}
    resolved_root = evidence_root.resolve() if evidence_root is not None else None
    for index, item in enumerate(values):
        raw = _object(item, f"evidence[{index}]")
        _exact_keys(raw, {"id", "path", "sha256"}, f"evidence[{index}]")
        evidence_id = _identifier(raw["id"], f"evidence[{index}].id")
        if evidence_id in output:
            raise RoutingManifestError(f"duplicate evidence id: {evidence_id}")
        path_text = _text(raw["path"], f"evidence[{index}].path").replace("\\", "/")
        path = PurePosixPath(path_text)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise RoutingManifestError(f"unsafe evidence path: {path_text!r}")
        digest = raw["sha256"]
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise RoutingManifestError(f"evidence[{index}].sha256 must be canonical")
        if verify_evidence:
            if resolved_root is None:
                raise RoutingManifestError("verify_evidence requires evidence_root")
            target = (resolved_root / Path(*path.parts)).resolve()
            try:
                target.relative_to(resolved_root)
            except ValueError as error:
                raise RoutingManifestError(f"evidence path escapes root: {path_text}") from error
            if not target.is_file() or target.is_symlink():
                raise RoutingManifestError(f"evidence is not a regular file: {path_text}")
            observed = _file_sha256(target)
            if observed != digest:
                raise RoutingManifestError(
                    f"evidence hash mismatch for {evidence_id}: expected {digest}, got {observed}"
                )
        output[evidence_id] = EvidenceRecord(evidence_id, path.as_posix(), digest)
    return output


def _parse_models(values: Any, evidence: dict[str, EvidenceRecord]) -> dict[str, ModelComponent]:
    if not isinstance(values, list) or not values:
        raise RoutingManifestError("components.models must be a non-empty array")
    output: dict[str, ModelComponent] = {}
    keys = {
        "id",
        "name",
        "provider",
        "locality",
        "roles",
        "languages",
        "max_context_tokens",
        "evidence_ids",
    }
    for index, item in enumerate(values):
        raw = _object(item, f"components.models[{index}]")
        _exact_keys(raw, keys, f"components.models[{index}]")
        model_id = _identifier(raw["id"], f"components.models[{index}].id")
        if model_id in output:
            raise RoutingManifestError(f"duplicate model id: {model_id}")
        output[model_id] = ModelComponent(
            id=model_id,
            name=_text(raw["name"], f"models.{model_id}.name"),
            provider=_identifier(raw["provider"], f"models.{model_id}.provider"),
            locality=_locality(raw["locality"], f"models.{model_id}.locality"),
            roles=_string_tuple(raw["roles"], f"models.{model_id}.roles", allow_wildcard=True),
            languages=_string_tuple(
                raw["languages"], f"models.{model_id}.languages", allow_wildcard=True
            ),
            max_context_tokens=_optional_context_limit(
                raw["max_context_tokens"], f"models.{model_id}.max_context_tokens"
            ),
            evidence_ids=_evidence_ids(
                raw["evidence_ids"], f"models.{model_id}.evidence_ids", evidence
            ),
        )
    return output


def _parse_harnesses(
    values: Any, evidence: dict[str, EvidenceRecord]
) -> dict[str, HarnessComponent]:
    if not isinstance(values, list) or not values:
        raise RoutingManifestError("components.harnesses must be a non-empty array")
    output: dict[str, HarnessComponent] = {}
    keys = {"id", "name", "locality", "mode", "tools", "evidence_ids"}
    for index, item in enumerate(values):
        raw = _object(item, f"components.harnesses[{index}]")
        _exact_keys(raw, keys, f"components.harnesses[{index}]")
        harness_id = _identifier(raw["id"], f"components.harnesses[{index}].id")
        if harness_id in output:
            raise RoutingManifestError(f"duplicate harness id: {harness_id}")
        output[harness_id] = HarnessComponent(
            id=harness_id,
            name=_text(raw["name"], f"harnesses.{harness_id}.name"),
            locality=_locality(raw["locality"], f"harnesses.{harness_id}.locality"),
            mode=_identifier(raw["mode"], f"harnesses.{harness_id}.mode"),
            tools=_string_tuple(raw["tools"], f"harnesses.{harness_id}.tools"),
            evidence_ids=_evidence_ids(
                raw["evidence_ids"], f"harnesses.{harness_id}.evidence_ids", evidence
            ),
        )
    return output


def _parse_edges(values: Any, evidence: dict[str, EvidenceRecord]) -> dict[str, EdgeComponent]:
    if not isinstance(values, list) or not values:
        raise RoutingManifestError("components.edges must be a non-empty array")
    output: dict[str, EdgeComponent] = {}
    keys = {"id", "name", "locality", "kind", "capabilities", "evidence_ids"}
    for index, item in enumerate(values):
        raw = _object(item, f"components.edges[{index}]")
        _exact_keys(raw, keys, f"components.edges[{index}]")
        edge_id = _identifier(raw["id"], f"components.edges[{index}].id")
        if edge_id in output:
            raise RoutingManifestError(f"duplicate edge id: {edge_id}")
        kind = raw["kind"]
        if kind not in {"inference", "execution", "combined"}:
            raise RoutingManifestError(f"edges.{edge_id}.kind is invalid")
        output[edge_id] = EdgeComponent(
            id=edge_id,
            name=_text(raw["name"], f"edges.{edge_id}.name"),
            locality=_locality(raw["locality"], f"edges.{edge_id}.locality"),
            kind=kind,
            capabilities=_string_tuple(raw["capabilities"], f"edges.{edge_id}.capabilities"),
            evidence_ids=_evidence_ids(
                raw["evidence_ids"], f"edges.{edge_id}.evidence_ids", evidence
            ),
        )
    return output


def _parse_verifiers(
    values: Any,
    evidence: dict[str, EvidenceRecord],
) -> dict[str, VerifierComponent]:
    if not isinstance(values, list) or not values:
        raise RoutingManifestError("components.verifiers must be a non-empty array")
    output: dict[str, VerifierComponent] = {}
    keys = {
        "id",
        "name",
        "model_id",
        "harness_id",
        "inference_edge_id",
        "status",
        "evidence_ids",
    }
    for index, item in enumerate(values):
        raw = _object(item, f"components.verifiers[{index}]")
        _exact_keys(raw, keys, f"components.verifiers[{index}]")
        verifier_id = _identifier(raw["id"], f"components.verifiers[{index}].id")
        if verifier_id in output:
            raise RoutingManifestError(f"duplicate verifier id: {verifier_id}")
        output[verifier_id] = VerifierComponent(
            id=verifier_id,
            name=_text(raw["name"], f"verifiers.{verifier_id}.name"),
            model_id=_identifier(raw["model_id"], f"verifiers.{verifier_id}.model_id"),
            harness_id=_identifier(raw["harness_id"], f"verifiers.{verifier_id}.harness_id"),
            inference_edge_id=_identifier(
                raw["inference_edge_id"], f"verifiers.{verifier_id}.inference_edge_id"
            ),
            status=_status(raw["status"], f"verifiers.{verifier_id}.status"),
            evidence_ids=_evidence_ids(
                raw["evidence_ids"], f"verifiers.{verifier_id}.evidence_ids", evidence
            ),
        )
    return output


def _parse_routes(
    values: Any,
    evidence: dict[str, EvidenceRecord],
) -> dict[str, RouteDefinition]:
    if not isinstance(values, list) or not values:
        raise RoutingManifestError("routes must be a non-empty array")
    output: dict[str, RouteDefinition] = {}
    keys = {
        "id",
        "name",
        "qualification_status",
        "composition",
        "model_ids",
        "harness_id",
        "verifier_id",
        "inference_edge_id",
        "execution_edge_id",
        "roles",
        "capabilities",
        "languages",
        "tool_needs",
        "max_context_tokens",
        "priority",
        "required_runtime_tags",
        "evidence_ids",
        "baseline_route_id",
        "team_gain_basis_points",
        "comparison_evidence_ids",
    }
    for index, item in enumerate(values):
        raw = _object(item, f"routes[{index}]")
        _exact_keys(raw, keys, f"routes[{index}]")
        route_id = _identifier(raw["id"], f"routes[{index}].id")
        if route_id in output:
            raise RoutingManifestError(f"duplicate route id: {route_id}")
        composition = raw["composition"]
        if composition not in {"single", "team"}:
            raise RoutingManifestError(f"routes.{route_id}.composition is invalid")
        verifier_id = raw["verifier_id"]
        if verifier_id is not None:
            verifier_id = _identifier(verifier_id, f"routes.{route_id}.verifier_id")
        baseline_route_id = raw["baseline_route_id"]
        if baseline_route_id is not None:
            baseline_route_id = _identifier(
                baseline_route_id, f"routes.{route_id}.baseline_route_id"
            )
        gain = raw["team_gain_basis_points"]
        if gain is not None and (
            not isinstance(gain, int) or isinstance(gain, bool) or not -10_000 <= gain <= 10_000
        ):
            raise RoutingManifestError(
                f"routes.{route_id}.team_gain_basis_points must be null or -10000..10000"
            )
        priority = raw["priority"]
        if (
            not isinstance(priority, int)
            or isinstance(priority, bool)
            or not 0 <= priority <= 10_000
        ):
            raise RoutingManifestError(f"routes.{route_id}.priority must be 0..10000")
        output[route_id] = RouteDefinition(
            id=route_id,
            name=_text(raw["name"], f"routes.{route_id}.name"),
            qualification_status=_status(
                raw["qualification_status"], f"routes.{route_id}.qualification_status"
            ),
            composition=composition,
            model_ids=_string_tuple(raw["model_ids"], f"routes.{route_id}.model_ids"),
            harness_id=_identifier(raw["harness_id"], f"routes.{route_id}.harness_id"),
            verifier_id=verifier_id,
            inference_edge_id=_identifier(
                raw["inference_edge_id"], f"routes.{route_id}.inference_edge_id"
            ),
            execution_edge_id=_identifier(
                raw["execution_edge_id"], f"routes.{route_id}.execution_edge_id"
            ),
            roles=_string_tuple(raw["roles"], f"routes.{route_id}.roles", allow_wildcard=True),
            capabilities=_string_tuple(
                raw["capabilities"],
                f"routes.{route_id}.capabilities",
                allow_wildcard=True,
            ),
            languages=_string_tuple(
                raw["languages"],
                f"routes.{route_id}.languages",
                allow_wildcard=True,
            ),
            tool_needs=_string_tuple(
                raw["tool_needs"], f"routes.{route_id}.tool_needs", allow_empty=True
            ),
            max_context_tokens=_optional_context_limit(
                raw["max_context_tokens"], f"routes.{route_id}.max_context_tokens"
            ),
            priority=priority,
            required_runtime_tags=_string_tuple(
                raw["required_runtime_tags"],
                f"routes.{route_id}.required_runtime_tags",
                allow_empty=True,
            ),
            evidence_ids=_evidence_ids(
                raw["evidence_ids"],
                f"routes.{route_id}.evidence_ids",
                evidence,
                allow_empty=raw["qualification_status"] == "not_run",
            ),
            baseline_route_id=baseline_route_id,
            team_gain_basis_points=gain,
            comparison_evidence_ids=_evidence_ids(
                raw["comparison_evidence_ids"],
                f"routes.{route_id}.comparison_evidence_ids",
                evidence,
                allow_empty=True,
            ),
        )
    return output


def _parse_policy(raw_value: Any) -> RoutingPolicy:
    raw = _object(raw_value, "policy")
    keys = {
        "cloud_primary_route_id",
        "sovereign_route_ids",
        "apple_execution_edge_id",
        "production_eligible_statuses",
        "automatic_retries",
    }
    _exact_keys(raw, keys, "policy")
    statuses = _string_tuple(
        raw["production_eligible_statuses"], "policy.production_eligible_statuses"
    )
    if set(statuses) != PRODUCTION_ELIGIBLE_STATUSES:
        raise RoutingManifestError("only accepted routes may be production eligible")
    if (
        not isinstance(raw["automatic_retries"], int)
        or isinstance(raw["automatic_retries"], bool)
        or raw["automatic_retries"] != AUTOMATIC_RETRIES
    ):
        raise RoutingManifestError("routing automatic_retries must be exactly zero")
    return RoutingPolicy(
        cloud_primary_route_id=_identifier(
            raw["cloud_primary_route_id"], "policy.cloud_primary_route_id"
        ),
        sovereign_route_ids=_string_tuple(raw["sovereign_route_ids"], "policy.sovereign_route_ids"),
        apple_execution_edge_id=_identifier(
            raw["apple_execution_edge_id"], "policy.apple_execution_edge_id"
        ),
        production_eligible_statuses=statuses,
        automatic_retries=AUTOMATIC_RETRIES,
    )


def _supports(values: tuple[str, ...], requested: str) -> bool:
    return "*" in values or requested in values


def _route_is_local(
    route: RouteDefinition,
    *,
    models: Mapping[str, ModelComponent],
    harnesses: Mapping[str, HarnessComponent],
    verifiers: Mapping[str, VerifierComponent],
    edges: Mapping[str, EdgeComponent],
) -> bool:
    localities = [models[item].locality for item in route.model_ids]
    localities.extend(
        [
            harnesses[route.harness_id].locality,
            edges[route.inference_edge_id].locality,
            edges[route.execution_edge_id].locality,
        ]
    )
    if route.verifier_id is not None:
        verifier = verifiers[route.verifier_id]
        localities.extend(
            [
                models[verifier.model_id].locality,
                harnesses[verifier.harness_id].locality,
                edges[verifier.inference_edge_id].locality,
            ]
        )
    return all(item == "local" for item in localities)


def _validate_cross_references(manifest: RoutingManifest) -> None:
    for verifier in manifest.verifiers.values():
        if verifier.model_id not in manifest.models:
            raise RoutingManifestError(
                f"verifier {verifier.id} references unknown model {verifier.model_id}"
            )
        if verifier.harness_id not in manifest.harnesses:
            raise RoutingManifestError(
                f"verifier {verifier.id} references unknown harness {verifier.harness_id}"
            )
        edge = manifest.edges.get(verifier.inference_edge_id)
        if edge is None:
            raise RoutingManifestError(
                f"verifier {verifier.id} references unknown edge {verifier.inference_edge_id}"
            )
        if edge.kind not in {"inference", "combined"}:
            raise RoutingManifestError(f"verifier {verifier.id} requires an inference edge")
        verifier_model = manifest.models[verifier.model_id]
        if not _supports(verifier_model.roles, "verify"):
            raise RoutingManifestError(
                f"verifier {verifier.id} uses a model without the verify role"
            )
        if verifier.status == "accepted" and not verifier.evidence_ids:
            raise RoutingManifestError(f"accepted verifier {verifier.id} requires evidence")

    for route in manifest.routes.values():
        unknown_models = sorted(set(route.model_ids) - set(manifest.models))
        if unknown_models:
            raise RoutingManifestError(f"route {route.id} has unknown models: {unknown_models}")
        if route.harness_id not in manifest.harnesses:
            raise RoutingManifestError(
                f"route {route.id} references unknown harness {route.harness_id}"
            )
        if route.verifier_id is not None and route.verifier_id not in manifest.verifiers:
            raise RoutingManifestError(
                f"route {route.id} references unknown verifier {route.verifier_id}"
            )
        inference_edge = manifest.edges.get(route.inference_edge_id)
        execution_edge = manifest.edges.get(route.execution_edge_id)
        if inference_edge is None or execution_edge is None:
            raise RoutingManifestError(f"route {route.id} references an unknown edge")
        if inference_edge.kind not in {"inference", "combined"}:
            raise RoutingManifestError(f"route {route.id} requires an inference edge")
        if execution_edge.kind not in {"execution", "combined"}:
            raise RoutingManifestError(f"route {route.id} requires an execution edge")
        if route.composition == "single" and len(route.model_ids) != 1:
            raise RoutingManifestError(f"single route {route.id} must name exactly one model")
        if route.composition == "team" and len(route.model_ids) < 2:
            raise RoutingManifestError(f"team route {route.id} must name at least two models")
        if route.composition == "single" and (
            route.baseline_route_id is not None
            or route.team_gain_basis_points is not None
            or route.comparison_evidence_ids
        ):
            raise RoutingManifestError(f"single route {route.id} cannot declare team comparison")
        if "implement" in route.roles and route.qualification_status == "accepted":
            if route.verifier_id is None:
                raise RoutingManifestError(
                    f"accepted implementation route {route.id} requires a verifier"
                )
            if manifest.verifiers[route.verifier_id].status != "accepted":
                raise RoutingManifestError(
                    f"accepted route {route.id} requires an accepted verifier"
                )
        if route.qualification_status == "accepted" and not route.evidence_ids:
            raise RoutingManifestError(f"accepted route {route.id} requires evidence")
        harness = manifest.harnesses[route.harness_id]
        unsupported_tools = sorted(
            set(route.tool_needs) - (set(harness.tools) | set(execution_edge.capabilities))
        )
        if unsupported_tools:
            raise RoutingManifestError(
                f"route {route.id} has tools unsupported by its harness and execution edge: "
                f"{unsupported_tools}"
            )
        for model_id in route.model_ids:
            model = manifest.models[model_id]
            if not all(_supports(model.roles, role) for role in route.roles):
                raise RoutingManifestError(
                    f"route {route.id} claims a role unsupported by model {model_id}"
                )
            if not all(_supports(model.languages, language) for language in route.languages):
                raise RoutingManifestError(
                    f"route {route.id} claims a language unsupported by model {model_id}"
                )
            if (
                route.max_context_tokens is not None
                and model.max_context_tokens is not None
                and route.max_context_tokens > model.max_context_tokens
            ):
                raise RoutingManifestError(
                    f"route {route.id} exceeds model {model_id} context limit"
                )
        if "xcode" in route.tool_needs and (
            route.execution_edge_id != manifest.policy.apple_execution_edge_id
        ):
            raise RoutingManifestError(f"route {route.id} must send Xcode work to the Mac edge")

    for route in manifest.routes.values():
        if route.composition != "team":
            continue
        if route.baseline_route_id is None:
            raise RoutingManifestError(f"team route {route.id} requires a single baseline")
        baseline = manifest.routes.get(route.baseline_route_id)
        if baseline is None or baseline.composition != "single":
            raise RoutingManifestError(
                f"team route {route.id} baseline must be a known single route"
            )
        if route.qualification_status == "accepted":
            if baseline.qualification_status != "accepted":
                raise RoutingManifestError(
                    f"accepted team route {route.id} requires an accepted single baseline"
                )
            set_comparable_fields = (
                "roles",
                "capabilities",
                "languages",
                "tool_needs",
            )
            if (
                any(
                    frozenset(getattr(route, field)) != frozenset(getattr(baseline, field))
                    for field in set_comparable_fields
                )
                or route.max_context_tokens != baseline.max_context_tokens
            ):
                raise RoutingManifestError(
                    f"accepted team route {route.id} must match its single baseline task surface"
                )
            if not route.comparison_evidence_ids:
                raise RoutingManifestError(
                    f"accepted team route {route.id} requires comparative evidence"
                )
            if route.team_gain_basis_points is None or route.team_gain_basis_points <= 0:
                raise RoutingManifestError(
                    f"accepted team route {route.id} requires measured positive gain"
                )

    policy = manifest.policy
    primary = manifest.routes.get(policy.cloud_primary_route_id)
    if primary is None or primary.qualification_status != "accepted":
        raise RoutingManifestError("cloud primary route must exist and be accepted")
    if _route_is_local(
        primary,
        models=manifest.models,
        harnesses=manifest.harnesses,
        verifiers=manifest.verifiers,
        edges=manifest.edges,
    ):
        raise RoutingManifestError("cloud primary route must actually contain a cloud component")
    apple = manifest.edges.get(policy.apple_execution_edge_id)
    if apple is None or apple.locality != "local" or apple.kind not in {"execution", "combined"}:
        raise RoutingManifestError("apple_execution_edge_id must be a local execution edge")
    if "xcode" not in apple.capabilities:
        raise RoutingManifestError("Apple execution edge must advertise Xcode")
    for route_id in policy.sovereign_route_ids:
        route = manifest.routes.get(route_id)
        if route is None:
            raise RoutingManifestError(f"unknown sovereign route: {route_id}")
        if route.qualification_status != "accepted":
            raise RoutingManifestError(f"sovereign route {route_id} must be accepted")
        if not _route_is_local(
            route,
            models=manifest.models,
            harnesses=manifest.harnesses,
            verifiers=manifest.verifiers,
            edges=manifest.edges,
        ):
            raise RoutingManifestError(f"sovereign route {route_id} must have no cloud dependency")


def validate_routing_manifest(
    value: Any,
    *,
    evidence_root: Path | None = None,
    verify_evidence: bool = False,
) -> RoutingManifest:
    """Validate a decoded manifest and return an immutable normalized view."""

    raw = _json_snapshot(value, "routing manifest")
    _exact_keys(
        raw,
        {"schema_version", "manifest_id", "evidence", "components", "routes", "policy"},
        "routing manifest",
    )
    if (
        not isinstance(raw["schema_version"], int)
        or isinstance(raw["schema_version"], bool)
        or raw["schema_version"] != ROUTING_SCHEMA_VERSION
    ):
        raise RoutingManifestError("only routing schema_version 1 is supported")
    evidence = _parse_evidence(
        raw["evidence"], evidence_root=evidence_root, verify_evidence=verify_evidence
    )
    components = _object(raw["components"], "components")
    _exact_keys(components, {"models", "harnesses", "verifiers", "edges"}, "components")
    models = _parse_models(components["models"], evidence)
    harnesses = _parse_harnesses(components["harnesses"], evidence)
    edges = _parse_edges(components["edges"], evidence)
    verifiers = _parse_verifiers(components["verifiers"], evidence)
    routes = _parse_routes(raw["routes"], evidence)
    policy = _parse_policy(raw["policy"])
    manifest = RoutingManifest(
        schema_version=ROUTING_SCHEMA_VERSION,
        manifest_id=_identifier(raw["manifest_id"], "manifest_id"),
        evidence=MappingProxyType(dict(evidence)),
        models=MappingProxyType(dict(models)),
        harnesses=MappingProxyType(dict(harnesses)),
        verifiers=MappingProxyType(dict(verifiers)),
        edges=MappingProxyType(dict(edges)),
        routes=MappingProxyType(dict(routes)),
        policy=policy,
        sha256=_sha256(raw),
    )
    _validate_cross_references(manifest)
    return manifest


def load_routing_manifest(
    path: Path | str,
    *,
    evidence_root: Path | None = None,
    verify_evidence: bool = True,
) -> RoutingManifest:
    """Load a manifest and, by default, verify every cited local artifact hash."""

    manifest_path = Path(path).expanduser().resolve()
    try:
        if manifest_path.stat().st_size > 2 * 1024 * 1024:
            raise RoutingManifestError("routing manifest exceeds 2 MiB")
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except RoutingManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RoutingManifestError(f"cannot load routing manifest: {error}") from error
    root = evidence_root
    if root is None and verify_evidence:
        if manifest_path.parent.name != "config":
            raise RoutingManifestError(
                "default evidence root requires the manifest to live under config/"
            )
        root = manifest_path.parent.parent
    return validate_routing_manifest(raw, evidence_root=root, verify_evidence=verify_evidence)


def _runtime_references_are_known(manifest: RoutingManifest, runtime: RuntimeState) -> None:
    if not isinstance(runtime.cloud_available, bool):
        raise RoutingRequestError("runtime cloud_available must be boolean")
    unknown_models = sorted(runtime.operable_model_ids - set(manifest.models))
    unknown_harnesses = sorted(runtime.operable_harness_ids - set(manifest.harnesses))
    unknown_edges = sorted(runtime.available_edge_ids - set(manifest.edges))
    if unknown_models or unknown_harnesses or unknown_edges:
        raise RoutingRequestError(
            "runtime snapshot references unknown components: "
            f"models={unknown_models}, harnesses={unknown_harnesses}, edges={unknown_edges}"
        )
    for item in runtime.satisfied_runtime_tags:
        if not isinstance(item, str) or not _ID_RE.fullmatch(item):
            raise RoutingRequestError("runtime tags must be stable identifiers")


def _route_has_cloud_dependency(manifest: RoutingManifest, route: RouteDefinition) -> bool:
    return not _route_is_local(
        route,
        models=manifest.models,
        harnesses=manifest.harnesses,
        verifiers=manifest.verifiers,
        edges=manifest.edges,
    )


def _consider_route(
    manifest: RoutingManifest,
    request: RoutingRequest,
    runtime: RuntimeState,
    route: RouteDefinition,
) -> RouteConsideration:
    reasons: list[str] = []
    if (
        request.mode == "production"
        and route.qualification_status not in PRODUCTION_ELIGIBLE_STATUSES
    ):
        reasons.append(f"status_{route.qualification_status}")
    if request.mode == "qualification" and request.candidate_route_id != route.id:
        reasons.append("not_requested_candidate")
    if _route_has_cloud_dependency(manifest, route) and not runtime.cloud_available:
        reasons.append("cloud_unavailable")
    missing_models = sorted(set(route.model_ids) - runtime.operable_model_ids)
    if missing_models:
        reasons.extend(f"model_unavailable:{item}" for item in missing_models)
    if route.harness_id not in runtime.operable_harness_ids:
        reasons.append(f"harness_unavailable:{route.harness_id}")
    if route.inference_edge_id not in runtime.available_edge_ids:
        reasons.append(f"inference_edge_unavailable:{route.inference_edge_id}")
    if route.execution_edge_id not in runtime.available_edge_ids:
        reasons.append(f"execution_edge_unavailable:{route.execution_edge_id}")
    if route.verifier_id is not None:
        verifier = manifest.verifiers[route.verifier_id]
        if verifier.model_id not in runtime.operable_model_ids:
            reasons.append(f"verifier_model_unavailable:{verifier.model_id}")
        if verifier.harness_id not in runtime.operable_harness_ids:
            reasons.append(f"verifier_harness_unavailable:{verifier.harness_id}")
        if verifier.inference_edge_id not in runtime.available_edge_ids:
            reasons.append(f"verifier_edge_unavailable:{verifier.inference_edge_id}")
    missing_tags = sorted(set(route.required_runtime_tags) - runtime.satisfied_runtime_tags)
    reasons.extend(f"runtime_tag_missing:{item}" for item in missing_tags)
    if not _supports(route.roles, request.role):
        reasons.append("role_unsupported")
    if not _supports(route.capabilities, request.capability):
        reasons.append("capability_unsupported")
    if not _supports(route.languages, request.language):
        reasons.append("language_unsupported")
    if route.max_context_tokens is not None and request.context_tokens > route.max_context_tokens:
        reasons.append("context_exceeds_route_limit")
    missing_tools = sorted(set(request.tool_needs) - set(route.tool_needs))
    if missing_tools:
        reasons.extend(f"tool_unsupported:{item}" for item in missing_tools)
    return RouteConsideration(
        route_id=route.id,
        qualification_status=route.qualification_status,
        eligible=not reasons,
        reason_codes=tuple(reasons),
    )


def _select_non_primary(
    manifest: RoutingManifest,
    eligible: list[RouteDefinition],
) -> RouteDefinition | None:
    singles = sorted(
        (route for route in eligible if route.composition == "single"),
        key=lambda route: (route.priority, route.id),
    )
    if not singles:
        return None
    best_single = singles[0]
    proven_teams = [
        route
        for route in eligible
        if route.composition == "team"
        and route.baseline_route_id == best_single.id
        and route.team_gain_basis_points is not None
        and route.team_gain_basis_points > 0
        and route.comparison_evidence_ids
    ]
    if not proven_teams:
        return best_single
    return sorted(
        proven_teams,
        key=lambda route: (-int(route.team_gain_basis_points or 0), route.priority, route.id),
    )[0]


def select_route(
    manifest: RoutingManifest,
    request: RoutingRequest,
    runtime: RuntimeState,
) -> RoutingDecision:
    """Return one deterministic route decision and complete fallback telemetry."""

    _runtime_references_are_known(manifest, runtime)
    if request.mode == "qualification" and request.candidate_route_id not in manifest.routes:
        raise RoutingRequestError(f"unknown candidate route: {request.candidate_route_id}")
    considerations = tuple(
        _consider_route(manifest, request, runtime, manifest.routes[route_id])
        for route_id in sorted(manifest.routes)
    )
    by_id = {item.route_id: item for item in considerations}
    eligible = [manifest.routes[item.route_id] for item in considerations if item.eligible]
    selected: RouteDefinition | None
    primary = manifest.routes[manifest.policy.cloud_primary_route_id]
    if request.mode == "qualification":
        selected = manifest.routes[request.candidate_route_id] if eligible else None
        if selected is not None and selected.id not in {item.id for item in eligible}:
            selected = None
    elif by_id[primary.id].eligible:
        selected = primary
    else:
        selected = _select_non_primary(manifest, eligible)

    fallback_from: str | None = None
    fallback_code: str | None = None
    fallback_details: tuple[str, ...] = ()
    if request.mode == "production" and selected is not None and selected.id != primary.id:
        fallback_from = primary.id
        fallback_details = by_id[primary.id].reason_codes
        fallback_code = (
            "cloud_unavailable"
            if "cloud_unavailable" in fallback_details
            else "cloud_primary_incompatible"
        )
    elif selected is None:
        if request.mode == "qualification":
            fallback_code = "qualification_candidate_unavailable"
            fallback_details = by_id[request.candidate_route_id].reason_codes
        else:
            fallback_from = primary.id
            fallback_code = "no_accepted_compatible_route"
            fallback_details = by_id[primary.id].reason_codes

    evidence_ids: tuple[str, ...] = ()
    if selected is not None:
        collected = set(selected.evidence_ids)
        for model_id in selected.model_ids:
            collected.update(manifest.models[model_id].evidence_ids)
        collected.update(manifest.harnesses[selected.harness_id].evidence_ids)
        collected.update(manifest.edges[selected.inference_edge_id].evidence_ids)
        collected.update(manifest.edges[selected.execution_edge_id].evidence_ids)
        if selected.verifier_id is not None:
            verifier = manifest.verifiers[selected.verifier_id]
            collected.update(verifier.evidence_ids)
            collected.update(manifest.models[verifier.model_id].evidence_ids)
            collected.update(manifest.harnesses[verifier.harness_id].evidence_ids)
            collected.update(manifest.edges[verifier.inference_edge_id].evidence_ids)
        collected.update(selected.comparison_evidence_ids)
        evidence_ids = tuple(sorted(collected))
    evidence_hashes = {item: manifest.evidence[item].sha256 for item in evidence_ids}
    return RoutingDecision(
        status="routed" if selected is not None else "unroutable",
        selected_route_id=selected.id if selected is not None else None,
        model_ids=selected.model_ids if selected is not None else (),
        harness_id=selected.harness_id if selected is not None else None,
        verifier_id=selected.verifier_id if selected is not None else None,
        inference_edge_id=selected.inference_edge_id if selected is not None else None,
        execution_edge_id=selected.execution_edge_id if selected is not None else None,
        fallback_from_route_id=fallback_from,
        fallback_reason_code=fallback_code,
        fallback_detail_codes=fallback_details,
        evidence_ids=evidence_ids,
        evidence_sha256=MappingProxyType(dict(evidence_hashes)),
        considered=considerations,
        request_sha256=_sha256(request.canonical()),
        runtime_sha256=_sha256(runtime.canonical()),
        manifest_sha256=manifest.sha256,
    )


def adjudicate_qualification_status(
    current_status: str,
    observation_status: str,
    *,
    evaluator_valid: bool,
    applicable: bool,
    evidence_ids: tuple[str, ...],
) -> str:
    """Apply one adjudicated outcome without turning invalid evidence into demotion.

    This helper does not persist anything.  It exists so callers cannot collapse
    evaluator failure, runtime failure, or insufficient evidence into rejection.
    A valid applicable acceptance or rejection must carry evidence IDs.
    """

    for label, value in (
        ("current_status", current_status),
        ("observation_status", observation_status),
    ):
        if value not in OUTCOME_STATUSES:
            raise RoutingRequestError(f"{label} is invalid")
    if not isinstance(evaluator_valid, bool) or not isinstance(applicable, bool):
        raise RoutingRequestError("evaluator_valid and applicable must be boolean")
    if not isinstance(evidence_ids, tuple) or len(evidence_ids) != len(set(evidence_ids)):
        raise RoutingRequestError("evidence_ids must be a duplicate-free tuple")
    if observation_status in {"evaluator_invalid", "runtime_blocked", "inconclusive", "not_run"}:
        return current_status
    if not evaluator_valid or not applicable:
        raise RoutingRequestError(
            "accepted/rejected observations require a valid applicable evaluator"
        )
    if not evidence_ids or any(
        not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in evidence_ids
    ):
        raise RoutingRequestError("accepted/rejected observations require evidence IDs")
    return observation_status
