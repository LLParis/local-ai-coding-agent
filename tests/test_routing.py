from __future__ import annotations

import copy
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_continuity.routing import (  # noqa: E402
    OUTCOME_STATUSES,
    RoutingManifestError,
    RoutingRequest,
    RoutingRequestError,
    RuntimeState,
    adjudicate_qualification_status,
    load_routing_manifest,
    select_route,
    validate_routing_manifest,
)

MANIFEST_PATH = ROOT / "config" / "routing-v1.json"


def _raw_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _runtime(manifest, *, cloud: bool, tags: tuple[str, ...] = ()) -> RuntimeState:
    return RuntimeState(
        cloud_available=cloud,
        operable_model_ids=frozenset(manifest.models),
        operable_harness_ids=frozenset(manifest.harnesses),
        available_edge_ids=frozenset(manifest.edges),
        satisfied_runtime_tags=frozenset(tags),
    )


def _request(
    *,
    task_id: str = "route-test",
    role: str = "implement",
    capability: str = "scoped_edit",
    language: str = "python",
    context_tokens: int = 16000,
    tool_needs: tuple[str, ...] = ("repo_inspection", "structured_edit", "project_test"),
    mode: str = "production",
    candidate_route_id: str | None = None,
) -> RoutingRequest:
    return RoutingRequest(
        task_id=task_id,
        role=role,
        capability=capability,
        language=language,
        context_tokens=context_tokens,
        tool_needs=tool_needs,
        mode=mode,
        candidate_route_id=candidate_route_id,
    )


def _route(raw: dict, route_id: str) -> dict:
    return next(item for item in raw["routes"] if item["id"] == route_id)


