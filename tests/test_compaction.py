from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from agent_continuity.compaction import (  # noqa: E402
    TASK_STATE_SCHEMA,
    FrozenSourceRange,
    TaskStateError,
    load_task_state,
    serialize_task_state,
    task_state_sha256,
    validate_compaction_proposal,
    validate_state_chain,
    validate_task_state,
    write_task_state,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
ARTIFACT_HASH = "sha256:" + "d" * 64


class Resolver:
    def __init__(self) -> None:
        self.events = {
            "event-objective": HASH_A,
            "event-constraint": HASH_A,
            "event-decision": HASH_A,
            "event-previous": HASH_A,
            "event-first": HASH_B,
            "event-tail": HASH_B,
            "event-last": HASH_C,
        }
        self.evidence = {
            "evidence-artifact",
            "evidence-blocked",
            "evidence-cleared",
            "evidence-completed",
            "evidence-criterion",
            "evidence-dispute",
            "evidence-old-work",
        }
        self.artifacts = {"src/example.py": ARTIFACT_HASH}

    def event_record_sha256(self, event_id: str) -> str | None:
        return self.events.get(event_id)

    def evidence_exists(self, evidence_id: str) -> bool:
        return evidence_id in self.evidence

    def artifact_sha256(self, path: str) -> str | None:
        return self.artifacts.get(path)


def base_state() -> dict[str, object]:
    return {
        "schema": TASK_STATE_SCHEMA,
        "task_id": "task-memory-v1",
        "objective": {
            "text": "Preserve continuity without losing obligations.",
            "source_event_id": "event-objective",
        },
        "success_criteria": [
            {
                "id": "criterion-replay",
                "text": "Fresh-process replay preserves the state.",
                "status": "open",
                "evidence_ids": [],
            }
        ],
        "constraints": [
            {
                "id": "constraint-no-retry",
                "text": "Do not retry a rejected compaction automatically.",
                "status": "active",
                "source_event_id": "event-constraint",
            }
        ],
        "decisions": [
            {
                "id": "decision-jsonl",
                "text": "Raw JSONL remains canonical.",
                "rationale": "Derived state must be rebuildable.",
                "source_event_ids": ["event-decision"],
            }
        ],
        "completed": [
            {
                "id": "work-schema",
                "text": "The state schema was frozen.",
                "evidence_ids": ["evidence-old-work"],
                "completed_at": "2026-08-20T08:00:00Z",
            }
        ],
        "current_action": {
            "text": "Validate the compaction proposal.",
            "owner": "deterministic-reducer",
            "started_at": "2026-08-20T08:01:00Z",
        },
        "next_actions": [
            {
                "id": "next-verifier",
                "order": 1,
                "text": "Run the independent semantic verifier.",
                "depends_on": [],
            }
        ],
        "blockers": [
            {
                "id": "blocker-proof",
                "text": "Replay has not yet been proven.",
                "status": "active",
                "evidence_ids": ["evidence-blocked"],
            }
        ],
        "open_tool_calls": [
            {"call_id": "call-copy", "tool": "copy_tree", "outcome": "unknown"}
        ],
        "artifacts": [
            {
                "path": "src/example.py",
                "sha256": ARTIFACT_HASH,
                "evidence_id": "evidence-artifact",
            }
        ],
        "disputes": [
            {
                "id": "dispute-owner",
                "text": "The owner of one subprocess is unresolved.",
                "status": "unresolved",
                "evidence_ids": ["evidence-dispute"],
            }
        ],
        "through_event_id": "event-previous",
        "previous_state_sha256": None,
    }


def candidate_state(previous: dict[str, object]) -> dict[str, object]:
    candidate = copy.deepcopy(previous)
    candidate["through_event_id"] = "event-last"
    candidate["previous_state_sha256"] = task_state_sha256(previous)
    candidate["success_criteria"][0]["status"] = "met"  # type: ignore[index]
    candidate["success_criteria"][0]["evidence_ids"] = [  # type: ignore[index]
        "evidence-criterion"
    ]
    candidate["blockers"][0]["status"] = "cleared"  # type: ignore[index]
    candidate["blockers"][0]["evidence_ids"].append(  # type: ignore[index, union-attr]
        "evidence-cleared"
    )
    candidate["completed"].append(  # type: ignore[union-attr]
        {
            "id": "work-replay",
            "text": "Fresh-process replay passed.",
            "evidence_ids": ["evidence-completed"],
            "completed_at": "2026-08-20T08:02:00Z",
        }
    )
    return candidate


def source_range() -> FrozenSourceRange:
    return FrozenSourceRange(
        first_event_id="event-first",
        first_record_sha256=HASH_B,
        last_event_id="event-last",
        last_record_sha256=HASH_C,
        retained_tail_event_id="event-tail",
    )


class TaskStateTests(unittest.TestCase):
    def test_canonical_serialization_and_hash_are_stable(self) -> None:
        state = base_state()
        reordered = {key: state[key] for key in reversed(list(state))}
        self.assertEqual(serialize_task_state(state), serialize_task_state(reordered))
        self.assertEqual(task_state_sha256(state), task_state_sha256(reordered))
        self.assertTrue(serialize_task_state(state).endswith(b"\n"))

    def test_schema_rejects_omitted_and_unknown_fields(self) -> None:
        omitted = base_state()
        del omitted["constraints"]
        with self.assertRaisesRegex(TaskStateError, "missing required keys: constraints"):
            validate_task_state(omitted)

        unknown = base_state()
        unknown["model_summary"] = "not authority"
        with self.assertRaisesRegex(TaskStateError, "unknown keys: model_summary"):
            validate_task_state(unknown)

    def test_schema_rejects_duplicate_ids_and_unproven_completion(self) -> None:
        duplicate = base_state()
        duplicate["next_actions"].append(copy.deepcopy(duplicate["next_actions"][0]))  # type: ignore[index, union-attr]
        with self.assertRaisesRegex(TaskStateError, "duplicate id"):
            validate_task_state(duplicate)

        unproven = base_state()
        unproven["completed"][0]["evidence_ids"] = []  # type: ignore[index]
        with self.assertRaisesRegex(TaskStateError, "must prove completed work"):
            validate_task_state(unproven)

    def test_immutable_write_and_fresh_process_reload(self) -> None:
        state = base_state()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "revision-1.json"
            write_task_state(path, state)
            self.assertEqual(load_task_state(path), validate_task_state(state))

            env = dict(os.environ)
            source_root = str(Path(__file__).resolve().parents[1] / "src")
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (source_root, env.get("PYTHONPATH", "")) if part
            )
            script = (
                "import json,sys; "
                "from pathlib import Path; "
                "from agent_continuity.compaction import load_task_state,task_state_sha256; "
                "print(json.dumps({'hash':task_state_sha256(load_task_state(Path(sys.argv[1])))}, "
                "sort_keys=True))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(json.loads(result.stdout)["hash"], task_state_sha256(state))

            changed = base_state()
            changed["objective"]["text"] = "Different"  # type: ignore[index]
            with self.assertRaisesRegex(TaskStateError, "refusing to overwrite"):
                write_task_state(path, changed)

    def test_state_hash_chain_accepts_valid_chain_and_rejects_stale_link(self) -> None:
        first = base_state()
        second = copy.deepcopy(first)
        second["previous_state_sha256"] = task_state_sha256(first)
        second["through_event_id"] = "event-first"
        third = copy.deepcopy(second)
        third["previous_state_sha256"] = task_state_sha256(second)
        third["through_event_id"] = "event-last"
        self.assertEqual(len(validate_state_chain([first, second, third])), 3)

        third["previous_state_sha256"] = HASH_A
        with self.assertRaisesRegex(TaskStateError, "chain is broken"):
            validate_state_chain([first, second, third])


class CompactionValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = Resolver()
        self.previous = base_state()
        self.candidate = candidate_state(self.previous)

    def test_valid_proposal_is_verifier_ready_not_committed(self) -> None:
        result = validate_compaction_proposal(
            self.previous, self.candidate, source_range(), self.resolver
        )
        self.assertTrue(result.accepted)
        self.assertTrue(result.verifier_ready)
        self.assertEqual(result.issues, ())
        report = result.as_dict()
        self.assertEqual(report["deterministic_checks"], "passed")
        self.assertEqual(report["semantic_verifier"]["status"], "pending")
        self.assertEqual(report["previous_state_sha256"], task_state_sha256(self.previous))
        self.assertEqual(report["candidate_state_sha256"], task_state_sha256(self.candidate))
        self.assertEqual(report["preserved"]["next_actions"], ["next-verifier"])

    def test_rejects_omitted_constraint_next_action_tool_artifact_and_dispute(self) -> None:
        for field in (
            "success_criteria",
            "constraints",
            "decisions",
            "completed",
            "next_actions",
            "blockers",
            "open_tool_calls",
            "artifacts",
            "disputes",
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(self.candidate)
                candidate[field] = []
                result = validate_compaction_proposal(
                    self.previous, candidate, source_range(), self.resolver
                )
                self.assertFalse(result.accepted)
                self.assertIn("invariant_omitted", {issue.code for issue in result.issues})

        objective = copy.deepcopy(self.candidate)
        objective["objective"]["text"] = "A forged replacement objective."  # type: ignore[index]
        result = validate_compaction_proposal(
            self.previous, objective, source_range(), self.resolver
        )
        self.assertIn("objective_changed", {issue.code for issue in result.issues})

    def test_rejects_forged_event_and_evidence_citations(self) -> None:
        candidate = copy.deepcopy(self.candidate)
        candidate["decisions"].append(  # type: ignore[union-attr]
            {
                "id": "decision-forged",
                "text": "Claim without a source.",
                "rationale": "No authority.",
                "source_event_ids": ["event-does-not-exist"],
            }
        )
        candidate["completed"].append(  # type: ignore[union-attr]
            {
                "id": "work-forged",
                "text": "Claim without proof.",
                "evidence_ids": ["evidence-does-not-exist"],
                "completed_at": "2026-08-20T08:03:00Z",
            }
        )
        result = validate_compaction_proposal(
            self.previous, candidate, source_range(), self.resolver
        )
        codes = {issue.code for issue in result.issues}
        self.assertIn("missing_event", codes)
        self.assertIn("missing_evidence", codes)
        self.assertFalse(result.verifier_ready)

    def test_rejects_stale_previous_state_and_artifact(self) -> None:
        candidate = copy.deepcopy(self.candidate)
        candidate["previous_state_sha256"] = HASH_A
        self.resolver.artifacts["src/example.py"] = HASH_B
        result = validate_compaction_proposal(
            self.previous, candidate, source_range(), self.resolver
        )
        codes = {issue.code for issue in result.issues}
        self.assertIn("previous_state_hash_mismatch", codes)
        self.assertIn("stale_artifact", codes)

    def test_rejects_forged_source_range_and_stale_through_event(self) -> None:
        candidate = copy.deepcopy(self.candidate)
        candidate["through_event_id"] = "event-first"
        forged = FrozenSourceRange(
            first_event_id="event-first",
            first_record_sha256=HASH_A,
            last_event_id="event-last",
            last_record_sha256=HASH_C,
            retained_tail_event_id="event-tail",
        )
        result = validate_compaction_proposal(
            self.previous, candidate, forged, self.resolver
        )
        codes = {issue.code for issue in result.issues}
        self.assertIn("source_hash_mismatch", codes)
        self.assertIn("stale_through_event", codes)

    def test_rejects_source_range_that_does_not_advance_state(self) -> None:
        candidate = copy.deepcopy(self.candidate)
        candidate["through_event_id"] = "event-previous"
        stale_range = FrozenSourceRange(
            first_event_id="event-first",
            first_record_sha256=HASH_B,
            last_event_id="event-previous",
            last_record_sha256=HASH_A,
            retained_tail_event_id="event-tail",
        )
        result = validate_compaction_proposal(
            self.previous, candidate, stale_range, self.resolver
        )
        self.assertFalse(result.accepted)
        self.assertIn("stale_through_event", {issue.code for issue in result.issues})

    def test_requires_evidence_for_newly_cleared_blocker(self) -> None:
        candidate = copy.deepcopy(self.candidate)
        candidate["blockers"][0]["evidence_ids"] = []  # type: ignore[index]
        result = validate_compaction_proposal(
            self.previous, candidate, source_range(), self.resolver
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.issues[0].code, "invalid_candidate_state")
        self.assertIn("must prove a cleared blocker", result.issues[0].message)

        carried_only = copy.deepcopy(self.candidate)
        carried_only["blockers"][0]["evidence_ids"] = ["evidence-blocked"]  # type: ignore[index]
        result = validate_compaction_proposal(
            self.previous, carried_only, source_range(), self.resolver
        )
        self.assertFalse(result.accepted)
        self.assertIn("unproven_clearance", {issue.code for issue in result.issues})


if __name__ == "__main__":
    unittest.main()
