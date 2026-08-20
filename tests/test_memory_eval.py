from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
SUITE_PATH = PACKAGE / "evals" / "memory" / "v1" / "suite.py"
SPEC = importlib.util.spec_from_file_location("memory_eval_v1_suite", SUITE_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("cannot load Memory v1 evaluation suite")
memory_suite = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = memory_suite
SPEC.loader.exec_module(memory_suite)


class MemoryEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.results, cls.report = memory_suite.run_all()
        cls.by_id = {result["case_id"]: result for result in cls.results}

    def test_manifest_freezes_exactly_five_cases_in_each_of_eight_tranches(self) -> None:
        manifest = memory_suite.load_manifest()
        self.assertEqual(manifest["case_count"], 40)
        self.assertEqual(len(manifest["cases"]), 40)
        self.assertEqual(len({case["id"] for case in manifest["cases"]}), 40)
        self.assertEqual(
            Counter(case["tranche"] for case in manifest["cases"]),
            Counter({tranche: 5 for tranche in manifest["tranches"]}),
        )
        required = {
            "seed_events",
            "query",
            "expected_memory_ids",
            "forbidden_memory_ids",
            "expected_state",
            "invariants",
            "evidence_requirements",
            "context_budget",
            "expected_answer_facts",
            "model_assisted_phase_required",
        }
        for case in manifest["cases"]:
            with self.subTest(case=case["id"]):
                self.assertTrue(required.issubset(case))
                self.assertEqual(case["query"]["top_k"], 10)
                self.assertEqual(case["context_budget"], memory_suite._CONTEXT_BUDGET)
                self.assertTrue(case["query"]["scopes"])
                self.assertIn("as_of", case["query"])

    def test_all_forty_cases_execute_and_self_validate_the_evaluator(self) -> None:
        self.assertEqual(len(self.results), 40)
        self.assertEqual(
            Counter(result["terminal_outcome"] for result in self.results),
            Counter({"accepted": 40}),
        )
        for result in self.results:
            with self.subTest(case=result["case_id"]):
                controls = result["evaluator_controls"]
                self.assertTrue(controls["passed"])
                self.assertEqual(
                    set(controls["controls"]),
                    {
                        "positive",
                        "semantic_equivalence",
                        "negative",
                        "malformed",
                        "evaluator_invalid",
                        "mutation",
                    },
                )
                self.assertEqual(
                    controls["controls"]["malformed"]["observed"],
                    "rejected",
                )
                self.assertEqual(
                    controls["controls"]["evaluator_invalid"]["observed"],
                    "evaluator_invalid",
                )
                self.assertEqual(
                    controls["invalid_controls_in_candidate_denominator"], 0
                )

    def test_aggregate_reports_truth_without_promoting_pending_phases(self) -> None:
        report = self.report
        self.assertEqual(report["deterministic_qualification"]["status"], "accepted")
        self.assertTrue(
            report["deterministic_qualification"]["correctness_checks_passed"]
        )
        self.assertTrue(report["deterministic_qualification"]["fully_qualified"])
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual(report["recall"]["exact_hits"], 20)
        self.assertEqual(report["recall"]["exact_total"], 20)
        self.assertEqual(report["recall"]["overall_hits"], report["recall"]["overall_total"])
        self.assertTrue(report["gates"]["exact_recall_at_10"]["passed"])
        self.assertTrue(report["gates"]["overall_recall_at_10"]["passed"])
        self.assertTrue(report["gates"]["scope_leakage"]["passed"])
        self.assertTrue(report["gates"]["fabricated_memory"]["passed"])
        self.assertTrue(report["gates"]["retention_execution"]["passed"])
        self.assertEqual(
            report["results"]["excluded_from_candidate_verdict_denominator"],
            [],
        )
        self.assertEqual(report["model_assisted_qualification"]["status"], "not_run")
        self.assertEqual(report["model_assisted_qualification"]["model_calls_made"], 0)
        self.assertEqual(report["latency_scale_phase"]["status"], "not_run")

    def test_declared_negative_execution_mutations_are_rejected(self) -> None:
        trials = (
            ("scope-01-owner", "include_forbidden_scope", "forbidden_exclusion"),
            (
                "compact-01-many-constraints",
                "omit_compaction_invariant",
                "compaction",
            ),
            (
                "crash-02-side-effect-unknown",
                "duplicate_side_effect",
                "duplicate_side_effects",
            ),
            ("negative-03-no-memory", "fabricate_negative", "no_fabrication"),
            (
                "retention-05-eligible-gc",
                "retention_claim_without_execution",
                "retention_execution",
            ),
        )
        for case_id, mutation, failed_check in trials:
            with self.subTest(case=case_id, mutation=mutation):
                result = memory_suite.run_case(
                    case_id,
                    mutation=mutation,
                    include_controls=False,
                )
                self.assertEqual(result["terminal_outcome"], "rejected")
                self.assertFalse(result["deterministic_checks_passed"])
                self.assertIn(
                    failed_check,
                    {check["id"] for check in result["checks"] if not check["passed"]},
                )

    def test_retention_verdict_classes_and_denominators_are_truthful(self) -> None:
        with mock.patch.object(memory_suite.MemoryStore, "plan_retention", None):
            missing = memory_suite.run_case("retention-05-eligible-gc")
        self.assertEqual(missing["terminal_outcome"], "inconclusive")
        self.assertFalse(missing["retention"]["capability_available"])

        with mock.patch.object(
            memory_suite.MemoryStore,
            "run_gc",
            side_effect=memory_suite.CapabilityError("zstd runtime unavailable"),
        ):
            blocked = memory_suite.run_case("retention-05-eligible-gc")
        self.assertEqual(blocked["terminal_outcome"], "runtime_blocked")
        self.assertTrue(blocked["retention"]["runtime_blocked"])

        rejected = memory_suite.run_case(
            "retention-05-eligible-gc",
            mutation="retention_claim_without_execution",
            include_controls=False,
        )
        self.assertEqual(rejected["terminal_outcome"], "rejected")

        missing_results = [
            missing if result["case_id"] == missing["case_id"] else result
            for result in self.results
        ]
        report = memory_suite.aggregate_results(missing_results)
        self.assertEqual(
            report["deterministic_qualification"]["status"], "inconclusive"
        )
        self.assertEqual(
            report["results"]["candidate_verdict_denominator"], 39
        )
        self.assertEqual(
            report["results"]["excluded_from_candidate_verdict_denominator"],
            ["retention-05-eligible-gc"],
        )

        blocked_results = [
            blocked if result["case_id"] == blocked["case_id"] else result
            for result in self.results
        ]
        blocked_report = memory_suite.aggregate_results(blocked_results)
        self.assertEqual(
            blocked_report["deterministic_qualification"]["status"],
            "runtime_blocked",
        )
        self.assertEqual(
            blocked_report["results"]["candidate_verdict_denominator"], 39
        )

    def test_fresh_process_crashes_leave_one_gap_and_replay_it_once(self) -> None:
        for case_id in (
            "crash-01-before-model",
            "crash-02-side-effect-unknown",
            "crash-03-after-verification",
        ):
            with self.subTest(case=case_id):
                crash = self.by_id[case_id]["crash_replay"]
                self.assertTrue(crash["fresh_process"])
                self.assertEqual(crash["observed_exit_code"], memory_suite.CRASH_EXIT_CODE)
                self.assertEqual(crash["projection_gap_before_reopen"], 1)
                self.assertEqual(crash["replayed_missing_events"], 1)
                self.assertEqual(
                    crash["raw_events_before_reopen"],
                    crash["projected_events_after_reopen"],
                )
        side_effect = self.by_id["crash-02-side-effect-unknown"]["crash_replay"]
        self.assertEqual(
            side_effect["synthetic_effects"],
            [{"effect_id": "effect-copy-1", "attempts": 1}],
        )

    def test_retention_cases_execute_protection_archive_eviction_and_tombstones(self) -> None:
        protected = self.by_id["retention-04-protected-gc"]["retention"]
        eligible = self.by_id["retention-05-eligible-gc"]["retention"]
        for observation in (protected, eligible):
            self.assertEqual(observation["operation_status"], "executed")
            self.assertTrue(observation["capability_available"])
            self.assertTrue(observation["truthful"])
            self.assertTrue(observation["passed"])
            self.assertEqual(observation["dry_run"]["status"], "dry_run")
            self.assertEqual(observation["execution"]["status"], "applied")
            self.assertEqual(observation["dry_run"]["actions_applied"], 0)

        protected_evidence = next(
            item
            for item in protected["decisions"]
            if item["retention_class"] == "evidence"
        )
        self.assertEqual(protected_evidence["action"], "keep")
        self.assertEqual(protected_evidence["reason"], "protected_reference")
        self.assertTrue(protected_evidence["pin_reasons"])
        self.assertEqual(protected["execution"]["actions_applied"], 0)

        eligible_by_class = {
            item["retention_class"]: item for item in eligible["catalog"]
        }
        self.assertEqual(eligible_by_class["bulk"]["availability"], "archived")
        self.assertEqual(eligible_by_class["scratch"]["availability"], "evicted")
        for retention_class in ("bulk", "scratch"):
            row = eligible_by_class[retention_class]
            self.assertIsNotNone(row["tombstone_locator"])
            self.assertIsNotNone(row["tombstone_sha256"])
            self.assertIsNotNone(row["excerpt_json"])
            self.assertIsNotNone(row["event_provenance_json"])
        self.assertIsNotNone(eligible_by_class["bulk"]["archive_locator"])
        self.assertIsNotNone(eligible_by_class["bulk"]["archive_sha256"])
        self.assertEqual(eligible["execution"]["actions_applied"], 2)

    def test_manifest_and_logical_outputs_are_byte_stable(self) -> None:
        lock = memory_suite.verify_suite_lock()
        self.assertEqual(
            lock["manifest_sha256"],
            "sha256:bdfbbdf1400ecbdf174a42da747af67eb8fde122234d9a8ec769b53826f956e0",
        )
        self.assertEqual(
            memory_suite.canonical_bytes(memory_suite.load_manifest()),
            memory_suite.canonical_bytes(memory_suite.load_manifest()),
        )
        first = memory_suite.run_case("exact-05-task-artifact-ids")
        second = memory_suite.run_case("exact-05-task-artifact-ids")
        self.assertEqual(first["store_fingerprint"], second["store_fingerprint"])
        self.assertEqual(
            first["deterministic_fingerprint"], second["deterministic_fingerprint"]
        )
        self.assertEqual(
            memory_suite.qualification_sha256(self.report),
            self.report["qualification_sha256"],
        )

    def test_aggregate_recomputes_failures_instead_of_trusting_pass_labels(self) -> None:
        fabricated = copy.deepcopy(self.results)
        target = next(
            result for result in fabricated if result["case_id"] == "negative-03-no-memory"
        )
        target["passed"] = True
        target["retrieval"]["fabricated_memory_ids"] = ["invented-memory"]
        report = memory_suite.aggregate_results(fabricated)
        self.assertFalse(report["gates"]["fabricated_memory"]["passed"])
        self.assertEqual(report["deterministic_qualification"]["status"], "rejected")

        missing = memory_suite.aggregate_results(self.results[:-1])
        self.assertFalse(missing["gates"]["all_40_cases_executed"]["passed"])
        self.assertIn("retention-05-eligible-gc", missing["results"]["missing_case_ids"])

    def test_model_assisted_phase_is_separate_and_makes_zero_calls(self) -> None:
        required = 0
        for result in self.results:
            phase = result["model_assisted_phase"]
            self.assertEqual(phase["model_calls_made"], 0)
            if phase["required"]:
                required += 1
                self.assertEqual(phase["status"], "not_run")
                self.assertEqual(phase["trials_required"], 3)
            else:
                self.assertEqual(phase["status"], "not_required")
        self.assertGreater(required, 0)


if __name__ == "__main__":
    unittest.main()