class RoutingManifestTest(unittest.TestCase):
    def test_checked_in_manifest_verifies_every_evidence_hash(self) -> None:
        manifest = load_routing_manifest(MANIFEST_PATH)
        self.assertEqual(manifest.schema_version, 1)
        self.assertEqual(manifest.policy.automatic_retries, 0)
        self.assertEqual(set(manifest.policy.production_eligible_statuses), {"accepted"})
        self.assertEqual(len(manifest.routes), 14)
        self.assertEqual(
            manifest.sha256,
            "sha256:07b583e8f3b50ccbb05f69a528c31b0f4b90b127eee82c6740f86cc602ab75df",
        )

    def test_component_roles_are_not_collapsed(self) -> None:
        manifest = load_routing_manifest(MANIFEST_PATH)
        route = manifest.routes["local-qwen38-q6-bounded"]
        self.assertEqual(route.model_ids, ("qwen3.8-27b-q6",))
        self.assertEqual(route.harness_id, "thin-structured-edit")
        self.assertEqual(route.verifier_id, "devstral-local-verifier")
        self.assertEqual(route.inference_edge_id, "excalibur")
        self.assertEqual(route.execution_edge_id, "excalibur")
        verifier = manifest.verifiers[route.verifier_id]
        self.assertEqual(verifier.model_id, "devstral-small-2-24b")
        self.assertNotEqual(route.model_ids[0], verifier.model_id)

    def test_evidence_hash_mutation_fails_closed(self) -> None:
        raw = _raw_manifest()
        raw["evidence"][0]["sha256"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(RoutingManifestError, "evidence hash mismatch"):
            validate_routing_manifest(raw, evidence_root=ROOT, verify_evidence=True)

    def test_schema_and_retry_mutations_fail_closed(self) -> None:
        raw = _raw_manifest()
        raw["unexpected"] = True
        with self.assertRaisesRegex(RoutingManifestError, "unexpected fields"):
            validate_routing_manifest(raw)

        raw = _raw_manifest()
        raw["policy"]["automatic_retries"] = 1
        with self.assertRaisesRegex(RoutingManifestError, "exactly zero"):
            validate_routing_manifest(raw)

        raw = _raw_manifest()
        raw["schema_version"] = True
        with self.assertRaisesRegex(RoutingManifestError, "schema_version"):
            validate_routing_manifest(raw)

        raw = _raw_manifest()
        raw["policy"]["automatic_retries"] = False
        with self.assertRaisesRegex(RoutingManifestError, "exactly zero"):
            validate_routing_manifest(raw)

    def test_rejected_verifier_cannot_back_an_accepted_implementation_route(self) -> None:
        raw = _raw_manifest()
        _route(raw, "local-qwen38-q6-bounded")["verifier_id"] = "gemma4-local-verifier"
        with self.assertRaisesRegex(RoutingManifestError, "accepted verifier"):
            validate_routing_manifest(raw)

    def test_verifier_model_must_advertise_the_verify_role(self) -> None:
        raw = _raw_manifest()
        devstral = next(
            item for item in raw["components"]["models"] if item["id"] == "devstral-small-2-24b"
        )
        devstral["roles"] = ["implement"]
        raw["routes"] = [
            item for item in raw["routes"] if item["id"] != "local-devstral-verification"
        ]
        raw["policy"]["sovereign_route_ids"].remove("local-devstral-verification")
        with self.assertRaisesRegex(RoutingManifestError, "without the verify role"):
            validate_routing_manifest(raw)

    def test_sovereign_policy_cannot_name_a_cloud_dependent_route(self) -> None:
        raw = _raw_manifest()
        raw["policy"]["sovereign_route_ids"].append("cloud-codex-primary")
        with self.assertRaisesRegex(RoutingManifestError, "no cloud dependency"):
            validate_routing_manifest(raw)

    def test_xcode_route_cannot_be_redirected_away_from_mac_edge(self) -> None:
        raw = _raw_manifest()
        excalibur = next(item for item in raw["components"]["edges"] if item["id"] == "excalibur")
        excalibur["capabilities"].extend(["swift_toolchain", "xcode"])
        _route(raw, "local-qwen38-q6-apple")["execution_edge_id"] = "excalibur"
        with self.assertRaisesRegex(RoutingManifestError, "send Xcode work to the Mac"):
            validate_routing_manifest(raw)

    def test_accepted_team_requires_single_baseline_positive_gain_and_comparison(self) -> None:
        raw = _raw_manifest()
        team = copy.deepcopy(_route(raw, "local-qwen38-q6-bounded"))
        team.update(
            {
                "id": "team-unproven",
                "name": "Unproven team",
                "composition": "team",
                "model_ids": ["qwen3.8-27b-q6", "gpt-oss-20b"],
                "baseline_route_id": "local-qwen38-q6-bounded",
                "team_gain_basis_points": None,
                "comparison_evidence_ids": [],
            }
        )
        raw["routes"].append(team)
        with self.assertRaisesRegex(RoutingManifestError, "comparative evidence"):
            validate_routing_manifest(raw)

    def test_equivalent_team_task_surface_order_is_not_overconstrained(self) -> None:
        raw = _raw_manifest()
        team = copy.deepcopy(_route(raw, "local-qwen38-q6-bounded"))
        team.update(
            {
                "id": "team-equivalent-order",
                "name": "Equivalent ordered team",
                "composition": "team",
                "model_ids": ["qwen3.8-27b-q6", "gpt-oss-20b"],
                "roles": list(reversed(team["roles"])),
                "capabilities": list(reversed(team["capabilities"])),
                "languages": list(reversed(team["languages"])),
                "tool_needs": list(reversed(team["tool_needs"])),
                "baseline_route_id": "local-qwen38-q6-bounded",
                "team_gain_basis_points": 1,
                "comparison_evidence_ids": ["tournament-v1"],
            }
        )
        raw["routes"].append(team)
        manifest = validate_routing_manifest(raw)
        self.assertIn("team-equivalent-order", manifest.routes)

    def test_manifest_order_does_not_change_equivalent_route(self) -> None:
        raw = _raw_manifest()
        normal = validate_routing_manifest(raw)
        raw["routes"].reverse()
        raw["components"]["models"].reverse()
        shuffled = validate_routing_manifest(raw)
        first = select_route(normal, _request(), _runtime(normal, cloud=False))
        second = select_route(shuffled, _request(), _runtime(shuffled, cloud=False))
        self.assertEqual(first.selected_route_id, second.selected_route_id)
        self.assertEqual(first.request_sha256, second.request_sha256)
        self.assertEqual(first.runtime_sha256, second.runtime_sha256)

    def test_verified_manifest_is_detached_and_deeply_immutable(self) -> None:
        raw = _raw_manifest()
        manifest = validate_routing_manifest(raw)
        original_hash = manifest.sha256
        original_status = manifest.routes["candidate-qwen38-pi"].qualification_status

        _route(raw, "candidate-qwen38-pi")["qualification_status"] = "accepted"
        raw["components"]["models"][0]["roles"].append("forged-role")
        self.assertEqual(manifest.sha256, original_hash)
        self.assertEqual(
            manifest.routes["candidate-qwen38-pi"].qualification_status,
            original_status,
        )
        self.assertNotIn("forged-role", manifest.models["hosted-codex-5.6-sol"].roles)

        with self.assertRaises(TypeError):
            manifest.routes["candidate-qwen38-pi"] = replace(  # type: ignore[index]
                manifest.routes["candidate-qwen38-pi"],
                qualification_status="accepted",
            )
        with self.assertRaises(TypeError):
            manifest.evidence["forged"] = manifest.evidence["operating-contract"]  # type: ignore[index]

    def test_decision_evidence_map_cannot_be_mutated_after_hash_bound_selection(self) -> None:
        manifest = load_routing_manifest(MANIFEST_PATH)
        decision = select_route(manifest, _request(), _runtime(manifest, cloud=False))
        with self.assertRaises(TypeError):
            decision.evidence_sha256["forged"] = "sha256:" + "0" * 64  # type: ignore[index]
        rendered = decision.to_dict()
        rendered["evidence_sha256"]["forged"] = "sha256:" + "0" * 64
        self.assertNotIn("forged", decision.evidence_sha256)


class RoutingDecisionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_routing_manifest(MANIFEST_PATH)

    def test_hosted_codex_is_primary_when_available(self) -> None:
        decision = select_route(
            self.manifest,
            _request(),
            _runtime(self.manifest, cloud=True),
        )
        self.assertEqual(decision.status, "routed")
        self.assertEqual(decision.selected_route_id, "cloud-codex-primary")
        self.assertEqual(decision.model_ids, ("hosted-codex-5.6-sol",))
        self.assertIsNone(decision.fallback_reason_code)
        self.assertEqual(decision.automatic_retries, 0)

    def test_malformed_candidate_identifier_fails_as_a_request_error(self) -> None:
        with self.assertRaisesRegex(RoutingRequestError, "candidate_route_id"):
            _request(
                mode="qualification",
                candidate_route_id=7,  # type: ignore[arg-type]
            )

    def test_cloud_unavailable_uses_sovereign_qwen_and_exact_fallback_reason(self) -> None:
        decision = select_route(
            self.manifest,
            _request(),
            _runtime(self.manifest, cloud=False),
        )
        self.assertEqual(decision.selected_route_id, "local-qwen38-q6-bounded")
        self.assertEqual(decision.model_ids, ("qwen3.8-27b-q6",))
        self.assertEqual(decision.harness_id, "thin-structured-edit")
        self.assertEqual(decision.verifier_id, "devstral-local-verifier")
        self.assertEqual(decision.fallback_from_route_id, "cloud-codex-primary")
        self.assertEqual(decision.fallback_reason_code, "cloud_unavailable")
        self.assertIn("cloud_unavailable", decision.fallback_detail_codes)
        self.assertTrue(decision.evidence_ids)
        self.assertEqual(set(decision.evidence_ids), set(decision.evidence_sha256))
        self.assertTrue(
            all(value.startswith("sha256:") for value in decision.evidence_sha256.values())
        )

    def test_local_route_has_no_cloud_dependency(self) -> None:
        decision = select_route(
            self.manifest,
            _request(),
            _runtime(self.manifest, cloud=False),
        )
        self.assertEqual(
            self.manifest.models[decision.model_ids[0]].locality,
            "local",
        )
        self.assertEqual(self.manifest.harnesses[decision.harness_id].locality, "local")
        self.assertEqual(self.manifest.edges[decision.inference_edge_id].locality, "local")
        self.assertEqual(self.manifest.edges[decision.execution_edge_id].locality, "local")
        verifier = self.manifest.verifiers[decision.verifier_id]
        self.assertEqual(self.manifest.models[verifier.model_id].locality, "local")

    def test_swift_xcode_routes_to_mac_edge_with_cloud_or_local_intelligence(self) -> None:
        request = _request(
            language="swift",
            tool_needs=(
                "repo_inspection",
                "structured_edit",
                "project_test",
                "swift_toolchain",
                "xcode",
            ),
        )
        hosted = select_route(self.manifest, request, _runtime(self.manifest, cloud=True))
        local = select_route(self.manifest, request, _runtime(self.manifest, cloud=False))
        self.assertEqual(hosted.selected_route_id, "cloud-codex-apple")
        self.assertEqual(hosted.execution_edge_id, "mac-apple")
        self.assertEqual(hosted.fallback_reason_code, "cloud_primary_incompatible")
        self.assertEqual(local.selected_route_id, "local-qwen38-q6-apple")
        self.assertEqual(local.execution_edge_id, "mac-apple")
        self.assertEqual(local.inference_edge_id, "excalibur")

    def test_unproven_native_context_is_not_silently_promoted(self) -> None:
        decision = select_route(
            self.manifest,
            _request(context_tokens=100000),
            _runtime(
                self.manifest,
                cloud=False,
                tags=("qwen-native-final-matrix-pass",),
            ),
        )
        self.assertEqual(decision.status, "unroutable")
        self.assertEqual(decision.fallback_reason_code, "no_accepted_compatible_route")
        native = next(
            item for item in decision.considered if item.route_id == "candidate-qwen38-q6-native"
        )
        self.assertIn("status_inconclusive", native.reason_codes)

    def test_pi_deepseek_laguna_and_rejected_gemma_remain_visible_but_unpromoted(self) -> None:
        request = _request(
            capability="interactive_agent",
            tool_needs=("read", "search", "edit", "test"),
        )
        decision = select_route(
            self.manifest,
            request,
            _runtime(
                self.manifest,
                cloud=False,
                tags=(
                    "pi-live-model-gate",
                    "deepseek-reviewed-four-tool-plugin",
                    "laguna-runtime-template-gate",
                ),
            ),
        )
        self.assertEqual(decision.status, "unroutable")
        considered = {item.route_id: item for item in decision.considered}
        self.assertIn("status_not_run", considered["candidate-qwen38-pi"].reason_codes)
        self.assertIn(
            "status_runtime_blocked", considered["candidate-qwen38-deepseek"].reason_codes
        )
        self.assertIn("candidate-laguna-thin", considered)
        self.assertIn("candidate-gemma4-verifier", considered)
        self.assertIn("status_rejected", considered["candidate-gemma4-verifier"].reason_codes)

    def test_exact_qualification_candidate_can_run_without_production_authority(
        self,
    ) -> None:
        request = _request(
            mode="qualification",
            candidate_route_id="candidate-qwen38-pi",
            capability="interactive_agent",
            tool_needs=("read", "search", "edit", "test"),
        )
        blocked = select_route(
            self.manifest,
            request,
            _runtime(self.manifest, cloud=False),
        )
        self.assertEqual(blocked.status, "unroutable")
        self.assertEqual(blocked.fallback_reason_code, "qualification_candidate_unavailable")
        self.assertIn("runtime_tag_missing:pi-live-model-gate", blocked.fallback_detail_codes)

        ready = select_route(
            self.manifest,
            request,
            _runtime(self.manifest, cloud=False, tags=("pi-live-model-gate",)),
        )
        self.assertEqual(ready.status, "routed")
        self.assertEqual(ready.selected_route_id, "candidate-qwen38-pi")
        self.assertEqual(
            self.manifest.routes[ready.selected_route_id].qualification_status,
            "not_run",
        )

    def test_evaluator_invalid_candidate_remains_visible_and_requalifiable(self) -> None:
        raw = _raw_manifest()
        _route(raw, "candidate-qwen38-pi")["qualification_status"] = "evaluator_invalid"
        manifest = validate_routing_manifest(raw)
        production_request = _request(
            capability="interactive_agent",
            tool_needs=("read", "search", "edit", "test"),
        )
        production = select_route(
            manifest,
            production_request,
            _runtime(manifest, cloud=False, tags=("pi-live-model-gate",)),
        )
        consideration = next(
            item for item in production.considered if item.route_id == "candidate-qwen38-pi"
        )
        self.assertIn("status_evaluator_invalid", consideration.reason_codes)

        qualification = select_route(
            manifest,
            _request(
                mode="qualification",
                candidate_route_id="candidate-qwen38-pi",
                capability="interactive_agent",
                tool_needs=("read", "search", "edit", "test"),
            ),
            _runtime(manifest, cloud=False, tags=("pi-live-model-gate",)),
        )
        self.assertEqual(qualification.selected_route_id, "candidate-qwen38-pi")

    def test_verification_routes_to_hosted_codex_then_devstral_when_cloud_is_absent(self) -> None:
        request = _request(
            role="verify",
            capability="verification",
            language="python",
            tool_needs=("diff_review", "project_test"),
        )
        hosted = select_route(self.manifest, request, _runtime(self.manifest, cloud=True))
        local = select_route(self.manifest, request, _runtime(self.manifest, cloud=False))
        self.assertEqual(hosted.selected_route_id, "cloud-codex-primary")
        self.assertEqual(local.selected_route_id, "local-devstral-verification")
        self.assertEqual(local.model_ids, ("devstral-small-2-24b",))

    def test_runtime_snapshot_unknown_component_fails_closed(self) -> None:
        runtime = RuntimeState(
            cloud_available=False,
            operable_model_ids=frozenset({"invented-model"}),
            operable_harness_ids=frozenset(self.manifest.harnesses),
            available_edge_ids=frozenset(self.manifest.edges),
        )
        with self.assertRaisesRegex(RoutingRequestError, "unknown components"):
            select_route(self.manifest, _request(), runtime)

    def test_runtime_snapshot_detaches_from_mutable_caller_sets(self) -> None:
        models = set(self.manifest.models)
        runtime = RuntimeState(
            cloud_available=False,
            operable_model_ids=models,
            operable_harness_ids=set(self.manifest.harnesses),
            available_edge_ids=set(self.manifest.edges),
        )
        models.clear()
        self.assertIsInstance(runtime.operable_model_ids, frozenset)
        decision = select_route(self.manifest, _request(), runtime)
        self.assertEqual(decision.selected_route_id, "local-qwen38-q6-bounded")

        with self.assertRaisesRegex(RoutingRequestError, "must be a set"):
            RuntimeState(
                cloud_available=False,
                operable_model_ids=list(self.manifest.models),  # type: ignore[arg-type]
                operable_harness_ids=frozenset(self.manifest.harnesses),
                available_edge_ids=frozenset(self.manifest.edges),
            )

    def test_proven_team_may_beat_but_cannot_erase_best_single_baseline(self) -> None:
        raw = _raw_manifest()
        team = copy.deepcopy(_route(raw, "local-qwen38-q6-bounded"))
        team.update(
            {
                "id": "team-proven-qwen-gptoss",
                "name": "Proven bounded team",
                "composition": "team",
                "model_ids": ["qwen3.8-27b-q6", "gpt-oss-20b"],
                "priority": 999,
                "baseline_route_id": "local-qwen38-q6-bounded",
                "team_gain_basis_points": 100,
                "comparison_evidence_ids": ["tournament-v1"],
            }
        )
        raw["routes"].append(team)
        manifest = validate_routing_manifest(raw)
        decision = select_route(manifest, _request(), _runtime(manifest, cloud=False))
        self.assertEqual(decision.selected_route_id, "team-proven-qwen-gptoss")
        self.assertIn("local-qwen38-q6-bounded", manifest.routes)
        self.assertEqual(
            manifest.routes["team-proven-qwen-gptoss"].baseline_route_id,
            "local-qwen38-q6-bounded",
        )


class QualificationVerdictIntegrityTest(unittest.TestCase):
    def test_all_required_terminal_outcomes_are_distinct(self) -> None:
        self.assertEqual(
            OUTCOME_STATUSES,
            {
                "accepted",
                "rejected",
                "inconclusive",
                "evaluator_invalid",
                "runtime_blocked",
                "not_run",
            },
        )

    def test_invalid_inconclusive_and_blocked_observations_do_not_demote(self) -> None:
        for outcome in (
            "evaluator_invalid",
            "inconclusive",
            "runtime_blocked",
            "not_run",
        ):
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    adjudicate_qualification_status(
                        "accepted",
                        outcome,
                        evaluator_valid=False,
                        applicable=False,
                        evidence_ids=(),
                    ),
                    "accepted",
                )

    def test_rejection_requires_valid_applicable_evaluator_and_evidence(self) -> None:
        with self.assertRaisesRegex(RoutingRequestError, "valid applicable"):
            adjudicate_qualification_status(
                "accepted",
                "rejected",
                evaluator_valid=False,
                applicable=True,
                evidence_ids=("tournament-v1",),
            )
        with self.assertRaisesRegex(RoutingRequestError, "evidence IDs"):
            adjudicate_qualification_status(
                "accepted",
                "rejected",
                evaluator_valid=True,
                applicable=True,
                evidence_ids=(),
            )
        self.assertEqual(
            adjudicate_qualification_status(
                "accepted",
                "rejected",
                evaluator_valid=True,
                applicable=True,
                evidence_ids=("tournament-v1",),
            ),
            "rejected",
        )

    def test_malformed_evaluator_flags_and_duplicate_evidence_fail_closed(self) -> None:
        with self.assertRaisesRegex(RoutingRequestError, "must be boolean"):
            adjudicate_qualification_status(
                "accepted",
                "rejected",
                evaluator_valid=1,  # type: ignore[arg-type]
                applicable=True,
                evidence_ids=("tournament-v1",),
            )
        with self.assertRaisesRegex(RoutingRequestError, "duplicate-free tuple"):
            adjudicate_qualification_status(
                "accepted",
                "rejected",
                evaluator_valid=True,
                applicable=True,
                evidence_ids=("tournament-v1", "tournament-v1"),
            )


if __name__ == "__main__":
    unittest.main()
