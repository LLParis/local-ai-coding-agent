"""Frozen deterministic Memory v1 qualification runner.

The runner uses only synthetic data beneath a newly created temporary root.  It
never calls a model or a network service.  Model-assisted answer synthesis is a
separate, explicitly not-run phase.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parents[2]
SOURCE = REPOSITORY / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from agent_continuity.compaction import (  # noqa: E402
    TASK_STATE_SCHEMA,
    FrozenSourceRange,
    task_state_sha256,
    validate_compaction_proposal,
    validate_state_chain,
    validate_task_state,
)
from agent_continuity.memory_store import (  # noqa: E402
    CapabilityError,
    MemoryScope,
    MemoryStore,
)
from agent_continuity.run_memory import RunMemoryError, ensure_secret_free  # noqa: E402
from agent_continuity.working_set import (  # noqa: E402
    MAX_MEMORIES,
    MEMORY_TOKEN_BUDGET,
    PER_MEMORY_TOKEN_CAP,
    STATE_TOKEN_BUDGET,
    OfflineWhitespaceTokenCounter,
    QueryInput,
    pack_working_set,
)

MANIFEST_SCHEMA = "coding-intelligence-memory-eval-suite/v1"
CASE_RESULT_SCHEMA = "coding-intelligence-memory-eval-case-result/v1"
CONTROL_SCHEMA = "coding-intelligence-memory-evaluator-control/v1"
REPORT_SCHEMA = "coding-intelligence-memory-eval-report/v1"
LOCK_SCHEMA = "coding-intelligence-memory-eval-lock/v1"
MANIFEST_PATH = ROOT / "manifest.json"
LOCK_PATH = ROOT / "suite.lock.json"

TASK_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "22222222-2222-2222-2222-222222222222"
HOST_ID = "memory-eval-host"
ACTOR = {"kind": "system", "id": "memory-v1-evaluator"}
EVENT_NAMESPACE = uuid.UUID("7f54b0df-6e91-4c41-905f-45105ed04cc7")
CRASH_EXIT_CODE = 73
ARTIFACT_BYTES = b"memory-v1-artifact\n"
ARTIFACT_SHA256 = "sha256:" + hashlib.sha256(ARTIFACT_BYTES).hexdigest()

_CASE_KEYS = {
    "id",
    "tranche",
    "description",
    "seed_events",
    "query",
    "expected_memory_ids",
    "forbidden_memory_ids",
    "expected_state",
    "invariants",
    "evidence_requirements",
    "context_budget",
    "expected_answer_facts",
    "action",
    "model_assisted_phase_required",
    "negative_mutation",
}
_QUERY_KEYS = {"text", "scopes", "as_of", "kinds", "top_k"}
_SCOPE_KEYS = {"owner_id", "workspace_id", "task_id", "agent_role", "session_id"}
_CONTEXT_BUDGET = {
    "state_tokens": STATE_TOKEN_BUDGET,
    "memory_tokens": MEMORY_TOKEN_BUDGET,
    "per_memory_tokens": PER_MEMORY_TOKEN_CAP,
    "max_memories": MAX_MEMORIES,
}
_TERMINAL_OUTCOMES = {
    "accepted",
    "rejected",
    "inconclusive",
    "evaluator_invalid",
    "runtime_blocked",
}
_MUTATIONS = {
    "drop_recall",
    "include_forbidden_scope",
    "temporal_wrong",
    "remove_provenance",
    "fabricate_negative",
    "omit_compaction_invariant",
    "duplicate_side_effect",
    "retention_claim_without_execution",
}


class SuiteError(ValueError):
    """The frozen evaluator or one of its declared fixtures is invalid."""


@dataclass(frozen=True)
class Check:
    check_id: str
    passed: bool
    expected: Any
    observed: Any
    critical: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.check_id,
            "passed": self.passed,
            "critical": self.critical,
            "expected": self.expected,
            "observed": self.observed,
        }


@dataclass
class SeedContext:
    records: dict[str, dict[str, Any]]
    memory_ids: set[str]
    rejected_memory_ids: set[str]
    support_event_id: str | None = None
    support_evidence_id: str | None = None
    previous_state: dict[str, Any] | None = None


class StoreResolver:
    """Bind compaction citations to the fresh store and scoped artifacts."""

    def __init__(self, store: MemoryStore):
        self.store = store

    def event_record_sha256(self, event_id: str) -> str | None:
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT record_sha256 FROM event_index WHERE event_id = ?", (event_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    def evidence_exists(self, evidence_id: str) -> bool:
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
        return row is not None

    def artifact_sha256(self, path: str) -> str | None:
        candidate = (self.store.root / path).resolve()
        try:
            candidate.relative_to(self.store.root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    """Stable UTF-8 JSON with one LF; volatile telemetry is not special-cased here."""

    try:
        return (
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SuiteError(f"value is not canonical JSON: {error}") from error


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SuiteError(f"cannot load {path}: {error}") from error


def load_manifest() -> dict[str, Any]:
    """Load and fully validate the frozen forty-case declaration."""

    manifest = _load_json(MANIFEST_PATH)
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise SuiteError("unsupported memory evaluation manifest")
    tranches = manifest.get("tranches")
    cases = manifest.get("cases")
    if not isinstance(tranches, list) or len(tranches) != 8 or len(set(tranches)) != 8:
        raise SuiteError("manifest must freeze exactly eight unique tranches")
    if not isinstance(cases, list) or len(cases) != 40 or manifest.get("case_count") != 40:
        raise SuiteError("manifest must freeze exactly forty cases")
    counts = Counter(case.get("tranche") for case in cases if isinstance(case, dict))
    if counts != Counter({tranche: 5 for tranche in tranches}):
        raise SuiteError("each memory tranche must contain exactly five cases")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(case_id, str) or not case_id for case_id in ids):
        raise SuiteError("every case needs a non-empty id")
    if len(ids) != len(set(ids)):
        raise SuiteError("memory case ids must be unique")

    for case in cases:
        if set(case) != _CASE_KEYS:
            raise SuiteError(
                f"case {case.get('id')} fields differ: "
                f"missing={sorted(_CASE_KEYS - set(case))}, "
                f"unknown={sorted(set(case) - _CASE_KEYS)}"
            )
        if not isinstance(case["description"], str) or not case["description"].strip():
            raise SuiteError(f"case {case['id']} has no description")
        if not isinstance(case["seed_events"], list) or not case["seed_events"]:
            raise SuiteError(f"case {case['id']} has no seed events")
        if not any(seed.get("type") == "state/committed" for seed in case["seed_events"]):
            raise SuiteError(f"case {case['id']} has no declared state seed event")
        query = case["query"]
        if not isinstance(query, dict) or set(query) != _QUERY_KEYS:
            raise SuiteError(f"case {case['id']} query fields differ")
        if not isinstance(query["text"], str) or not query["text"].strip():
            raise SuiteError(f"case {case['id']} query text is empty")
        if not isinstance(query["scopes"], list) or not query["scopes"]:
            raise SuiteError(f"case {case['id']} query scopes are empty")
        for scope in query["scopes"]:
            if not isinstance(scope, dict) or set(scope) != _SCOPE_KEYS:
                raise SuiteError(f"case {case['id']} has a non-exact query scope")
            MemoryScope.from_value(scope)
        if query["top_k"] != 10:
            raise SuiteError(f"case {case['id']} must freeze top_k=10")
        if set(case["context_budget"]) != set(_CONTEXT_BUDGET) or (
            case["context_budget"] != _CONTEXT_BUDGET
        ):
            raise SuiteError(f"case {case['id']} changed the fixed context budget")
        if set(case["expected_state"]) != {"profile", "revision"}:
            raise SuiteError(f"case {case['id']} expected_state must name profile and revision")
        profile = case["expected_state"]["profile"]
        if profile not in manifest["state_profiles"]:
            raise SuiteError(f"case {case['id']} names an unknown state profile")
        if not isinstance(case["invariants"], list) or not case["invariants"]:
            raise SuiteError(f"case {case['id']} has no state invariants")
        for field in (
            "expected_memory_ids",
            "forbidden_memory_ids",
            "expected_answer_facts",
        ):
            if not isinstance(case[field], list):
                raise SuiteError(f"case {case['id']} {field} must be a list")
        if set(case["expected_memory_ids"]) & set(case["forbidden_memory_ids"]):
            raise SuiteError(f"case {case['id']} overlaps expected and forbidden ids")
        evidence = case["evidence_requirements"]
        if set(evidence) != {
            "minimum_rows_per_expected_memory",
            "required_source_kinds",
            "required_authorities",
            "require_source_event_ids",
        }:
            raise SuiteError(f"case {case['id']} evidence requirements differ")
        if not isinstance(case["model_assisted_phase_required"], bool):
            raise SuiteError(f"case {case['id']} model phase flag is not boolean")
        if case["negative_mutation"] not in _MUTATIONS:
            raise SuiteError(f"case {case['id']} names an unsupported mutation")
        action = case["action"]
        if not isinstance(action, dict) or action.get("kind") not in {
            "retrieve_and_pack",
            "temporal",
            "secret_admission",
            "compaction",
            "crash_replay",
        }:
            raise SuiteError(f"case {case['id']} action is unsupported")

    policy = manifest.get("model_assisted_policy")
    if policy != {
        "trials": 3,
        "model_calls_per_trial": 1,
        "automatic_retries": 0,
        "status": "not_run",
    }:
        raise SuiteError("model-assisted policy must remain frozen and not-run")
    return manifest


def manifest_sha256() -> str:
    return _digest(canonical_bytes(load_manifest()))


def verify_suite_lock() -> dict[str, Any]:
    value = _load_json(LOCK_PATH)
    expected = {
        "schema": LOCK_SCHEMA,
        "manifest_sha256": manifest_sha256(),
        "case_count": 40,
    }
    if value != expected:
        raise SuiteError(f"suite lock mismatch: expected {expected}, observed {value}")
    return value


def _event_id(case_id: str, key: str) -> str:
    return str(uuid.uuid5(EVENT_NAMESPACE, f"{case_id}:{key}"))


def _case_by_id(manifest: Mapping[str, Any], case_id: str) -> dict[str, Any]:
    for case in manifest["cases"]:
        if case["id"] == case_id:
            return case
    raise SuiteError(f"unknown memory evaluation case: {case_id}")


def _scope(seed: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any]:
    value = seed.get("scope", case["query"]["scopes"][0])
    if not isinstance(value, dict) or set(value) != _SCOPE_KEYS:
        raise SuiteError(f"seed {seed.get('key')} has an invalid exact scope")
    MemoryScope.from_value(value)
    return dict(value)


def _evidence_rows(seed: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = seed.get("evidence")
    rows = raw if isinstance(raw, list) else [raw]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise SuiteError(f"memory seed {seed.get('key')} must declare evidence")
    normalized: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.setdefault("excerpt", seed["claim"]["searchable_text"])
        normalized.append(item)
    return normalized


def _memory_payload(seed: Mapping[str, Any]) -> dict[str, Any]:
    claim = seed.get("claim")
    if not isinstance(claim, dict) or set(claim) != {
        "kind",
        "subject",
        "predicate",
        "object",
        "searchable_text",
    }:
        raise SuiteError(f"memory seed {seed.get('key')} has an invalid claim")
    payload: dict[str, Any] = {
        "memory_id": seed["memory_id"],
        **claim,
        "verification_status": seed.get("verification_status", "tested"),
        "evidence": _evidence_rows(seed),
    }
    for field in ("status", "valid_from", "valid_to", "observed_at", "supersedes_id"):
        if seed.get(field) is not None:
            payload[field] = seed[field]
    padding = seed.get("padding_bytes", 0)
    if padding:
        if not isinstance(padding, int) or padding < 1 or padding > 16_384:
            raise SuiteError("padding_bytes must be between 1 and 16384")
        payload["frozen_padding"] = "x" * padding
    return payload


def _profile_state(
    manifest: Mapping[str, Any],
    profile_name: str,
    *,
    event_id: str,
    support_event_id: str,
    support_evidence_id: str,
    previous_state_sha256: str | None,
) -> dict[str, Any]:
    profile = manifest["state_profiles"][profile_name]
    criteria = [
        {
            "id": item["id"],
            "text": f"Criterion {item['id']} must remain truthful.",
            "status": item["status"],
            "evidence_ids": [support_evidence_id] if item["status"] == "met" else [],
        }
        for item in profile["criteria"]
    ]
    completed = [
        {
            "id": item_id,
            "text": f"Completed {item_id} with frozen evidence.",
            "evidence_ids": [support_evidence_id],
            "completed_at": "2026-08-20T00:00:00Z",
        }
        for item_id in profile["completed"]
    ]
    blockers = [
        {
            "id": item["id"],
            "text": f"Blocker {item['id']} remains tracked.",
            "status": item["status"],
            "evidence_ids": [support_evidence_id],
        }
        for item in profile["blockers"]
    ]
    state = {
        "schema": TASK_STATE_SCHEMA,
        "task_id": TASK_ID,
        "objective": {
            "text": profile["objective_text"],
            "source_event_id": support_event_id,
        },
        "success_criteria": criteria,
        "constraints": [
            {
                "id": item_id,
                "text": f"Constraint {item_id} remains active.",
                "status": "active",
                "source_event_id": support_event_id,
            }
            for item_id in profile["constraints"]
        ],
        "decisions": [
            {
                "id": item_id,
                "text": f"Decision {item_id} remains recorded.",
                "rationale": "The frozen case requires this decision.",
                "source_event_ids": [support_event_id],
            }
            for item_id in profile["decisions"]
        ],
        "completed": completed,
        "current_action": {
            "text": profile["current_action"],
            "owner": "deterministic-evaluator",
            "started_at": "2026-08-20T00:00:00Z",
        },
        "next_actions": [
            {
                "id": item["id"],
                "order": item["order"],
                "text": f"Execute {item['id']} without losing state.",
                "depends_on": list(item["depends_on"]),
            }
            for item in profile["next_actions"]
        ],
        "blockers": blockers,
        "open_tool_calls": copy.deepcopy(profile["open_tool_calls"]),
        "artifacts": [
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "evidence_id": support_evidence_id,
            }
            for item in profile["artifacts"]
        ],
        "disputes": [
            {
                "id": item_id,
                "text": f"Dispute {item_id} remains unresolved.",
                "status": "unresolved",
                "evidence_ids": [support_evidence_id],
            }
            for item_id in profile["disputes"]
        ],
        "through_event_id": event_id,
        "previous_state_sha256": previous_state_sha256,
    }
    return validate_task_state(state)


def _append_kwargs(
    case: Mapping[str, Any], seed: Mapping[str, Any], event_id: str
) -> dict[str, Any]:
    scope = _scope(seed, case)
    task_id = scope["task_id"] or TASK_ID
    session_id = scope["session_id"] or SESSION_ID
    return {
        "host_id": HOST_ID,
        "session_id": session_id,
        "task_id": task_id,
        "event_type": seed["type"],
        "actor": ACTOR,
        "scope": scope,
        "event_id": event_id,
        "occurred_at": seed.get("occurred_at", "2026-08-20T00:00:00Z"),
        "retention_class": seed.get("retention_class", "core"),
    }


def _record_synthetic_effect(store: MemoryStore, payload: Mapping[str, Any]) -> None:
    """Perform one temp-root-only side effect before the crash failpoint."""

    effect_id = payload.get("effect_id")
    if not isinstance(effect_id, str) or not effect_id:
        raise SuiteError("synthetic side effect requires a non-empty effect_id")
    if not _safe_identifier(effect_id):
        raise SuiteError("synthetic effect_id is not a safe filename identifier")
    path = store.root / "effects" / f"{effect_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_bytes({"effect_id": effect_id, "attempts": 1})
    try:
        with path.open("xb", buffering=0) as stream:
            if stream.write(encoded) != len(encoded):
                raise SuiteError("short synthetic side-effect write")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise SuiteError(f"duplicate synthetic side effect: {effect_id}") from error
    store._sync_directory(path.parent)


def _safe_identifier(value: str) -> bool:
    return bool(value) and len(value) <= 128 and all(
        character.isalnum() or character in "._-" for character in value
    )


def _crash_during_projection(store: MemoryStore, kwargs: Mapping[str, Any]) -> None:
    """Exit the worker after the final JSONL fsync and before projection."""

    original = store._apply_event
    calls = 0

    def injected(*args: Any, **named: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            os._exit(CRASH_EXIT_CODE)
        return original(*args, **named)

    store._apply_event = injected  # type: ignore[method-assign]
    store.append(**kwargs)
    os._exit(CRASH_EXIT_CODE + 1)


def _seed_case(
    store: MemoryStore,
    manifest: Mapping[str, Any],
    case: Mapping[str, Any],
    *,
    crash_worker: bool = False,
) -> SeedContext:
    context = SeedContext(records={}, memory_ids=set(), rejected_memory_ids=set())
    for index, seed in enumerate(case["seed_events"], start=1):
        event_id = _event_id(case["id"], seed["key"])
        kwargs = _append_kwargs(case, seed, event_id)
        if "occurred_at" not in seed:
            kwargs["occurred_at"] = f"2026-08-20T00:00:{index:02d}Z"
        event_type = seed["type"]
        if event_type == "memory/committed":
            payload = _memory_payload(seed)
            try:
                ensure_secret_free(payload)
            except RunMemoryError:
                if seed.get("expect_admission") != "rejected_secret":
                    raise
                context.rejected_memory_ids.add(seed["memory_id"])
                continue
            if seed.get("expect_admission") == "rejected_secret":
                raise SuiteError("secret control was not rejected by the admission guard")
            kwargs["payload"] = payload
        elif event_type in {"memory/superseded", "memory/retired", "memory/disputed"}:
            kwargs["payload"] = {"memory_id": seed["memory_id"]}
            if seed.get("valid_to") is not None:
                kwargs["payload"]["valid_to"] = seed["valid_to"]
        elif event_type == "state/committed":
            if context.support_event_id is None or context.support_evidence_id is None:
                raise SuiteError("state seed requires an earlier admitted memory and evidence")
            state = _profile_state(
                manifest,
                seed["profile"],
                event_id=event_id,
                support_event_id=context.support_event_id,
                support_evidence_id=context.support_evidence_id,
                previous_state_sha256=(
                    None
                    if context.previous_state is None
                    else task_state_sha256(context.previous_state)
                ),
            )
            context.previous_state = state
            kwargs["task_id"] = TASK_ID
            if kwargs["scope"]["task_id"] not in {None, TASK_ID}:
                kwargs["scope"] = {
                    "owner_id": kwargs["scope"]["owner_id"],
                    "workspace_id": kwargs["scope"]["workspace_id"],
                    "task_id": None,
                    "agent_role": kwargs["scope"]["agent_role"],
                    "session_id": None,
                }
                kwargs["session_id"] = SESSION_ID
            kwargs["payload"] = {"revision": seed["revision"], "state": state}
        else:
            payload = copy.deepcopy(seed.get("payload", {}))
            padding = seed.get("padding_bytes", 0)
            if padding:
                payload["frozen_padding"] = "x" * padding
            kwargs["payload"] = payload
            if event_type == "tool/side_effect_observed":
                _record_synthetic_effect(store, payload)

        if seed.get("crash_after_durable_append"):
            if not crash_worker:
                raise SuiteError("crash seed may run only in the dedicated child process")
            _crash_during_projection(store, kwargs)
        record = store.append(**kwargs)
        context.records[seed["key"]] = record
        if event_type == "memory/committed":
            context.memory_ids.add(seed["memory_id"])
            if context.support_event_id is None:
                context.support_event_id = record["event_id"]
                evidence = _evidence_rows(seed)
                context.support_evidence_id = evidence[0]["evidence_id"]
    if crash_worker:
        raise SuiteError("crash case reached the end without its declared failpoint")
    return context


def _write_artifacts(store_root: Path, manifest: Mapping[str, Any]) -> None:
    paths = {
        item["path"]
        for profile in manifest["state_profiles"].values()
        for item in profile["artifacts"]
    }
    for relative in sorted(paths):
        path = (store_root / relative).resolve()
        try:
            path.relative_to(store_root.resolve())
        except ValueError as error:
            raise SuiteError(f"artifact escapes the fresh store: {relative}") from error
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ARTIFACT_BYTES)
        if _digest(path.read_bytes()) != ARTIFACT_SHA256:
            raise SuiteError(f"artifact digest mismatch: {relative}")


def _raw_event_count(root: Path) -> int:
    return sum(
        len(path.read_bytes().splitlines()) for path in sorted((root / "events").glob("*/*.jsonl"))
    )


def _projected_event_count(root: Path) -> int:
    database = root / "memory.sqlite3"
    if not database.exists():
        return 0
    with closing(sqlite3.connect(database)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM event_index").fetchone()[0])


def _projection_snapshot(store: MemoryStore) -> dict[str, Any]:
    tables = {
        "event_index": "event_id",
        "memory_item": "memory_id",
        "evidence": "evidence_id",
        "task_state": "task_id",
        "blob_catalog": "blob_sha256",
        "blob_reference": "event_id",
        "blob_citation": "citation_event_id, source_event_id",
        "task_terminal": "task_id",
    }
    snapshot: dict[str, Any] = {}
    with closing(store._connect()) as connection:
        for table, order in tables.items():
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            values = [dict(row) for row in rows]
            if table == "event_index":
                for value in values:
                    value["segment_path"] = Path(value["segment_path"]).as_posix()
            snapshot[table] = values
        snapshot["fts_counts"] = {
            "unicode61": connection.execute(
                "SELECT COUNT(*) FROM memory_fts_unicode"
            ).fetchone()[0],
            "trigram": connection.execute(
                "SELECT COUNT(*) FROM memory_fts_trigram"
            ).fetchone()[0],
        }
    return snapshot


def _store_fingerprint(root: Path) -> str:
    entries: list[dict[str, Any]] = []
    for parent in (
        root / "events",
        root / "blobs",
        root / "archive",
        root / "retention",
        root / "effects",
    ):
        if not parent.exists():
            continue
        for path in sorted(item for item in parent.rglob("*") if item.is_file()):
            raw = path.read_bytes()
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size_bytes": len(raw),
                    "sha256": _digest(raw),
                }
            )
    return _digest(canonical_bytes(entries))


def _effect_snapshot(root: Path) -> list[dict[str, Any]]:
    effects: list[dict[str, Any]] = []
    directory = root / "effects"
    if not directory.exists():
        return effects
    for path in sorted(directory.glob("*.json")):
        value = _load_json(path)
        if not isinstance(value, dict) or set(value) != {"effect_id", "attempts"}:
            raise SuiteError(f"malformed synthetic effect journal: {path.name}")
        effects.append(value)
    return effects


def _run_crash_worker(case_id: str, root: Path) -> dict[str, Any]:
    environment = os.environ.copy()
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(SOURCE) if not current else str(SOURCE) + os.pathsep + current
    )
    process = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--crash-worker",
            case_id,
            "--store-root",
            str(root),
        ],
        cwd=REPOSITORY,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return {
        "fresh_process": True,
        "expected_exit_code": CRASH_EXIT_CODE,
        "observed_exit_code": process.returncode,
        "stdout_empty": not process.stdout,
        "stderr_empty": not process.stderr,
    }


def _state_from_projection(store: MemoryStore) -> tuple[int, dict[str, Any]]:
    with closing(store._connect()) as connection:
        row = connection.execute(
            "SELECT revision, state_json FROM task_state WHERE task_id = ?", (TASK_ID,)
        ).fetchone()
    if row is None:
        raise SuiteError("fresh store has no projected task state")
    return int(row["revision"]), validate_task_state(json.loads(row["state_json"]))


def _state_snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "objective_text": state["objective"]["text"],
        "active_constraint_ids": sorted(
            item["id"] for item in state["constraints"] if item["status"] == "active"
        ),
        "open_criterion_ids": sorted(
            item["id"] for item in state["success_criteria"] if item["status"] == "open"
        ),
        "met_criterion_ids": sorted(
            item["id"] for item in state["success_criteria"] if item["status"] == "met"
        ),
        "active_blocker_ids": sorted(
            item["id"] for item in state["blockers"] if item["status"] == "active"
        ),
        "unknown_tool_calls": sorted(
            (dict(item) for item in state["open_tool_calls"]),
            key=lambda item: item["call_id"],
        ),
        "next_actions": sorted(
            (
                {
                    "id": item["id"],
                    "order": item["order"],
                    "depends_on": sorted(item["depends_on"]),
                }
                for item in state["next_actions"]
            ),
            key=lambda item: item["order"],
        ),
        "artifact_hashes": sorted(
            ({"path": item["path"], "sha256": item["sha256"]} for item in state["artifacts"]),
            key=lambda item: item["path"],
        ),
        "unresolved_dispute_ids": sorted(
            item["id"] for item in state["disputes"] if item["status"] == "unresolved"
        ),
        "completed_ids": sorted(item["id"] for item in state["completed"]),
    }


def _expected_state_snapshot(manifest: Mapping[str, Any], profile_name: str) -> dict[str, Any]:
    profile = manifest["state_profiles"][profile_name]
    return {
        "objective_text": profile["objective_text"],
        "active_constraint_ids": sorted(profile["constraints"]),
        "open_criterion_ids": sorted(
            item["id"] for item in profile["criteria"] if item["status"] == "open"
        ),
        "met_criterion_ids": sorted(
            item["id"] for item in profile["criteria"] if item["status"] == "met"
        ),
        "active_blocker_ids": sorted(
            item["id"] for item in profile["blockers"] if item["status"] == "active"
        ),
        "unknown_tool_calls": sorted(
            (dict(item) for item in profile["open_tool_calls"]),
            key=lambda item: item["call_id"],
        ),
        "next_actions": sorted(
            (
                {
                    "id": item["id"],
                    "order": item["order"],
                    "depends_on": sorted(item["depends_on"]),
                }
                for item in profile["next_actions"]
            ),
            key=lambda item: item["order"],
        ),
        "artifact_hashes": sorted(
            ({"path": item["path"], "sha256": item["sha256"]} for item in profile["artifacts"]),
            key=lambda item: item["path"],
        ),
        "unresolved_dispute_ids": sorted(profile["disputes"]),
        "completed_ids": sorted(profile["completed"]),
    }


def _query_scopes(
    case: Mapping[str, Any], mutation: str | None
) -> list[dict[str, Any]]:
    scopes = copy.deepcopy(case["query"]["scopes"])
    if mutation != "include_forbidden_scope":
        return scopes
    forbidden = set(case["forbidden_memory_ids"])
    for seed in case["seed_events"]:
        if seed.get("memory_id") in forbidden:
            candidate = _scope(seed, case)
            if candidate not in scopes:
                scopes.append(candidate)
    return scopes


def _retrieval(
    store: MemoryStore,
    case: Mapping[str, Any],
    *,
    mutation: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = case["query"]
    scopes = _query_scopes(case, mutation)
    started = time.perf_counter_ns()
    results = store.search(
        query["text"],
        scopes,
        as_of=query["as_of"],
        kinds=query["kinds"],
        top_k=query["top_k"],
    )
    elapsed_ns = time.perf_counter_ns() - started
    channels = {"exact": False, "word": False, "trigram": False}
    for result in results:
        for item in result["retrieval"]["channels"]:
            channels[item["channel"]] = True
    telemetry = {
        "corpus_size": _memory_count(store),
        "search_elapsed_ns": elapsed_ns,
        "production_threshold_ns": None,
        "channels": {
            channel: {
                "available": True,
                "participated": channels[channel],
                "individual_latency": "not_exposed_by_memory_v1",
            }
            for channel in ("exact", "word", "trigram")
        },
    }
    return results, telemetry


def _memory_count(store: MemoryStore) -> int:
    with closing(store._connect()) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM memory_item").fetchone()[0])


def _run_temporal_probes(
    store: MemoryStore,
    case: Mapping[str, Any],
    mutation: str | None,
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for probe in case["action"].get("probes", []):
        rows = store.search(
            case["query"]["text"],
            case["query"]["scopes"],
            as_of=probe["as_of"],
            kinds=case["query"]["kinds"],
            top_k=case["query"]["top_k"],
        )
        observed = [row["memory_id"] for row in rows]
        if mutation == "temporal_wrong":
            observed = list(probe["forbidden_memory_ids"] or ["fabricated-temporal"])
        probes.append(
            {
                "id": probe["id"],
                "as_of": probe["as_of"],
                "expected_memory_ids": list(probe["expected_memory_ids"]),
                "forbidden_memory_ids": list(probe["forbidden_memory_ids"]),
                "observed_memory_ids": observed,
                "passed": (
                    set(probe["expected_memory_ids"]).issubset(observed)
                    and not set(probe["forbidden_memory_ids"]) & set(observed)
                ),
            }
        )
    return probes


def _pack(
    store: MemoryStore,
    case: Mapping[str, Any],
    mutation: str | None,
) -> dict[str, Any]:
    if case["query"]["as_of"] is not None:
        return {
            "status": "not_applicable_to_explicit_as_of_search",
            "included_memory_ids": [],
            "state_tokens": None,
            "memory_tokens": None,
            "within_budget": True,
            "forbidden_surface": [],
        }
    working = pack_working_set(
        store,
        TASK_ID,
        QueryInput(request=case["query"]["text"]),
        _query_scopes(case, mutation),
        OfflineWhitespaceTokenCounter(),
    )
    candidate_ids = {
        item["memory_id"]
        for item in working.retrieval_audit["memory"]["candidates"]
    }
    forbidden = [
        memory_id
        for memory_id in case["forbidden_memory_ids"]
        if memory_id in working.included_memory_ids or memory_id in candidate_ids
    ]
    candidates = working.retrieval_audit["memory"]["candidates"]
    within = (
        working.state_tokens <= STATE_TOKEN_BUDGET
        and working.memory_tokens <= MEMORY_TOKEN_BUDGET
        and len(working.included_memory_ids) <= MAX_MEMORIES
        and all(
            item["candidate_tokens"] is None
            or item["candidate_tokens"] <= PER_MEMORY_TOKEN_CAP
            or item["decision"] == "excluded"
            for item in candidates
        )
    )
    return {
        "status": "packed",
        "included_memory_ids": list(working.included_memory_ids),
        "state_tokens": working.state_tokens,
        "memory_tokens": working.memory_tokens,
        "within_budget": within,
        "forbidden_surface": forbidden,
        "audit_has_all_candidates": all("decision" in item for item in candidates),
    }


def _provenance(
    store: MemoryStore,
    case: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    mutation: str | None,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    requirements = case["evidence_requirements"]
    by_id = {row["memory_id"]: row for row in rows}
    memory_checks: dict[str, bool] = {}
    with closing(store._connect()) as connection:
        for memory_id in case["expected_memory_ids"]:
            row = by_id.get(memory_id)
            if row is None:
                memory_checks[memory_id] = False
                continue
            evidence = row["evidence"]
            source_kinds = {item["source_kind"] for item in evidence}
            authorities = {item["authority"] for item in evidence}
            source_ids = [item["source_event_id"] for item in evidence]
            sources_exist = all(
                source_id is not None
                and connection.execute(
                    "SELECT 1 FROM event_index WHERE event_id = ?", (source_id,)
                ).fetchone()
                is not None
                for source_id in source_ids
            )
            memory_checks[memory_id] = (
                len(evidence) >= requirements["minimum_rows_per_expected_memory"]
                and set(requirements["required_source_kinds"]).issubset(source_kinds)
                and set(requirements["required_authorities"]).issubset(authorities)
                and (
                    not requirements["require_source_event_ids"]
                    or (bool(source_ids) and all(source_ids) and sources_exist)
                )
            )
        completion_evidence = [
            evidence_id
            for item in state["completed"]
            for evidence_id in item["evidence_ids"]
        ]
        completion_ok = all(
            connection.execute(
                "SELECT 1 FROM evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
            is not None
            for evidence_id in completion_evidence
        )
    if mutation == "remove_provenance":
        if memory_checks:
            memory_checks[next(iter(memory_checks))] = False
        else:
            completion_ok = False
    return {
        "memory_coverage": memory_checks,
        "completed_claim_coverage": completion_ok,
        "passed": all(memory_checks.values()) and completion_ok,
    }


def _answer_fact_checks(
    store: MemoryStore, case: Mapping[str, Any]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for expected in case["expected_answer_facts"]:
        memory_id = expected.get("memory_id")
        if memory_id is None:
            checks.append({"expected": expected, "observed": expected, "passed": True})
            continue
        memory = store.get(memory_id)
        observed = None if memory is None else memory["object"]
        checks.append(
            {
                "memory_id": memory_id,
                "expected": expected["value"],
                "observed": observed,
                "passed": observed == expected["value"],
            }
        )
    return checks


def _run_compaction(
    store: MemoryStore,
    case: Mapping[str, Any],
    state: dict[str, Any],
    mutation: str | None,
) -> dict[str, Any]:
    resolver = StoreResolver(store)
    states = [copy.deepcopy(state)]
    reports: list[dict[str, Any]] = []
    previous = states[0]
    for round_index in range(1, case["action"]["rounds"] + 1):
        event_id = _event_id(case["id"], f"compaction-source-{round_index}")
        record = store.append(
            host_id=HOST_ID,
            session_id=SESSION_ID,
            task_id=TASK_ID,
            event_type="compaction/source",
            actor=ACTOR,
            scope=case["query"]["scopes"][0],
            event_id=event_id,
            occurred_at=f"2026-08-20T01:00:{round_index:02d}Z",
            payload={"round": round_index},
        )
        candidate = copy.deepcopy(previous)
        candidate["through_event_id"] = event_id
        candidate["previous_state_sha256"] = task_state_sha256(previous)
        if mutation == "omit_compaction_invariant" and round_index == 1:
            candidate["constraints"] = candidate["constraints"][1:]
        frozen = FrozenSourceRange(
            first_event_id=event_id,
            first_record_sha256=record["record_sha256"],
            last_event_id=event_id,
            last_record_sha256=record["record_sha256"],
            retained_tail_event_id=event_id,
        )
        result = validate_compaction_proposal(previous, candidate, frozen, resolver)
        reports.append(result.as_dict())
        if not result.accepted:
            break
        states.append(candidate)
        previous = candidate
    chain_valid = False
    try:
        validate_state_chain(states)
        chain_valid = True
    except ValueError:
        chain_valid = False
    return {
        "rounds_required": case["action"]["rounds"],
        "rounds_accepted": sum(report["accepted"] for report in reports),
        "reports": reports,
        "state_chain_valid": chain_valid,
        "semantic_verifier_status": "pending",
        "final_state": previous,
        "passed": (
            len(reports) == case["action"]["rounds"]
            and all(report["accepted"] for report in reports)
            and chain_valid
        ),
    }


def _retention_observation(
    store: MemoryStore, case: Mapping[str, Any], mutation: str | None
) -> dict[str, Any]:
    retention = case["action"].get("retention")
    if retention is None:
        return {
            "required": False,
            "capability_available": True,
            "operation_status": "not_required",
            "truthful": True,
            "passed": True,
            "runtime_blocked": False,
            "catalog": _retention_catalog(store),
        }
    capability = callable(getattr(store, "plan_retention", None)) and callable(
        getattr(store, "run_gc", None)
    )
    if not capability:
        return {
            "required": True,
            "required_operation": retention["required_operation"],
            "capability_available": False,
            "operation_status": "not_run_missing_implementation",
            "truthful": True,
            "passed": False,
            "runtime_blocked": False,
            "catalog": _retention_catalog(store),
        }

    scope = case["query"]["scopes"][0]
    if case["id"] == "retention-05-eligible-gc":
        store.append(
            host_id=HOST_ID,
            session_id=SESSION_ID,
            task_id=TASK_ID,
            event_type="retention/cited",
            actor=ACTOR,
            scope=scope,
            event_id=_event_id(case["id"], "retention-citation"),
            occurred_at="2026-06-05T00:30:00Z",
            payload={"source_event_ids": [_event_id(case["id"], "bulk")]},
        )
    store.append(
        host_id=HOST_ID,
        session_id=SESSION_ID,
        task_id=TASK_ID,
        event_type="task/completed",
        actor=ACTOR,
        scope=scope,
        event_id=_event_id(case["id"], "retention-terminal"),
        occurred_at="2026-06-06T00:00:00Z",
        payload={"status": "terminal-retention-fixture"},
    )
    try:
        plan = store.plan_retention(now="2026-08-20T00:00:00Z")
        dry_run = store.run_gc(plan)
        execution: dict[str, Any] | None = None
        if mutation is None:
            execution = store.run_gc(plan, apply=True)
    except CapabilityError as error:
        return {
            "required": True,
            "required_operation": retention["required_operation"],
            "capability_available": False,
            "operation_status": "runtime_blocked",
            "truthful": True,
            "passed": False,
            "runtime_blocked": True,
            "error": str(error),
            "catalog": _retention_catalog(store),
        }

    source_decisions = {
        source_event_id: next(
            (
                item
                for item in plan.decisions
                if source_event_id in item.source_event_ids
            ),
            None,
        )
        for source_event_id in (
            _event_id(case["id"], "memory"),
            _event_id(case["id"], "bulk"),
            _event_id(case["id"], "scratch"),
        )
    }
    passed = False
    if case["id"] == "retention-04-protected-gc":
        protected = source_decisions[_event_id(case["id"], "memory")]
        passed = bool(
            protected is not None
            and protected.action == "keep"
            and protected.pin_reasons
            and _retention_catalog_by_digest(store, protected.blob_sha256)["availability"]
            == "hot"
        )
    elif case["id"] == "retention-05-eligible-gc":
        bulk = source_decisions[_event_id(case["id"], "bulk")]
        scratch = source_decisions[_event_id(case["id"], "scratch")]
        passed = bool(
            bulk is not None
            and scratch is not None
            and bulk.action == "archive"
            and scratch.action == "evict"
            and execution is not None
            and execution["actions_applied"] >= 2
            and _retention_catalog_by_digest(store, bulk.blob_sha256)["availability"]
            == "archived"
            and _retention_catalog_by_digest(store, scratch.blob_sha256)["availability"]
            == "evicted"
            and store._read_tombstone(bulk.blob_sha256)["source_event_ids"]
            == list(bulk.source_event_ids)
            and store._read_tombstone(scratch.blob_sha256)["source_event_ids"]
            == list(scratch.source_event_ids)
        )
    if mutation == "retention_claim_without_execution":
        passed = False
    return {
        "required": True,
        "required_operation": retention["required_operation"],
        "capability_available": True,
        "operation_status": (
            "claimed_executed_without_apply"
            if mutation == "retention_claim_without_execution"
            else "executed"
        ),
        "truthful": mutation != "retention_claim_without_execution",
        "passed": passed,
        "runtime_blocked": False,
        "dry_run": dry_run,
        "execution": execution,
        "plan_sha256": plan.plan_sha256,
        "decisions": [item.as_dict() for item in plan.decisions],
        "catalog": _retention_catalog(store),
    }


def _retention_catalog(store: MemoryStore) -> list[dict[str, Any]]:
    with closing(store._connect()) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM blob_catalog ORDER BY blob_sha256"
            ).fetchall()
        ]


def _retention_catalog_by_digest(store: MemoryStore, digest: str) -> dict[str, Any]:
    with closing(store._connect()) as connection:
        row = connection.execute(
            "SELECT * FROM blob_catalog WHERE blob_sha256 = ?", (digest,)
        ).fetchone()
    if row is None:
        raise SuiteError(f"retention evaluator lost blob catalog row: {digest}")
    return dict(row)


def _control_observation(
    case: Mapping[str, Any],
    *,
    retrieved_ids: Sequence[str],
    state_snapshot: Mapping[str, Any],
    provenance_ok: bool,
    temporal_ok: bool,
    compaction_ok: bool,
    retention: Mapping[str, Any],
    duplicate_effects: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema": CONTROL_SCHEMA,
        "case_id": case["id"],
        "candidate_output_valid": True,
        "retrieved_memory_ids": list(retrieved_ids),
        "state_snapshot": copy.deepcopy(state_snapshot),
        "provenance_ok": provenance_ok,
        "temporal_ok": temporal_ok,
        "compaction_ok": compaction_ok,
        "retention": copy.deepcopy(retention),
        "duplicate_side_effect_recommendations": list(duplicate_effects),
    }


def _score_control(
    manifest: Mapping[str, Any], case: Mapping[str, Any], raw: Any
) -> dict[str, Any]:
    required = {
        "schema",
        "case_id",
        "candidate_output_valid",
        "retrieved_memory_ids",
        "state_snapshot",
        "provenance_ok",
        "temporal_ok",
        "compaction_ok",
        "retention",
        "duplicate_side_effect_recommendations",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        return {
            "terminal_outcome": "evaluator_invalid",
            "passed": False,
            "reason": "malformed_control_observation",
        }
    if raw["schema"] != CONTROL_SCHEMA or raw["case_id"] != case["id"]:
        return {
            "terminal_outcome": "evaluator_invalid",
            "passed": False,
            "reason": "control_identity_mismatch",
        }
    expected = set(case["expected_memory_ids"])
    forbidden = set(case["forbidden_memory_ids"])
    seeded = {
        seed["memory_id"]
        for seed in case["seed_events"]
        if seed.get("type") == "memory/committed"
        and seed.get("expect_admission") != "rejected_secret"
    }
    observed = set(raw["retrieved_memory_ids"])
    expected_state = _expected_state_snapshot(manifest, case["expected_state"]["profile"])
    retention = raw["retention"]
    checks = {
        "candidate_output_contract": raw["candidate_output_valid"] is True,
        "recall": expected.issubset(observed),
        "forbidden_exclusion": not forbidden & observed,
        "no_fabrication": observed.issubset(seeded),
        "state": raw["state_snapshot"] == expected_state,
        "provenance": raw["provenance_ok"] is True,
        "temporal": raw["temporal_ok"] is True,
        "compaction": raw["compaction_ok"] is True,
        "duplicate_effects": raw["duplicate_side_effect_recommendations"] == [],
        "retention_truth": retention.get("truthful") is True,
        "retention_execution": (
            not retention.get("required")
            or (
                retention.get("capability_available") is True
                and retention.get("operation_status") == "executed"
                and retention.get("passed") is True
            )
        ),
    }
    base_checks = all(
        passed for name, passed in checks.items() if name != "retention_execution"
    )
    if base_checks:
        if retention.get("required") and retention.get("runtime_blocked"):
            outcome = "runtime_blocked"
        elif retention.get("required") and not retention.get("capability_available"):
            outcome = "inconclusive"
        elif not checks["retention_execution"]:
            outcome = "rejected"
        else:
            outcome = "accepted"
    else:
        outcome = "rejected"
    return {
        "terminal_outcome": outcome,
        "passed": outcome == "accepted",
        "reason": None,
        "checks": checks,
    }


def _mutate_control(
    case: Mapping[str, Any], observation: Mapping[str, Any], mutation: str
) -> dict[str, Any]:
    value = copy.deepcopy(observation)
    expected = case["expected_memory_ids"]
    forbidden = case["forbidden_memory_ids"]
    if mutation == "drop_recall":
        value["retrieved_memory_ids"] = [
            item for item in value["retrieved_memory_ids"] if item != expected[0]
        ]
    elif mutation == "include_forbidden_scope":
        value["retrieved_memory_ids"].append(forbidden[0])
    elif mutation == "temporal_wrong":
        value["temporal_ok"] = False
    elif mutation == "remove_provenance":
        value["provenance_ok"] = False
    elif mutation == "fabricate_negative":
        value["retrieved_memory_ids"].append("fabricated-memory")
    elif mutation == "omit_compaction_invariant":
        value["state_snapshot"]["active_constraint_ids"] = []
        value["compaction_ok"] = False
    elif mutation == "duplicate_side_effect":
        value["duplicate_side_effect_recommendations"].append("effect-copy-1")
    elif mutation == "retention_claim_without_execution":
        value["retention"]["operation_status"] = "executed"
        value["retention"]["truthful"] = False
    else:  # pragma: no cover - load_manifest owns this branch
        raise SuiteError(f"unsupported control mutation: {mutation}")
    return value


def _evaluator_controls(
    manifest: Mapping[str, Any], case: Mapping[str, Any], golden: Mapping[str, Any]
) -> dict[str, Any]:
    positive = _score_control(manifest, case, copy.deepcopy(golden))
    equivalent = copy.deepcopy(golden)
    equivalent["retrieved_memory_ids"] = list(reversed(equivalent["retrieved_memory_ids"]))
    equivalent["state_snapshot"] = {
        key: equivalent["state_snapshot"][key]
        for key in reversed(list(equivalent["state_snapshot"]))
    }
    semantic = _score_control(manifest, case, equivalent)
    negative = _score_control(
        manifest,
        case,
        _mutate_control(case, golden, case["negative_mutation"]),
    )
    malformed = copy.deepcopy(golden)
    malformed["candidate_output_valid"] = False
    malformed_result = _score_control(manifest, case, malformed)
    invalid_record = copy.deepcopy(golden)
    invalid_record.pop("retrieved_memory_ids")
    invalid_result = _score_control(manifest, case, invalid_record)
    independent_mutation = copy.deepcopy(golden)
    independent_mutation["retrieved_memory_ids"].append("fabricated-control-memory")
    mutation = _score_control(manifest, case, independent_mutation)
    expected_positive = (
        "runtime_blocked"
        if golden["retention"].get("runtime_blocked")
        else (
            "inconclusive"
            if golden["retention"].get("required")
            and not golden["retention"].get("capability_available")
            else "accepted"
        )
    )
    controls = {
        "positive": {
            "expected": expected_positive,
            "observed": positive["terminal_outcome"],
        },
        "semantic_equivalence": {
            "expected": expected_positive,
            "observed": semantic["terminal_outcome"],
        },
        "negative": {"expected": "rejected", "observed": negative["terminal_outcome"]},
        "malformed": {
            "expected": "rejected",
            "observed": malformed_result["terminal_outcome"],
        },
        "evaluator_invalid": {
            "expected": "evaluator_invalid",
            "observed": invalid_result["terminal_outcome"],
        },
        "mutation": {"expected": "rejected", "observed": mutation["terminal_outcome"]},
    }
    return {
        "controls": controls,
        "passed": all(item["expected"] == item["observed"] for item in controls.values()),
        "invalid_controls_preserved": [
            {
                "control": "evaluator_invalid",
                "terminal_outcome": invalid_result["terminal_outcome"],
            }
        ],
        "invalid_controls_in_candidate_denominator": 0,
    }


def _check(checks: list[Check], check_id: str, expected: Any, observed: Any) -> None:
    checks.append(Check(check_id, expected == observed, expected, observed))


def _deterministic_fingerprint(result: Mapping[str, Any]) -> str:
    stable = {
        "schema": result["schema"],
        "suite_id": result["suite_id"],
        "case_id": result["case_id"],
        "tranche": result["tranche"],
        "terminal_outcome": result["terminal_outcome"],
        "retrieved_memory_ids": result["retrieval"]["observed_memory_ids"],
        "state": result["state"],
        "temporal_probes": result["temporal_probes"],
        "store_fingerprint": result["store_fingerprint"],
        "checks": result["checks"],
        "evaluator_controls": result["evaluator_controls"],
        "retention": result["retention"],
    }
    return _digest(canonical_bytes(stable))


def _run_case(
    manifest: Mapping[str, Any],
    case: Mapping[str, Any],
    *,
    mutation: str | None,
    include_controls: bool,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="memory-v1-eval-") as directory:
        root = Path(directory).resolve()
        crash = case["action"]["kind"] == "crash_replay"
        if not crash:
            _write_artifacts(root, manifest)
        crash_report: dict[str, Any] | None = None
        context: SeedContext | None = None
        if crash:
            crash_report = _run_crash_worker(case["id"], root)
            raw_before = _raw_event_count(root)
            projected_before = _projected_event_count(root)
            store = MemoryStore(root, blob_threshold=1024)
            projected_after = _projected_event_count(root)
            crash_report.update(
                {
                    "raw_events_before_reopen": raw_before,
                    "projected_events_before_reopen": projected_before,
                    "projected_events_after_reopen": projected_after,
                    "projection_gap_before_reopen": raw_before - projected_before,
                    "replayed_missing_events": projected_after - projected_before,
                    "synthetic_effects": _effect_snapshot(root),
                }
            )
            memory_ids = {
                seed["memory_id"]
                for seed in case["seed_events"]
                if seed.get("type") == "memory/committed"
                and seed.get("expect_admission") != "rejected_secret"
            }
            context = SeedContext(records={}, memory_ids=memory_ids, rejected_memory_ids=set())
        else:
            store = MemoryStore(root, blob_threshold=1024)
            context = _seed_case(store, manifest, case)

        retention = _retention_observation(store, case, mutation)
        verification = store.verify()
        before_rebuild = _projection_snapshot(store)
        rebuild = store.replay(rebuild=True)
        after_rebuild = _projection_snapshot(store)
        projection_stable = before_rebuild == after_rebuild

        revision, projected_state = _state_from_projection(store)
        action_state = projected_state
        compaction = {
            "required": False,
            "passed": True,
            "semantic_verifier_status": "not_required",
        }
        if case["action"]["kind"] == "compaction":
            compaction = _run_compaction(store, case, projected_state, mutation)
            action_state = compaction["final_state"]

        rows, telemetry = _retrieval(store, case, mutation=mutation)
        observed_ids = [row["memory_id"] for row in rows]
        if mutation == "drop_recall" and case["expected_memory_ids"]:
            observed_ids = [
                item for item in observed_ids if item != case["expected_memory_ids"][0]
            ]
        if mutation == "fabricate_negative":
            observed_ids.append("fabricated-memory")

        temporal_probes = _run_temporal_probes(store, case, mutation)
        packed = _pack(store, case, mutation)
        provenance = _provenance(store, case, rows, mutation, action_state)
        facts = _answer_fact_checks(store, case)
        duplicate_effects: list[str] = []
        if mutation == "duplicate_side_effect":
            duplicate_effects.append("effect-copy-1")

        all_seeded = set(context.memory_ids)
        fabricated = sorted(set(observed_ids) - all_seeded)
        forbidden_observed = sorted(
            set(case["forbidden_memory_ids"])
            & (set(observed_ids) | set(packed["forbidden_surface"]))
        )
        expected_state = _expected_state_snapshot(
            manifest, case["expected_state"]["profile"]
        )
        observed_state = _state_snapshot(action_state)
        temporal_ok = all(probe["passed"] for probe in temporal_probes)
        expected_hits = len(set(case["expected_memory_ids"]) & set(observed_ids))

        checks: list[Check] = []
        _check(checks, "canonical_chain", True, verification.event_count > 0)
        _check(checks, "projection_rebuild_stability", True, projection_stable)
        _check(checks, "expected_recall_at_10", len(case["expected_memory_ids"]), expected_hits)
        _check(checks, "forbidden_exclusion", [], forbidden_observed)
        _check(checks, "no_fabrication", [], fabricated)
        _check(checks, "state_revision", case["expected_state"]["revision"], revision)
        _check(checks, "state_invariants", expected_state, observed_state)
        _check(checks, "provenance", True, provenance["passed"])
        _check(checks, "answer_facts", True, all(item["passed"] for item in facts))
        _check(checks, "temporal_truth", True, temporal_ok)
        _check(checks, "working_set_budget", True, packed["within_budget"])
        _check(checks, "working_set_scope", [], packed["forbidden_surface"])
        _check(checks, "compaction", True, compaction["passed"])
        _check(checks, "duplicate_side_effects", [], duplicate_effects)
        _check(checks, "retention_telemetry_truth", True, retention["truthful"])
        _check(
            checks,
            "retention_execution",
            True,
            (not retention["required"]) or retention["passed"],
        )
        if crash_report is not None:
            _check(
                checks,
                "crash_worker_exit",
                CRASH_EXIT_CODE,
                crash_report["observed_exit_code"],
            )
            _check(
                checks,
                "fresh_process_projection_gap",
                case["action"]["expected_projection_gap"],
                crash_report["projection_gap_before_reopen"],
            )
            _check(
                checks,
                "fresh_process_replay",
                crash_report["raw_events_before_reopen"],
                crash_report["projected_events_after_reopen"],
            )
            effect_seeds = [
                seed
                for seed in case["seed_events"]
                if seed.get("type") == "tool/side_effect_observed"
            ]
            _check(
                checks,
                "synthetic_side_effect_count",
                len(effect_seeds),
                len(crash_report["synthetic_effects"]),
            )
            _check(
                checks,
                "synthetic_side_effect_exactly_once",
                [1] * len(effect_seeds),
                [effect["attempts"] for effect in crash_report["synthetic_effects"]],
            )

        golden = _control_observation(
            case,
            retrieved_ids=observed_ids,
            state_snapshot=observed_state,
            provenance_ok=provenance["passed"],
            temporal_ok=temporal_ok,
            compaction_ok=compaction["passed"],
            retention=retention,
            duplicate_effects=duplicate_effects,
        )
        controls = (
            _evaluator_controls(manifest, case, golden)
            if include_controls and mutation is None
            else {
                "controls": {},
                "passed": True,
                "invalid_controls_preserved": [],
                "invalid_controls_in_candidate_denominator": 0,
            }
        )
        deterministic_passed = all(check.passed for check in checks)
        if retention.get("runtime_blocked"):
            outcome = "runtime_blocked"
        elif not controls["passed"]:
            outcome = "evaluator_invalid"
        elif retention["required"] and not retention["capability_available"]:
            outcome = "inconclusive"
        elif not deterministic_passed:
            outcome = "rejected"
        else:
            outcome = "accepted"

        result: dict[str, Any] = {
            "schema": CASE_RESULT_SCHEMA,
            "suite_id": manifest["suite_id"],
            "manifest_sha256": manifest_sha256(),
            "case_id": case["id"],
            "tranche": case["tranche"],
            "terminal_outcome": outcome,
            "passed": outcome == "accepted",
            "deterministic_checks_passed": deterministic_passed,
            "checks": [check.as_dict() for check in checks],
            "retrieval": {
                "expected_memory_ids": list(case["expected_memory_ids"]),
                "forbidden_memory_ids": list(case["forbidden_memory_ids"]),
                "observed_memory_ids": observed_ids,
                "expected_hits": expected_hits,
                "expected_total": len(case["expected_memory_ids"]),
                "forbidden_observed": forbidden_observed,
                "fabricated_memory_ids": fabricated,
            },
            "state": {
                "expected_revision": case["expected_state"]["revision"],
                "observed_revision": revision,
                "expected": expected_state,
                "observed": observed_state,
                "invariants": list(case["invariants"]),
            },
            "provenance": provenance,
            "context": packed,
            "temporal_probes": temporal_probes,
            "compaction": {
                key: value for key, value in compaction.items() if key != "final_state"
            },
            "crash_replay": crash_report,
            "retention": retention,
            "duplicate_side_effect_recommendations": duplicate_effects,
            "telemetry": telemetry,
            "answer_fact_checks": facts,
            "canonical_verification": asdict(verification),
            "rebuild": asdict(rebuild),
            "store_fingerprint": _store_fingerprint(root),
            "evaluator_controls": controls,
            "model_assisted_phase": {
                "required": case["model_assisted_phase_required"],
                "status": (
                    "not_run" if case["model_assisted_phase_required"] else "not_required"
                ),
                "trials_required": (
                    manifest["model_assisted_policy"]["trials"]
                    if case["model_assisted_phase_required"]
                    else 0
                ),
                "model_calls_made": 0,
            },
            "mutation": mutation,
        }
        result["deterministic_fingerprint"] = _deterministic_fingerprint(result)
        return result


def run_case(
    case_id: str,
    *,
    mutation: str | None = None,
    include_controls: bool = True,
) -> dict[str, Any]:
    """Execute one frozen case in a fresh synthetic store."""

    manifest = load_manifest()
    if mutation is not None and mutation not in _MUTATIONS:
        raise SuiteError(f"unsupported execution mutation: {mutation}")
    case = _case_by_id(manifest, case_id)
    try:
        return _run_case(
            manifest,
            case,
            mutation=mutation,
            include_controls=include_controls,
        )
    except CapabilityError as error:
        return {
            "schema": CASE_RESULT_SCHEMA,
            "suite_id": manifest["suite_id"],
            "manifest_sha256": manifest_sha256(),
            "case_id": case_id,
            "tranche": case["tranche"],
            "terminal_outcome": "runtime_blocked",
            "passed": False,
            "deterministic_checks_passed": False,
            "error": str(error),
            "model_assisted_phase": {
                "required": case["model_assisted_phase_required"],
                "status": "not_run",
                "trials_required": 3 if case["model_assisted_phase_required"] else 0,
                "model_calls_made": 0,
            },
        }
    except SuiteError as error:
        return {
            "schema": CASE_RESULT_SCHEMA,
            "suite_id": manifest["suite_id"],
            "manifest_sha256": manifest_sha256(),
            "case_id": case_id,
            "tranche": case["tranche"],
            "terminal_outcome": "evaluator_invalid",
            "passed": False,
            "deterministic_checks_passed": False,
            "error": str(error),
            "model_assisted_phase": {
                "required": case["model_assisted_phase_required"],
                "status": "not_run",
                "trials_required": 3 if case["model_assisted_phase_required"] else 0,
                "model_calls_made": 0,
            },
        }


def _gate(name: str, passed: bool, expected: Any, observed: Any) -> dict[str, Any]:
    return {"id": name, "passed": passed, "expected": expected, "observed": observed}


def aggregate_results(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Recompute every hard gate from per-case evidence; never trust pass labels."""

    manifest = load_manifest()
    expected_ids = {case["id"] for case in manifest["cases"]}
    by_id: dict[str, Mapping[str, Any]] = {}
    duplicates: list[str] = []
    invalid_records: list[dict[str, Any]] = []
    for result in results:
        case_id = result.get("case_id")
        outcome = result.get("terminal_outcome")
        if case_id not in expected_ids or outcome not in _TERMINAL_OUTCOMES:
            invalid_records.append(
                {
                    "case_id": case_id,
                    "terminal_outcome": "evaluator_invalid",
                    "reason": "result_contract_or_identity_invalid",
                }
            )
            continue
        if case_id in by_id:
            duplicates.append(str(case_id))
            continue
        by_id[str(case_id)] = result
    missing = sorted(expected_ids - set(by_id))

    valid_measurements = [
        result
        for result in by_id.values()
        if result.get("terminal_outcome") not in {"evaluator_invalid", "runtime_blocked"}
        and isinstance(result.get("retrieval"), dict)
    ]
    exact = [result for result in valid_measurements if result["tranche"] == "exact_identifier"]
    exact_hits = sum(result["retrieval"]["expected_hits"] for result in exact)
    exact_total = sum(result["retrieval"]["expected_total"] for result in exact)
    overall_hits = sum(result["retrieval"]["expected_hits"] for result in valid_measurements)
    overall_total = sum(result["retrieval"]["expected_total"] for result in valid_measurements)
    scope_leaks = sum(
        len(result["retrieval"]["forbidden_observed"]) for result in valid_measurements
    )
    fabrications = sum(
        len(result["retrieval"]["fabricated_memory_ids"]) for result in valid_measurements
    )
    duplicate_effects = sum(
        len(result.get("duplicate_side_effect_recommendations", []))
        for result in valid_measurements
    )
    duplicate_effects += sum(
        max(0, int(effect.get("attempts", 0)) - 1)
        for result in valid_measurements
        for effect in (result.get("crash_replay") or {}).get("synthetic_effects", [])
    )
    evidence_losses = sum(
        0 if result.get("provenance", {}).get("passed") else 1
        for result in valid_measurements
    )
    temporal_errors = sum(
        sum(not probe["passed"] for probe in result.get("temporal_probes", []))
        for result in valid_measurements
    )
    state_losses = sum(
        result.get("state", {}).get("expected") != result.get("state", {}).get("observed")
        for result in valid_measurements
    )
    projection_errors = sum(
        not any(
            check.get("id") == "projection_rebuild_stability" and check.get("passed")
            for check in result.get("checks", [])
        )
        for result in valid_measurements
    )
    telemetry_errors = sum(
        not (
            isinstance(result.get("telemetry", {}).get("search_elapsed_ns"), int)
            and result["telemetry"]["search_elapsed_ns"] >= 0
            and set(result["telemetry"].get("channels", {}))
            == {"exact", "word", "trigram"}
            and all(
                channel.get("available") is True
                and channel.get("individual_latency") == "not_exposed_by_memory_v1"
                for channel in result["telemetry"]["channels"].values()
            )
        )
        for result in valid_measurements
    )
    control_errors = sum(
        not result.get("evaluator_controls", {}).get("passed", False)
        for result in valid_measurements
    )
    invalid_controls_in_denominator = sum(
        result.get("evaluator_controls", {}).get(
            "invalid_controls_in_candidate_denominator", 0
        )
        for result in valid_measurements
    )
    retention_pending = sorted(
        result["case_id"]
        for result in by_id.values()
        if result.get("retention", {}).get("required")
        and (
            not result["retention"].get("capability_available")
            or result["retention"].get("operation_status") != "executed"
            or result["retention"].get("passed") is not True
        )
    )
    retention_runtime_blocked = sorted(
        result["case_id"]
        for result in by_id.values()
        if result.get("retention", {}).get("runtime_blocked")
    )
    model_required = sorted(
        result["case_id"]
        for result in by_id.values()
        if result.get("model_assisted_phase", {}).get("required")
    )
    model_calls = sum(
        result.get("model_assisted_phase", {}).get("model_calls_made", 0)
        for result in by_id.values()
    )

    gates = {
        "all_40_cases_executed": _gate(
            "all_40_cases_executed",
            not missing and not duplicates and len(by_id) == 40,
            40,
            len(by_id),
        ),
        "canonical_chain_projection": _gate(
            "canonical_chain_projection", projection_errors == 0, 0, projection_errors
        ),
        "scope_leakage": _gate("scope_leakage", scope_leaks == 0, 0, scope_leaks),
        "fabricated_memory": _gate(
            "fabricated_memory", fabrications == 0, 0, fabrications
        ),
        "duplicate_side_effects": _gate(
            "duplicate_side_effects", duplicate_effects == 0, 0, duplicate_effects
        ),
        "evidence_loss": _gate("evidence_loss", evidence_losses == 0, 0, evidence_losses),
        "temporal_truth": _gate("temporal_truth", temporal_errors == 0, 0, temporal_errors),
        "state_invariants": _gate("state_invariants", state_losses == 0, 0, state_losses),
        "provenance_coverage": _gate(
            "provenance_coverage", evidence_losses == 0, "1.00", (
                "1.00"
                if not valid_measurements
                else f"{(len(valid_measurements) - evidence_losses) / len(valid_measurements):.2f}"
            )
        ),
        "exact_recall_at_10": _gate(
            "exact_recall_at_10",
            exact_total > 0 and exact_hits * 100 >= exact_total * 95,
            ">=0.95",
            f"{exact_hits}/{exact_total}",
        ),
        "overall_recall_at_10": _gate(
            "overall_recall_at_10",
            overall_total > 0 and overall_hits * 100 >= overall_total * 90,
            ">=0.90",
            f"{overall_hits}/{overall_total}",
        ),
        "truthful_channel_telemetry": _gate(
            "truthful_channel_telemetry", telemetry_errors == 0, 0, telemetry_errors
        ),
        "evaluator_controls": _gate(
            "evaluator_controls", control_errors == 0, 0, control_errors
        ),
        "invalid_controls_excluded": _gate(
            "invalid_controls_excluded",
            invalid_controls_in_denominator == 0,
            0,
            invalid_controls_in_denominator,
        ),
        "retention_execution": _gate(
            "retention_execution", not retention_pending, [], retention_pending
        ),
    }
    correctness_without_retention = all(
        gate["passed"]
        for name, gate in gates.items()
        if name != "retention_execution"
    )
    deterministic_status = (
        "accepted"
        if correctness_without_retention and not retention_pending
        else "runtime_blocked"
        if correctness_without_retention and retention_runtime_blocked
        else "inconclusive"
        if correctness_without_retention and retention_pending
        else "evaluator_invalid"
        if control_errors or invalid_records or duplicates
        else "rejected"
    )
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "suite_id": manifest["suite_id"],
        "manifest_sha256": manifest_sha256(),
        "deterministic_qualification": {
            "status": deterministic_status,
            "correctness_checks_passed": correctness_without_retention,
            "fully_qualified": deterministic_status == "accepted",
            "retention_capability_pending_cases": retention_pending,
        },
        "model_assisted_qualification": {
            "status": "not_run",
            "required_case_ids": model_required,
            "trials_per_case": manifest["model_assisted_policy"]["trials"],
            "model_calls_made": model_calls,
            "failures_remain_in_denominator": True,
        },
        "promotion_eligible": False,
        "promotion_blockers": [
            *(["deterministic_retention_execution_missing"] if retention_pending else []),
            *(["model_assisted_answer_synthesis_not_run"] if model_required else []),
            "scale_latency_matrix_not_run",
        ],
        "results": {
            "expected_cases": 40,
            "observed_cases": len(by_id),
            "missing_case_ids": missing,
            "duplicate_case_ids": sorted(duplicates),
            "terminal_outcomes": dict(
                sorted(Counter(result["terminal_outcome"] for result in by_id.values()).items())
            ),
            "invalid_records": invalid_records,
            "candidate_verdict_denominator": sum(
                result["terminal_outcome"] in {"accepted", "rejected"}
                for result in by_id.values()
            ),
            "excluded_from_candidate_verdict_denominator": sorted(
                result["case_id"]
                for result in by_id.values()
                if result["terminal_outcome"]
                in {"inconclusive", "evaluator_invalid", "runtime_blocked"}
            ),
        },
        "recall": {
            "exact_hits": exact_hits,
            "exact_total": exact_total,
            "overall_hits": overall_hits,
            "overall_total": overall_total,
        },
        "gates": gates,
        "latency_scale_phase": {
            "status": "not_run",
            "required_corpus_sizes": manifest["latency_corpus_sizes"],
            "production_threshold": None,
            "small_case_observations_recorded": len(valid_measurements),
        },
    }
    report["qualification_sha256"] = qualification_sha256(report)
    return report


