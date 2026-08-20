"""Durable Memory v1 recording for one bounded local coding run."""

from __future__ import annotations

import json
import math
import os
import platform
import re
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointError, _reject_secrets
from .compaction import TASK_STATE_SCHEMA, validate_task_state
from .memory_store import MemoryScope, MemoryStore, MemoryStoreError

RUN_FINALIZATION_SCHEMA = "coding-intelligence-run-finalization/v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class RunMemoryError(RuntimeError):
    """A coding run cannot truthfully advance its durable memory."""


@dataclass(frozen=True)
class RunIdentity:
    task_id: str
    session_id: str

    @classmethod
    def create(cls) -> RunIdentity:
        return cls(str(uuid.uuid4()), str(uuid.uuid4()))


def default_memory_root() -> Path:
    override = os.environ.get("CODING_INTELLIGENCE_MEMORY_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RunMemoryError("LOCALAPPDATA is required for the Windows memory root")
        return (Path(local_app_data) / "CodingIntelligence" / "MemoryV1").resolve()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return (base / "CodingIntelligence" / "MemoryV1").resolve()


def text_sha256(value: str) -> str:
    return f"sha256:{sha256(value.encode('utf-8')).hexdigest()}"


def bytes_sha256(value: bytes) -> str:
    return f"sha256:{sha256(value).hexdigest()}"


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def ensure_secret_free(value: Any) -> None:
    """Reject secret-like material without persisting the inspected value."""

    _secret_scan(value)


class RunMemoryRecorder:
    """Append bounded run evidence without copying prompts, outputs, or diffs."""

    def __init__(
        self,
        *,
        root: Path,
        identity: RunIdentity,
        workspace: Path,
        objective: str,
        model: str,
        host_id: str | None = None,
    ):
        self.root = Path(root).expanduser().resolve()
        self.identity = RunIdentity(
            _canonical_uuid(identity.task_id, "task_id"),
            _canonical_uuid(identity.session_id, "session_id"),
        )
        self.workspace = Path(workspace).expanduser().resolve(strict=True)
        self.objective = _bounded_text(objective, "objective", 16_384)
        self.model = _bounded_text(model, "model", 512)
        self.host_id = _host_id(host_id)
        self.workspace_id = text_sha256(os.path.normcase(str(self.workspace)))
        self.scope = MemoryScope(
            owner_id="local-owner",
            workspace_id=self.workspace_id,
            task_id=self.identity.task_id,
            agent_role="implementer",
            session_id=self.identity.session_id,
        )
        self.store = MemoryStore(self.root)

    def ensure_started(
        self,
        *,
        manifest_sha256: str | None = None,
        mutable: Sequence[str] = (),
        context: Sequence[str] = (),
        verify_context: Sequence[str] = (),
        test_command: Sequence[str] = (),
    ) -> dict[str, Any]:
        material = {
            "objective": self.objective,
            "model": self.model,
            "mutable": list(mutable),
            "context": list(context),
            "verify_context": list(verify_context),
            "test_command": list(test_command),
        }
        _secret_scan(material)
        if manifest_sha256 is not None:
            _digest(manifest_sha256, "manifest_sha256")

        started = self._find_event("task/started")
        if started is None:
            started = self._append(
                "task/started",
                {
                    "objective_sha256": text_sha256(self.objective),
                    "workspace_id": self.workspace_id,
                    "model": self.model,
                    "manifest_sha256": manifest_sha256,
                    "mutable_sha256": _json_sha256(list(mutable)),
                    "context_sha256": _json_sha256(list(context)),
                    "verify_context_sha256": _json_sha256(list(verify_context)),
                    "test_command_sha256": _json_sha256(list(test_command)),
                },
            )
        state_row = self._state_row()
        if state_row is None:
            state_event_id = str(uuid.uuid4())
            state = _initial_state(
                task_id=self.identity.task_id,
                objective=self.objective,
                source_event_id=started["event_id"],
                through_event_id=state_event_id,
            )
            self._append(
                "state/committed",
                {"revision": 1, "state": state},
                event_id=state_event_id,
                parent_event_id=started["event_id"],
            )
        else:
            state = validate_task_state(json.loads(state_row["state_json"]))
            if state["objective"]["text"] != self.objective:
                raise RunMemoryError("existing run memory belongs to a different objective")
        return {
            "root": str(self.root),
            "task_id": self.identity.task_id,
            "session_id": self.identity.session_id,
            "host_id": self.host_id,
            "workspace_id": self.workspace_id,
            "resume": self.resume_classification(),
        }

    def record_model_intent(
        self,
        *,
        request_sha256: str,
        prompt_sha256: str,
        trajectory_path: Path,
    ) -> dict[str, Any]:
        _digest(request_sha256, "request_sha256")
        _digest(prompt_sha256, "prompt_sha256")
        classification = self.resume_classification()
        if classification != "not_started":
            raise RunMemoryError(
                f"model dispatch is not safe to repeat; prior outcome is {classification}"
            )
        return self._append(
            "model/request",
            {
                "call_id": "model-call-1",
                "model": self.model,
                "request_sha256": request_sha256,
                "prompt_sha256": prompt_sha256,
                "trajectory_locator": str(Path(trajectory_path).resolve()),
                "raw_request_stored": False,
                "automatic_retries": 0,
            },
        )

    def record_model_response(
        self,
        *,
        response_sha256: str,
        response_bytes: int,
        http_status: int,
        inference_seconds: float,
    ) -> dict[str, Any]:
        _digest(response_sha256, "response_sha256")
        if self._find_event("model/request") is None:
            raise RunMemoryError("model response has no durable request intent")
        existing = self._find_event("model/response")
        if existing is not None:
            return existing
        return self._append(
            "model/response",
            {
                "call_id": "model-call-1",
                "response_sha256": response_sha256,
                "response_bytes": _nonnegative_int(response_bytes, "response_bytes"),
                "http_status": _nonnegative_int(http_status, "http_status"),
                "inference_seconds": _nonnegative_number(
                    inference_seconds, "inference_seconds"
                ),
                "raw_response_stored": False,
                "automatic_retries": 0,
            },
            parent_event_id=self._find_event("model/request")["event_id"],
        )

    def record_implementation(
        self,
        *,
        files: Sequence[Mapping[str, str]],
        diff_sha256: str,
        test_command_sha256: str,
        test_exit: int,
        test_output_sha256: str,
        trajectory_path: Path,
        trajectory_sha256: str,
        status: str,
    ) -> dict[str, str]:
        for name, value in (
            ("diff_sha256", diff_sha256),
            ("test_command_sha256", test_command_sha256),
            ("test_output_sha256", test_output_sha256),
            ("trajectory_sha256", trajectory_sha256),
        ):
            _digest(value, name)
        normalized_files = []
        for item in files:
            if set(item) != {"path", "before_sha256", "after_sha256"}:
                raise RunMemoryError("edited file summaries have an invalid shape")
            normalized_files.append(
                {
                    "path": _bounded_text(item["path"], "edited path", 4_096),
                    "before_sha256": _digest(item["before_sha256"], "before_sha256"),
                    "after_sha256": _digest(item["after_sha256"], "after_sha256"),
                }
            )
        edited = self._find_event("file/edited")
        if edited is None:
            edited = self._append(
                "file/edited",
                {
                    "files": normalized_files,
                    "diff_sha256": diff_sha256,
                    "stage_locator": str(Path(trajectory_path).resolve().parent),
                },
            )
        tested = self._find_event("verification/result", phase="authoritative_test")
        if tested is None:
            tested = self._append(
                "verification/result",
                {
                    "phase": "authoritative_test",
                    "status": _status(status),
                    "test_command_sha256": test_command_sha256,
                    "test_exit": int(test_exit),
                    "test_output_sha256": test_output_sha256,
                    "diff_sha256": diff_sha256,
                    "trajectory_locator": str(Path(trajectory_path).resolve()),
                    "trajectory_sha256": trajectory_sha256,
                },
                parent_event_id=edited["event_id"],
            )
        return {"file_event_id": edited["event_id"], "test_event_id": tested["event_id"]}

    def finalize(self, value: Mapping[str, Any]) -> dict[str, Any]:
        summary = _finalization(value)
        self.ensure_started()
        self._ensure_recovery_events(summary)
        test_event = self._find_event("verification/result", phase="authoritative_test")
        if test_event is None:
            raise RunMemoryError("finalization has no authoritative-test event")

        verifier_event = self._find_event(
            "verification/result", phase="independent_verifier"
        )
        if verifier_event is None:
            verifier_event = self._append(
                "verification/result",
                {
                    "phase": "independent_verifier",
                    **summary["verifier"],
                },
                parent_event_id=test_event["event_id"],
            )
        restore_event = self._find_event("tool/result", phase="backend_restore")
        if restore_event is None:
            restore_event = self._append(
                "tool/result",
                {"phase": "backend_restore", **summary["restore"]},
                parent_event_id=verifier_event["event_id"],
            )
        terminal_type = "task/completed" if summary["status"] == "verified" else "task/failed"
        terminal = self._find_event(terminal_type)
        if terminal is None:
            terminal = self._append(
                terminal_type,
                {
                    "status": summary["status"],
                    "process_exit": summary["process_exit"],
                    "command_exit": summary["command_exit"],
                    "model_calls": summary["model_calls"],
                    "automatic_retries": summary["automatic_retries"],
                    "failure_sha256": summary["failure_sha256"],
                },
                parent_event_id=restore_event["event_id"],
            )

        memory_id = f"episode:{self.identity.task_id}"
        memory = self.store.get(memory_id)
        if memory is None:
            memory_event_id = str(uuid.uuid4())
            test_evidence_id = str(uuid.uuid5(uuid.UUID(memory_event_id), "test"))
            verifier_evidence_id = str(uuid.uuid5(uuid.UUID(memory_event_id), "verifier"))
            self._append(
                "memory/committed",
                {
                    "memory_id": memory_id,
                    "kind": "episode",
                    "subject": self.identity.task_id,
                    "predicate": "coding_run_outcome",
                    "object": {
                        "status": summary["status"],
                        "model": self.model,
                        "test_exit": summary["implementation"]["test_exit"],
                        "verifier_status": summary["verifier"]["status"],
                        "verdict": summary["verifier"]["verdict"],
                        "restore_status": summary["restore"]["status"],
                        "stage_locator": summary["implementation"]["stage_locator"],
                        "trajectory_locator": summary["implementation"][
                            "trajectory_locator"
                        ],
                        "trajectory_sha256": summary["implementation"][
                            "trajectory_sha256"
                        ],
                        "diff_sha256": summary["implementation"]["diff_sha256"],
                        "response_observed": summary["implementation"][
                            "response_observed"
                        ],
                        "model_calls": summary["model_calls"],
                        "automatic_retries": summary["automatic_retries"],
                    },
                    "searchable_text": (
                        f"{self.objective} coding run {summary['status']} "
                        f"test {summary['implementation']['test_exit']} "
                        f"verifier {summary['verifier']['status']}"
                    ),
                    "observed_at": _now(),
                    "verification_status": (
                        "tested" if summary["implementation"]["test_exit"] == 0 else "observed"
                    ),
                    "evidence": [
                        {
                            "evidence_id": test_evidence_id,
                            "source_kind": "test",
                            "source_locator": summary["implementation"][
                                "trajectory_locator"
                            ],
                            "source_event_id": test_event["event_id"],
                            "source_sha256": summary["implementation"][
                                "test_output_sha256"
                            ],
                            "authority": "authoritative-test",
                            "excerpt": (
                                f"status={summary['implementation']['status']}; "
                                f"exit={summary['implementation']['test_exit']}"
                            ),
                        },
                        {
                            "evidence_id": verifier_evidence_id,
                            "source_kind": "event",
                            "source_locator": f"event:{verifier_event['event_id']}",
                            "source_event_id": verifier_event["event_id"],
                            "source_sha256": verifier_event["payload_sha256"],
                            "authority": "independent-verifier",
                            "excerpt": (
                                f"status={summary['verifier']['status']}; "
                                f"verdict={summary['verifier']['verdict']}"
                            ),
                        },
                    ],
                },
                event_id=memory_event_id,
                parent_event_id=terminal["event_id"],
            )
        memory = self.store.get(memory_id)
        if memory is None:
            raise RunMemoryError("episodic memory was not projected")

        state_row = self._state_row()
        if state_row is None:
            raise RunMemoryError("initial task state is missing during finalization")
        if int(state_row["revision"]) < 2:
            prior = validate_task_state(json.loads(state_row["state_json"]))
            final_event_id = str(uuid.uuid4())
            final_state = _final_state(
                previous=prior,
                previous_sha256=state_row["state_sha256"],
                through_event_id=final_event_id,
                summary=summary,
                evidence=memory["evidence"],
            )
            self._append(
                "state/committed",
                {"revision": 2, "state": final_state},
                event_id=final_event_id,
                parent_event_id=memory["source_event_id"],
            )
        return {
            "status": "recorded",
            "root": str(self.root),
            "task_id": self.identity.task_id,
            "session_id": self.identity.session_id,
            "event_count": len(self._events()),
            "resume": self.resume_classification(),
            "terminal_status": summary["status"],
        }

    def resume_classification(self) -> str:
        if self._find_event("model/request") is None:
            return "not_started"
        if self._find_event("model/response") is None:
            return "outcome_unknown"
        return "recorded"

    def failure_report(self, message: str, *, test_command: Sequence[str]) -> dict[str, Any]:
        request = self._find_event("model/request")
        response = self._find_event("model/response")
        trajectory = (
            Path(request["payload"]["trajectory_locator"]) if request is not None else None
        )
        empty = text_sha256("")
        return {
            "status": "failed",
            "model": self.model,
            "stage": str(trajectory.parent) if trajectory is not None else "unavailable",
            "diagnosis": None,
            "diff": "",
            "inference_seconds": (
                response["payload"]["inference_seconds"] if response is not None else 0.0
            ),
            "model_calls": 1 if request is not None else 0,
            "automatic_retries": 0,
            "test_seconds": 0.0,
            "test_command": list(test_command),
            "test_exit": -1,
            "test_output": "",
            "trajectory": str(trajectory) if trajectory is not None else "unavailable",
            "trajectory_sha256": (
                file_sha256(trajectory)
                if trajectory is not None and trajectory.is_file()
                else empty
            ),
            "request_sha256": (
                request["payload"]["request_sha256"] if request is not None else empty
            ),
            "prompt_sha256": (
                request["payload"]["prompt_sha256"] if request is not None else empty
            ),
            "response_sha256": (
                response["payload"]["response_sha256"] if response is not None else empty
            ),
            "response_observed": response is not None,
            "response_bytes": response["payload"]["response_bytes"] if response else 0,
            "http_status": response["payload"]["http_status"] if response else 0,
            "diff_sha256": empty,
            "test_command_sha256": _json_sha256(list(test_command)),
            "test_output_sha256": empty,
            "edited_files": [],
            "raw_failure": _bounded_text(message, "failure", 8_192),
            "memory": {
                "root": str(self.root),
                "task_id": self.identity.task_id,
                "session_id": self.identity.session_id,
                "host_id": self.host_id,
                "workspace_id": self.workspace_id,
                "resume": self.resume_classification(),
            },
        }

    def _ensure_recovery_events(self, summary: Mapping[str, Any]) -> None:
        implementation = summary["implementation"]
        if self._find_event("model/request") is None and summary["model_calls"] > 0:
            self.record_model_intent(
                request_sha256=implementation["request_sha256"],
                prompt_sha256=implementation["prompt_sha256"],
                trajectory_path=Path(implementation["trajectory_locator"]),
            )
        if (
            self._find_event("model/response") is None
            and summary["model_calls"] > 0
            and implementation["response_observed"]
        ):
            self.record_model_response(
                response_sha256=implementation["response_sha256"],
                response_bytes=implementation["response_bytes"],
                http_status=implementation["http_status"],
                inference_seconds=implementation["inference_seconds"],
            )
        if self._find_event("verification/result", phase="authoritative_test") is None:
            self.record_implementation(
                files=implementation["files"],
                diff_sha256=implementation["diff_sha256"],
                test_command_sha256=implementation["test_command_sha256"],
                test_exit=implementation["test_exit"],
                test_output_sha256=implementation["test_output_sha256"],
                trajectory_path=Path(implementation["trajectory_locator"]),
                trajectory_sha256=implementation["trajectory_sha256"],
                status=implementation["status"],
            )

    def _append(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        event_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            return self.store.append(
                host_id=self.host_id,
                session_id=self.identity.session_id,
                task_id=self.identity.task_id,
                event_type=event_type,
                actor={"kind": "system", "id": "coding-continuity-runtime"},
                scope=self.scope,
                payload=payload,
                event_id=event_id,
                parent_event_id=parent_event_id,
            )
        except MemoryStoreError as error:
            raise RunMemoryError(f"memory append failed for {event_type}: {error}") from error

    def _events(self) -> list[dict[str, Any]]:
        with closing(self.store._connect()) as connection:
            rows = connection.execute(
                "SELECT event_id, event_type, segment_path, byte_offset, payload_sha256 "
                "FROM event_index WHERE session_id = ? ORDER BY seq",
                (self.identity.session_id,),
            ).fetchall()
        events = []
        for row in rows:
            path = self.root / row["segment_path"]
            with path.open("rb") as stream:
                stream.seek(row["byte_offset"])
                record = json.loads(stream.readline())
            payload = dict(self.store._resolve_payload(record))
            events.append(
                {
                    "event_id": row["event_id"],
                    "type": row["event_type"],
                    "payload": payload,
                    "payload_sha256": row["payload_sha256"],
                }
            )
        return events

    def _find_event(self, event_type: str, *, phase: str | None = None) -> dict[str, Any] | None:
        for event in reversed(self._events()):
            if event["type"] != event_type:
                continue
            if phase is not None and event["payload"].get("phase") != phase:
                continue
            return event
        return None

    def _state_row(self) -> dict[str, Any] | None:
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                "SELECT revision, state_json, state_sha256, through_event_id "
                "FROM task_state WHERE task_id = ?",
                (self.identity.task_id,),
            ).fetchone()
        return None if row is None else dict(row)


def _initial_state(
    *, task_id: str, objective: str, source_event_id: str, through_event_id: str
) -> dict[str, Any]:
    return {
        "schema": TASK_STATE_SCHEMA,
        "task_id": task_id,
        "objective": {"text": objective, "source_event_id": source_event_id},
        "success_criteria": [
            {
                "id": "sc-1",
                "text": "Produce a scoped staged edit that passes its authoritative test.",
                "status": "open",
                "evidence_ids": [],
            },
            {
                "id": "sc-2",
                "text": "Receive an independent verifier decision and restore the idle backend.",
                "status": "open",
                "evidence_ids": [],
            },
        ],
        "constraints": [
            {
                "id": "c-1",
                "text": "Original workspace files remain unchanged by the staged local edit.",
                "status": "active",
                "source_event_id": source_event_id,
            },
            {
                "id": "c-2",
                "text": "No automatic model retry is permitted.",
                "status": "active",
                "source_event_id": source_event_id,
            },
        ],
        "decisions": [],
        "completed": [],
        "current_action": {
            "text": "Dispatch the single local implementation request.",
            "owner": "continuity-runtime",
            "started_at": _now(),
        },
        "next_actions": [
            {"id": "n-1", "order": 1, "text": "Record model outcome.", "depends_on": []},
            {
                "id": "n-2",
                "order": 2,
                "text": "Run the authoritative staged test.",
                "depends_on": ["n-1"],
            },
            {
                "id": "n-3",
                "order": 3,
                "text": "Run independent verification and restore the backend.",
                "depends_on": ["n-2"],
            },
        ],
        "blockers": [],
        "open_tool_calls": [
            {"call_id": "model-call-1", "tool": "local-model-post", "outcome": "not_started"}
        ],
        "artifacts": [],
        "disputes": [],
        "through_event_id": through_event_id,
        "previous_state_sha256": None,
    }


def _final_state(
    *,
    previous: Mapping[str, Any],
    previous_sha256: str,
    through_event_id: str,
    summary: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    evidence_ids = [item["evidence_id"] for item in evidence]
    verified = summary["status"] == "verified"
    state = {
        **previous,
        "success_criteria": [
            {
                **item,
                "status": "met" if verified else "open",
                "evidence_ids": evidence_ids if verified else [],
            }
            for item in previous["success_criteria"]
        ],
        "decisions": [
            {
                "id": "d-final",
                "text": f"Terminal coding-run status is {summary['status']}.",
                "rationale": "Authoritative test, verifier, and restore evidence were recorded.",
                "source_event_ids": sorted(
                    {item["source_event_id"] for item in evidence if item["source_event_id"]}
                ),
            }
        ],
        "completed": (
            [
                {
                    "id": "w-1",
                    "text": "Completed staged implementation, test, verification, and restore.",
                    "evidence_ids": evidence_ids,
                    "completed_at": _now(),
                }
            ]
            if verified
            else []
        ),
        "current_action": {
            "text": "Run is terminal; inspect cited evidence before promotion.",
            "owner": "continuity-runtime",
            "started_at": _now(),
        },
        "next_actions": (
            []
            if verified
            else [
                {
                    "id": "n-review",
                    "order": 1,
                    "text": "Inspect the preserved stage and terminal evidence.",
                    "depends_on": [],
                }
            ]
        ),
        "blockers": (
            []
            if verified
            else [
                {
                    "id": "b-terminal",
                    "text": f"Coding run ended with status {summary['status']}.",
                    "status": "active",
                    "evidence_ids": evidence_ids,
                }
            ]
        ),
        "open_tool_calls": (
            []
            if summary["implementation"]["response_observed"]
            else [
                {
                    "call_id": "model-call-1",
                    "tool": "local-model-post",
                    "outcome": "unknown",
                }
            ]
        ),
        "artifacts": [
            {
                "path": summary["implementation"]["stage_locator"],
                "sha256": summary["implementation"]["diff_sha256"],
                "evidence_id": evidence_ids[0],
            },
            {
                "path": summary["implementation"]["trajectory_locator"],
                "sha256": summary["implementation"]["trajectory_sha256"],
                "evidence_id": evidence_ids[0],
            },
        ],
        "through_event_id": through_event_id,
        "previous_state_sha256": previous_sha256,
    }
    return validate_task_state(state)


def _finalization(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema") != RUN_FINALIZATION_SCHEMA:
        raise RunMemoryError(f"finalization schema must be {RUN_FINALIZATION_SCHEMA}")
    required = {
        "schema",
        "status",
        "process_exit",
        "command_exit",
        "model_calls",
        "automatic_retries",
        "failure_sha256",
        "implementation",
        "verifier",
        "restore",
    }
    if set(value) != required:
        raise RunMemoryError("finalization fields do not match the v1 contract")
    implementation = _shape(
        value["implementation"],
        "implementation",
        {
            "status",
            "stage_locator",
            "trajectory_locator",
            "trajectory_sha256",
            "request_sha256",
            "prompt_sha256",
            "response_sha256",
            "response_observed",
            "response_bytes",
            "http_status",
            "inference_seconds",
            "files",
            "diff_sha256",
            "test_command_sha256",
            "test_exit",
            "test_output_sha256",
        },
    )
    verifier = _shape(
        value["verifier"],
        "verifier",
        {"status", "model", "verdict", "reason_sha256", "risks_sha256", "model_calls"},
    )
    restore = _shape(
        value["restore"], "restore", {"status", "backend", "error_sha256"}
    )
    files = implementation["files"]
    if not isinstance(files, list) or len(files) > 4 or any(
        not isinstance(item, Mapping) for item in files
    ):
        raise RunMemoryError("implementation files must contain at most four objects")
    automatic_retries = _nonnegative_int(value["automatic_retries"], "automatic_retries")
    if automatic_retries != 0:
        raise RunMemoryError("automatic_retries must remain zero")
    normalized = {
        "schema": RUN_FINALIZATION_SCHEMA,
        "status": _one_of(value["status"], "status", {"verified", "rejected", "failed"}),
        "process_exit": _integer(value["process_exit"], "process_exit"),
        "command_exit": _integer(value["command_exit"], "command_exit"),
        "model_calls": _nonnegative_int(value["model_calls"], "model_calls"),
        "automatic_retries": automatic_retries,
        "failure_sha256": _optional_digest(value["failure_sha256"], "failure_sha256"),
        "implementation": {
            **implementation,
            "status": _one_of(
                implementation["status"], "implementation status", {"verified", "failed"}
            ),
            "stage_locator": _bounded_text(
                implementation["stage_locator"], "stage_locator", 8_192
            ),
            "trajectory_locator": _bounded_text(
                implementation["trajectory_locator"], "trajectory_locator", 8_192
            ),
            "response_bytes": _nonnegative_int(
                implementation["response_bytes"], "response_bytes"
            ),
            "response_observed": _boolean(
                implementation["response_observed"], "response_observed"
            ),
            "http_status": _nonnegative_int(
                implementation["http_status"], "http_status"
            ),
            "inference_seconds": _nonnegative_number(
                implementation["inference_seconds"], "inference_seconds"
            ),
            "test_exit": _integer(implementation["test_exit"], "test_exit"),
            "files": list(files),
        },
        "verifier": {
            "status": _one_of(
                verifier["status"],
                "verifier status",
                {"accepted", "rejected", "failed", "not_run"},
            ),
            "model": _bounded_text(verifier["model"], "verifier model", 512),
            "verdict": (
                None
                if verifier["verdict"] is None
                else _bounded_text(verifier["verdict"], "verdict", 64)
            ),
            "reason_sha256": _optional_digest(verifier["reason_sha256"], "reason_sha256"),
            "risks_sha256": _optional_digest(verifier["risks_sha256"], "risks_sha256"),
            "model_calls": _nonnegative_int(verifier["model_calls"], "verifier model_calls"),
        },
        "restore": {
            "status": _one_of(restore["status"], "restore status", {"ready", "failed"}),
            "backend": _bounded_text(restore["backend"], "restore backend", 512),
            "error_sha256": _optional_digest(restore["error_sha256"], "restore error_sha256"),
        },
    }
    for name in (
        "trajectory_sha256",
        "request_sha256",
        "prompt_sha256",
        "response_sha256",
        "diff_sha256",
        "test_command_sha256",
        "test_output_sha256",
    ):
        normalized["implementation"][name] = _digest(implementation[name], name)
    _secret_scan(normalized)
    return normalized


def _shape(value: Any, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise RunMemoryError(f"{label} fields do not match the v1 contract")
    return dict(value)


def _host_id(value: str | None) -> str:
    raw = value or platform.node() or "local-host"
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-")[:128]
    return normalized or "local-host"


def _canonical_uuid(value: str, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (TypeError, ValueError) as error:
        raise RunMemoryError(f"{label} must be a canonical UUID") from error
    if str(parsed) != value.lower():
        raise RunMemoryError(f"{label} must be a canonical UUID")
    return str(parsed)


def _bounded_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise RunMemoryError(f"{label} must be non-empty text of at most {limit} characters")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RunMemoryError(f"{label} must be a canonical sha256 digest")
    return value


def _optional_digest(value: Any, label: str) -> str | None:
    return None if value is None else _digest(value, label)


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunMemoryError(f"{label} must be a non-negative integer")
    return value


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RunMemoryError(f"{label} must be an integer")
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise RunMemoryError(f"{label} must be a boolean")
    return value


def _nonnegative_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise RunMemoryError(f"{label} must be a non-negative number")
    return round(float(value), 6)


def _status(value: Any) -> str:
    return _bounded_text(value, "status", 64)


def _one_of(value: Any, label: str, allowed: set[str]) -> str:
    text = _bounded_text(value, label, 64)
    if text not in allowed:
        raise RunMemoryError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return text


def _json_sha256(value: Any) -> str:
    return bytes_sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _secret_scan(value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    try:
        _reject_secrets(text)
    except CheckpointError as error:
        raise RunMemoryError("secret-like material rejected before model dispatch") from error


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
