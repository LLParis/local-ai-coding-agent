"""Production Memory retrieval and stable DeepSeek continuity identities."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .compaction import TASK_STATE_SCHEMA, task_state_sha256, validate_task_state
from .memory_store import MemoryScope, MemoryStore
from .run_memory import default_memory_root, ensure_secret_free, text_sha256
from .working_set import (
    LlamaCppTokenCounter,
    QueryInput,
    TokenCounter,
    WorkingSet,
    pack_working_set,
)

PRODUCTION_MEMORY_SCHEMA = "coding-intelligence-production-memory/v1"
_CONTINUITY_NAMESPACE = uuid.UUID("d78f7ae3-caf1-5f91-a31b-f36f03ce0b06")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEFAULT_TOKENIZER_URL = "http://127.0.0.1:8818/tokenize"


class ProductionMemoryError(RuntimeError):
    """The production worker cannot establish truthful bounded continuity."""


@dataclass(frozen=True)
class DeepSeekContinuity:
    """Stable identity and normal-user storage root for one workspace task stream."""

    task_key: str
    task_id: str
    session_family: str
    session_id: str
    home: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "task_key": self.task_key,
            "task_id": self.task_id,
            "session_family": self.session_family,
            "session_id": self.session_id,
            "home": str(self.home),
        }


class ProductionMemoryJournal:
    """Journal one run and retrieve prior evidence from its exact workspace scope."""

    def __init__(
        self,
        source: Path,
        objective: str,
        run_id: str,
        *,
        task_key: str = "default",
        harness: str = "deepseek",
        root: Path | None = None,
    ) -> None:
        self.source = Path(source).expanduser().resolve(strict=True)
        self.objective = _bounded_text(objective, "objective", 16_384)
        ensure_secret_free(self.objective)
        self.run_id = str(uuid.UUID(run_id))
        self.task_key = _task_key(task_key)
        if harness not in {"deepseek", "qwen-code"}:
            raise ProductionMemoryError("harness must be deepseek or qwen-code")
        self.harness = harness
        self.root = Path(root or default_memory_root()).expanduser().resolve()
        self.store = MemoryStore(self.root)
        self.host_id = "EXCALIBUR"
        self.workspace_id = text_sha256(os.path.normcase(str(self.source)))
        run_namespace = uuid.UUID(self.run_id)
        self.task_id = str(uuid.uuid5(run_namespace, "production-memory-task"))
        self.session_id = str(uuid.uuid5(run_namespace, "production-memory-session"))
        stable_task_id = str(
            uuid.uuid5(
                _CONTINUITY_NAMESPACE,
                f"{self.workspace_id}\n{self.task_key.casefold()}",
            )
        )
        self.stable_task_id = stable_task_id
        session_family = f"ci-{stable_task_id}"
        self.deepseek = (
            DeepSeekContinuity(
                task_key=self.task_key,
                task_id=stable_task_id,
                session_family=session_family,
                session_id=f"{session_family}-{self.run_id}",
                home=_deepseek_home(self.root),
            )
            if harness == "deepseek"
            else None
        )
        self.scope = MemoryScope(
            owner_id="local-owner",
            workspace_id=self.workspace_id,
            task_id=self.task_id,
            agent_role="implementer",
            session_id=self.session_id,
        )
        self.workspace_scope = MemoryScope(
            owner_id="local-owner",
            workspace_id=self.workspace_id,
        )
        self.parent: str | None = None
        self._events: dict[str, dict[str, Any]] = {}

        started_payload = {
            "schema": PRODUCTION_MEMORY_SCHEMA,
            "run_id": self.run_id,
            "objective_sha256": text_sha256(self.objective),
            "workspace_id": self.workspace_id,
            "task_key": self.task_key,
            "harness": self.harness,
        }
        if self.deepseek is not None:
            started_payload["deepseek_session_id"] = self.deepseek.session_id
        started = self.append("task/started", started_payload)
        state_event_id = str(uuid.uuid5(run_namespace, "production-memory-state-1"))
        self._state = _initial_state(
            task_id=self.task_id,
            objective=self.objective,
            source_event_id=str(started["event_id"]),
            through_event_id=state_event_id,
        )
        self.append(
            "state/committed",
            {"revision": 1, "state": self._state},
            event_id=state_event_id,
        )

    def append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        event_id: str | None = None,
        scope: MemoryScope | None = None,
    ) -> dict[str, Any]:
        event = self.store.append(
            host_id=self.host_id,
            session_id=self.session_id,
            task_id=self.task_id,
            event_type=event_type,
            actor={"kind": "agent", "id": "coding-intelligence-production"},
            scope=scope or self.scope,
            payload=payload,
            parent_event_id=self.parent,
            event_id=event_id,
            retention_class="core",
        )
        self.parent = str(event["event_id"])
        self._events[event_type] = event
        return event

    def working_set(
        self,
        token_counter: TokenCounter | None = None,
    ) -> WorkingSet:
        """Retrieve a compact, provenance-bearing workspace context for this run."""

        counter = token_counter or LlamaCppTokenCounter(_DEFAULT_TOKENIZER_URL)
        packed = pack_working_set(
            self.store,
            self.task_id,
            QueryInput(request=self.objective),
            (self.workspace_scope,),
            counter,
        )
        audit_text = json.dumps(
            packed.retrieval_audit,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.append(
            "memory/retrieved",
            {
                "schema": PRODUCTION_MEMORY_SCHEMA,
                "included_memory_ids": list(packed.included_memory_ids),
                "state_tokens": packed.state_tokens,
                "memory_tokens": packed.memory_tokens,
                "total_tokens": packed.total_tokens,
                "retrieval_audit_sha256": text_sha256(audit_text),
                "token_counter": packed.retrieval_audit["token_counter"],
            },
        )
        return packed

    def commit_episode(self, final: Mapping[str, Any]) -> dict[str, Any]:
        """Project a bounded workspace episode and close the typed task state."""

        episode = self.validate_episode(final)
        status = episode["status"]
        changed_paths = episode["changed_paths"]
        source = self._events.get("verification/result") or self._events.get("model/response")
        if source is None:
            raise ProductionMemoryError("production episode lacks a durable evidence event")

        memory_id = f"episode:{self.task_id}"
        searchable = " ".join(
            part
            for part in (
                self.objective,
                " ".join(changed_paths),
                f"status {status}",
                f"verification {episode['verification_passed']}",
                f"review {episode['devstral_status']}",
            )
            if part
        )
        memory_object = {
            "status": status,
            "objective_sha256": text_sha256(self.objective),
            "changed_paths": list(changed_paths),
            "diff_sha256": episode["diff_sha256"],
            "verification_passed": episode["verification_passed"],
            "devstral_status": episode["devstral_status"],
            "applied": episode["applied"],
            "stage_only": episode["stage_only"],
            "task_key": self.task_key,
        }
        if episode["deepseek_session_id"] is not None:
            memory_object["deepseek_session_id"] = episode["deepseek_session_id"]
        memory_event_id = str(uuid.uuid5(uuid.UUID(self.run_id), "production-memory-episode"))
        self.append(
            "memory/committed",
            {
                "memory_id": memory_id,
                "kind": "episode",
                "subject": self.stable_task_id,
                "predicate": "production_coding_outcome",
                "object": memory_object,
                "searchable_text": searchable,
                "verification_status": (
                    "tested" if episode["verification_passed"] else "observed"
                ),
                "evidence": [
                    {
                        "source_kind": "event",
                        "source_locator": f"event:{source['event_id']}",
                        "source_event_id": source["event_id"],
                        "source_sha256": source["payload_sha256"],
                        "authority": "production-worker-deterministic-record",
                        "excerpt": (
                            f"status={status}; "
                            f"verification_passed={episode['verification_passed']}; "
                            f"applied={episode['applied']}; changed_paths={len(changed_paths)}"
                        ),
                    }
                ],
            },
            event_id=memory_event_id,
            scope=self.workspace_scope,
        )
        memory = self.store.get(memory_id)
        if memory is None:
            raise ProductionMemoryError("production episode was not projected")
        evidence_ids = [item["evidence_id"] for item in memory["evidence"]]

        state_event_id = str(uuid.uuid5(uuid.UUID(self.run_id), "production-memory-state-2"))
        final_state = _final_state(
            previous=self._state,
            previous_sha256=task_state_sha256(self._state),
            through_event_id=state_event_id,
            status=str(status),
            evidence_ids=evidence_ids,
            source_event_id=str(source["event_id"]),
        )
        self.append(
            "state/committed",
            {"revision": 2, "state": final_state},
            event_id=state_event_id,
        )
        self._state = final_state
        return {
            "schema": PRODUCTION_MEMORY_SCHEMA,
            "memory_id": memory_id,
            "included_scope": self.workspace_scope.key,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "deepseek": (
                dict(final["deepseek_continuity"])
                if isinstance(final.get("deepseek_continuity"), Mapping)
                else None
            ),
            "state_revision": 2,
        }

    def validate_episode(self, final: Mapping[str, Any]) -> dict[str, Any]:
        """Validate every bounded episode field without mutating the Memory store."""

        status = final.get("status")
        if status not in {"verified", "failed"}:
            raise ProductionMemoryError("production episode status must be verified or failed")
        devstral = final.get("devstral")
        if not isinstance(devstral, Mapping):
            raise ProductionMemoryError("devstral result must be an object")
        continuity = final.get("deepseek_continuity")
        if continuity is not None and not isinstance(continuity, Mapping):
            raise ProductionMemoryError("deepseek_continuity must be an object or null")
        if continuity is not None and self.deepseek is None:
            raise ProductionMemoryError("Qwen Code episodes cannot claim DeepSeek continuity")
        deepseek_session_id = None
        if continuity is not None:
            deepseek_session_id = _bounded_text(
                continuity.get("session_id"), "deepseek session id", 256
            )
        return {
            "status": status,
            "diff_sha256": _digest(final.get("diff_sha256"), "diff_sha256"),
            "changed_paths": _paths(final.get("changed_paths", ())),
            "verification_passed": _boolean(
                final.get("verification_passed"), "verification_passed"
            ),
            "applied": _boolean(final.get("applied"), "applied"),
            "stage_only": _boolean(final.get("stage_only"), "stage_only"),
            "devstral_status": _bounded_text(
                str(devstral.get("status", "unknown")), "devstral status", 128
            ),
            "deepseek_session_id": deepseek_session_id,
        }


def _initial_state(
    *, task_id: str, objective: str, source_event_id: str, through_event_id: str
) -> dict[str, Any]:
    return validate_task_state(
        {
            "schema": TASK_STATE_SCHEMA,
            "task_id": task_id,
            "objective": {"text": objective, "source_event_id": source_event_id},
            "success_criteria": [],
            "constraints": [],
            "decisions": [],
            "completed": [],
            "current_action": {
                "text": "Retrieve prior workspace Memory, then execute the staged objective.",
                "owner": "coding-intelligence-production",
                "started_at": _now(),
            },
            "next_actions": [],
            "blockers": [],
            "open_tool_calls": [],
            "artifacts": [],
            "disputes": [],
            "through_event_id": through_event_id,
            "previous_state_sha256": None,
        }
    )


def _final_state(
    *,
    previous: Mapping[str, Any],
    previous_sha256: str,
    through_event_id: str,
    status: str,
    evidence_ids: Sequence[str],
    source_event_id: str,
) -> dict[str, Any]:
    verified = status == "verified"
    return validate_task_state(
        {
            **previous,
            "decisions": [
                {
                    "id": "d-terminal",
                    "text": f"Production worker terminal status is {status}.",
                    "rationale": "The deterministic worker result was durably recorded.",
                    "source_event_ids": [source_event_id],
                }
            ],
            "completed": (
                [
                    {
                        "id": "w-terminal",
                        "text": "Completed the staged production coding objective.",
                        "evidence_ids": list(evidence_ids),
                        "completed_at": _now(),
                    }
                ]
                if verified
                else []
            ),
            "current_action": {
                "text": f"Run is terminal with status {status}.",
                "owner": "coding-intelligence-production",
                "started_at": _now(),
            },
            "blockers": (
                []
                if verified
                else [
                    {
                        "id": "b-terminal",
                        "text": "The production coding objective did not verify.",
                        "status": "active",
                        "evidence_ids": list(evidence_ids),
                    }
                ]
            ),
            "through_event_id": through_event_id,
            "previous_state_sha256": previous_sha256,
        }
    )


def _deepseek_home(memory_root: Path) -> Path:
    override = os.environ.get("CODING_INTELLIGENCE_DEEPSEEK_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (memory_root.parent / "DeepSeekHarnessV1").resolve()


def _task_key(value: str) -> str:
    key = _bounded_text(value, "task_key", 128).strip()
    if any(character in key for character in "\r\n\x00"):
        raise ProductionMemoryError("task_key must be a single-line identifier")
    return key


def _paths(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProductionMemoryError("changed_paths must be a sequence")
    if len(value) > 4_096:
        raise ProductionMemoryError("changed_paths exceeds the production memory bound")
    return tuple(_bounded_text(item, "changed path", 4_096) for item in value)


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ProductionMemoryError(f"{label} must contain 1..{maximum} UTF-8 bytes")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ProductionMemoryError(f"{label} must be sha256:<hex>")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ProductionMemoryError(f"{label} must be boolean")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