def qualification_sha256(report: Mapping[str, Any]) -> str:
    stable = copy.deepcopy(dict(report))
    stable.pop("qualification_sha256", None)
    return _digest(canonical_bytes(stable))


def run_all() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    verify_suite_lock()
    manifest = load_manifest()
    results = [run_case(case["id"]) for case in manifest["cases"]]
    return results, aggregate_results(results)


def _crash_worker(case_id: str, root: Path) -> None:
    manifest = load_manifest()
    case = _case_by_id(manifest, case_id)
    if case["action"]["kind"] != "crash_replay":
        raise SuiteError("crash worker may execute only a crash_replay case")
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise SuiteError("crash worker requires an empty fresh store root")
    root.mkdir(parents=True, exist_ok=True)
    _write_artifacts(root, manifest)
    store = MemoryStore(root, blob_threshold=1024)
    _seed_case(store, manifest, case, crash_worker=True)


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--crash-worker")
    parser.add_argument("--store-root", type=Path)
    args = parser.parse_args(argv)
    if args.crash_worker:
        if args.store_root is None:
            parser.error("--crash-worker requires --store-root")
        _crash_worker(args.crash_worker, args.store_root)
        return 0
    verify_suite_lock()
    if args.case:
        payload: Any = run_case(args.case)
    else:
        results, report = run_all()
        payload = {"results": results, "report": report}
    encoded = canonical_bytes(payload)
    if args.output is None:
        sys.stdout.buffer.write(encoded)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
