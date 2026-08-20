from __future__ import annotations

import contextlib
import copy
import http.server
import io
import json
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
LONG_CONTEXT = PACKAGE / "evals" / "long_context"
sys.path.insert(0, str(LONG_CONTEXT))

import live_runner  # noqa: E402
from suite import (  # noqa: E402
    OUTPUT_SCHEMA,
    ROOT,
    SuiteError,
    aggregate_qualification,
    build_case,
    load_manifest,
    score_runner_output,
    whitespace_token_count,
    write_case,
)


def _empty_response() -> dict:
    return {
        "status": "completed",
        "citations": [],
        "facts": {},
        "rejected_claim_ids": [],
        "temporal_answers": {},
        "active_constraint_ids": [],
        "next_action_ids": [],
        "diagnosis_code": None,
        "edit": None,
        "verification": None,
        "tool_trace": [],
        "resume_state": None,
        "duplicate_effect_ids": [],
    }


def _depth_candidate(*, correct: bool = True) -> dict:
    expected = next(
        item["expected"]
        for item in load_manifest()["families"]
        if item["id"] == "depth_retrieval"
    )
    response = _empty_response()
    response["citations"] = list(expected["required_citations"])
    response["facts"] = copy.deepcopy(expected["facts"])
    response["rejected_claim_ids"] = list(expected["rejected_claim_ids"])
    if not correct:
        response["facts"]["continuity_codename"]["value"] = "ORBITAL-CEDAR-713"
    return response


def _fake_chat_tokens(payload: dict) -> int:
    messages = payload.get("messages", [])
    return 17 + sum(
        whitespace_token_count(message.get("content", ""))
        for message in messages
        if isinstance(message, dict)
    )


class FakeLlamaHandler(http.server.BaseHTTPRequestHandler):
    server: socketserver.TCPServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write(self, status: int, value: object) -> None:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(raw)

    def _state(self) -> dict:
        return self.server.state  # type: ignore[attr-defined, no-any-return]

    def do_GET(self) -> None:
        state = self._state()
        if self.path == "/health":
            self._write(200, {"status": "ok"})
        elif self.path == "/v1/models":
            self._write(200, {"data": [{"id": state["listed_model"]}]})
        elif self.path == "/props":
            self._write(
                200,
                {
                    "default_generation_settings": {
                        "n_ctx": state["n_ctx"],
                        "params": {
                            "speculative.types": (
                                "draft-mtp" if state["speculative"] else "none"
                            )
                        },
                    },
                    "total_slots": 1,
                    "model_path": "D:/models/Qwen3.8-27B-Q6_K.gguf",
                    "chat_template": "fake-qwen-template-v1",
                    "modalities": {"vision": False},
                    "build_info": "b10435-9e40df63b",
                },
            )
        else:
            self._write(404, {"error": "not found"})

    def do_POST(self) -> None:
        state = self._state()
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        if self.path == "/tokenize":
            state["tokenize_calls"] += 1
            count = whitespace_token_count(payload.get("content", ""))
            self._write(200, {"tokens": list(range(count))})
            return
        if self.path == "/v1/chat/completions/input_tokens":
            if state["input_tokens_404"]:
                self._write(404, {"error": "unsupported"})
                return
            state["token_count_calls"] += 1
            user_content = payload.get("messages", [{}, {}])[-1].get("content", "")
            key = str(hash(user_content))
            state["count_seen"][key] = state["count_seen"].get(key, 0) + 1
            drift = int(
                state["recount_drift"]
                and "END OF CASE" in user_content
                and state["count_seen"][key] >= 3
            )
            self._write(
                200,
                {
                    "object": "response.input_tokens",
                    "input_tokens": _fake_chat_tokens(payload) + drift,
                },
            )
            return
        if self.path == "/apply-template":
            messages = payload.get("messages", [])
            content = " ".join(
                message.get("content", "") for message in messages if isinstance(message, dict)
            )
            self._write(200, {"prompt": ("template " * 17) + content})
            return
        if self.path == "/v1/chat/completions":
            state["completion_calls"] += 1
            if state["delay_seconds"]:
                time.sleep(state["delay_seconds"])
            content = state["content"]
            prompt_tokens = _fake_chat_tokens(payload) + state["usage_drift"]
            self._write(
                200,
                {
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": content,
                                "reasoning_content": "",
                            },
                        }
                    ],
                    "model": state["response_model"],
                    "system_fingerprint": state["fingerprint"],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": whitespace_token_count(content),
                        "total_tokens": prompt_tokens + whitespace_token_count(content),
                    },
                    "timings": {
                        "prompt_ms": 100.0,
                        "predicted_per_token_ms": 20.0,
                        "prompt_per_second": 1000.0,
                        "predicted_per_second": 50.0,
                    },
                },
            )
            return
        self._write(404, {"error": "not found"})


class FakeLlamaServer:
    def __init__(self, **overrides: object) -> None:
        model = "arm-qwen38-q6-native-262k"
        self.state = {
            "listed_model": model,
            "response_model": model,
            "n_ctx": 262144,
            "speculative": False,
            "fingerprint": "b10435-9e40df63b",
            "content": json.dumps(_depth_candidate(), separators=(",", ":")),
            "usage_drift": 0,
            "recount_drift": False,
            "input_tokens_404": False,
            "delay_seconds": 0.0,
            "token_count_calls": 0,
            "tokenize_calls": 0,
            "completion_calls": 0,
            "count_seen": {},
        }
        self.state.update(overrides)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLlamaHandler)
        self.server.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> FakeLlamaServer:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def valid_result(case: object, *, phase: str = "measured", trial_index: int = 1) -> dict:
    runner_input = case.runner_input  # type: ignore[attr-defined]
    oracle = case.oracle  # type: ignore[attr-defined]
    expected = oracle["expected"]
    family = runner_input["family"]
    response = {
        "status": "completed",
        "citations": list(expected["required_citations"]),
        "facts": {},
        "rejected_claim_ids": [],
        "temporal_answers": {},
        "active_constraint_ids": [],
        "next_action_ids": [],
        "diagnosis_code": None,
        "edit": None,
        "verification": None,
        "tool_trace": [],
        "resume_state": None,
        "duplicate_effect_ids": [],
    }
    if family == "depth_retrieval":
        response["facts"] = copy.deepcopy(expected["facts"])
        response["rejected_claim_ids"] = list(expected["rejected_claim_ids"])
    elif family == "temporal_decision":
        response["temporal_answers"] = copy.deepcopy(expected["temporal_answers"])
        response["active_constraint_ids"] = list(expected["active_constraint_ids"])
        response["next_action_ids"] = list(expected["next_action_ids"])
    elif family == "repository_diagnosis":
        response["diagnosis_code"] = expected["diagnosis_code"]
        response["edit"] = {
            "path": expected["edit_path"],
            "before_sha256": expected["before_sha256"],
            "after_sha256": expected["after_sha256"],
        }
        response["verification"] = {
            "command_id": expected["verification_command_id"],
            "passed": True,
            "exit_code": 0,
        }
    elif family == "tool_contract":
        response["tool_trace"] = [
            {
                "sequence": index,
                "call_id": f"call-{index}",
                "tool": item["tool"],
                "arguments": copy.deepcopy(item["arguments"]),
                "outcome": "success",
                "side_effect_id": item["side_effect_id"],
            }
            for index, item in enumerate(expected["tool_calls"], start=1)
        ]
    elif family == "resume_compaction":
        response["resume_state"] = copy.deepcopy(expected["resume_state"])
    else:  # pragma: no cover - manifest validation owns this branch
        raise AssertionError(family)

    return {
        "schema": OUTPUT_SCHEMA,
        "suite_id": runner_input["suite_id"],
        "case_id": runner_input["case_id"],
        "phase": phase,
        "trial_index": trial_index,
        "model": {
            "family": "qwen3.8-27b",
            "provider": "llama.cpp",
            "model_id": "qwen3.8-27b-q6",
            "quantization": "Q6_K",
        },
        "runtime": {
            "backend": "llama.cpp",
            "build": "b10435",
            "context_tokens": 262144,
            "kv_cache": "q4_0",
            "mtp": False,
        },
        "prompt": {
            "sha256": runner_input["prompt"]["sha256"],
            "token_count": runner_input["prompt"]["token_count"],
        },
        "response": response,
        "telemetry": {
            "request_tokens": runner_input["prompt"]["token_count"],
            "reasoning_tokens": 100,
            "output_tokens": 200,
            "ttft_ms": 10.0,
            "wall_ms": 20.0,
            "prompt_tokens_per_second": 1000.0,
            "decode_tokens_per_second": 50.0,
            "peak_vram_mib": 30163,
            "peak_ram_mib": 12000,
            "backend_warnings": [],
            "backend_restarts": 0,
            "timed_out": False,
        },
    }


class GenerationTests(unittest.TestCase):
    def test_manifest_freezes_five_families_and_three_targets(self) -> None:
        manifest = load_manifest()
        self.assertEqual(
            [item["prompt_tokens"] for item in manifest["fill_targets"]],
            [131072, 196608, 235930],
        )
        self.assertEqual(len(manifest["families"]), 5)

    def test_all_fifteen_corpora_hit_exact_targets_and_depths(self) -> None:
        manifest = load_manifest()
        for family in manifest["families"]:
            source_positions = {
                record["source_id"]: record["position"] for record in family["records"]
            }
            for target in manifest["fill_targets"]:
                with self.subTest(family=family["id"], target=target["prompt_tokens"]):
                    case = build_case(
                        family["id"],
                        target["prompt_tokens"],
                        whitespace_token_count,
                        tokenizer_id="test-whitespace-v1",
                    )
                    runner_input = case.runner_input
                    prompt = runner_input["prompt"]
                    self.assertEqual(prompt["token_count"], target["prompt_tokens"])
                    self.assertEqual(
                        whitespace_token_count(prompt["text"]), target["prompt_tokens"]
                    )
                    self.assertIn(f"TASK REQUEST: {family['request']}", prompt["text"])
                    for source_id, position in source_positions.items():
                        start = prompt["needle_positions"][source_id]["start_token"]
                        fraction = start / target["prompt_tokens"]
                        if position == "early":
                            self.assertLess(fraction, 0.20)
                        elif position == "middle":
                            self.assertGreater(fraction, 0.35)
                            self.assertLess(fraction, 0.65)
                        else:
                            self.assertGreater(fraction, 0.75)
                        self.assertIn(source_id, prompt["text"])

    def test_generation_and_immutable_files_are_byte_stable(self) -> None:
        first = build_case(
            "resume_compaction",
            131072,
            whitespace_token_count,
            tokenizer_id="test-whitespace-v1",
        )
        second = build_case(
            "resume_compaction",
            131072,
            whitespace_token_count,
            tokenizer_id="test-whitespace-v1",
        )
        self.assertEqual(first.runner_input, second.runner_input)
        self.assertEqual(first.oracle, second.oracle)
        with tempfile.TemporaryDirectory() as directory:
            input_path, oracle_path = write_case(Path(directory), first)
            original = input_path.read_bytes()
            write_case(Path(directory), second)
            self.assertEqual(input_path.read_bytes(), original)
            self.assertEqual(
                json.loads(oracle_path.read_text())["case_id"], first.oracle["case_id"]
            )
            changed = copy.deepcopy(second.runner_input)
            changed["limits"]["automatic_retries"] = 1
            with self.assertRaisesRegex(SuiteError, "refusing to overwrite"):
                write_case(
                    Path(directory),
                    type(first)(runner_input=changed, oracle=second.oracle),
                )

    def test_repository_fixture_replacement_passes_hidden_test(self) -> None:
        case = build_case(
            "repository_diagnosis",
            131072,
            whitespace_token_count,
            tokenizer_id="test-whitespace-v1",
        )
        expected = case.oracle["expected"]
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "fixture"
            shutil.copytree(ROOT / "fixtures" / "repository_diagnosis", stage)
            (stage / expected["edit_path"]).write_text(expected["replacement"], encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "verify.test_hidden", "-v"],
                cwd=stage,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ScoringTests(unittest.TestCase):
    def test_every_family_golden_result_passes(self) -> None:
        for family in [item["id"] for item in load_manifest()["families"]]:
            with self.subTest(family=family):
                case = build_case(
                    family,
                    131072,
                    whitespace_token_count,
                    tokenizer_id="test-whitespace-v1",
                )
                score = score_runner_output(
                    case.runner_input, case.oracle, valid_result(case)
                )
                self.assertTrue(score["passed"], score["checks"])
                self.assertEqual(score["fraction"], 1.0)

    def test_family_failures_are_deterministically_rejected(self) -> None:
        mutations = {
            "depth_retrieval": lambda result: result["response"]["rejected_claim_ids"].clear(),
            "temporal_decision": lambda result: result["response"]["temporal_answers"].update(
                {"as_of_2026-05-20": {"value": 3, "source_ids": ["SRC-TIME-002"]}}
            ),
            "repository_diagnosis": lambda result: result["response"]["edit"].update(
                {"after_sha256": "sha256:" + "0" * 64}
            ),
            "tool_contract": lambda result: result["response"]["tool_trace"].append(
                copy.deepcopy(result["response"]["tool_trace"][2])
            ),
            "resume_compaction": lambda result: result["response"]["resume_state"][
                "artifacts"
            ].clear(),
        }
        for family, mutate in mutations.items():
            with self.subTest(family=family):
                case = build_case(
                    family,
                    131072,
                    whitespace_token_count,
                    tokenizer_id="test-whitespace-v1",
                )
                result = valid_result(case)
                mutate(result)
                score = score_runner_output(case.runner_input, case.oracle, result)
                self.assertFalse(score["passed"])

    def test_malformed_result_fails_without_throwing(self) -> None:
        case = build_case(
            "depth_retrieval",
            131072,
            whitespace_token_count,
            tokenizer_id="test-whitespace-v1",
        )
        result = valid_result(case)
        del result["telemetry"]["request_tokens"]
        score = score_runner_output(case.runner_input, case.oracle, result)
        self.assertFalse(score["passed"])
        self.assertEqual(score["checks"][0]["id"], "output_contract")

    def test_aggregate_requires_warmup_and_three_measured_trials(self) -> None:
        manifest = load_manifest()
        scores = []
        for family in manifest["families"]:
            for target in manifest["fill_targets"]:
                case = build_case(
                    family["id"],
                    target["prompt_tokens"],
                    whitespace_token_count,
                    tokenizer_id="test-whitespace-v1",
                )
                for phase, trials in (("warmup", (0,)), ("measured", (1, 2, 3))):
                    for trial in trials:
                        result = valid_result(case, phase=phase, trial_index=trial)
                        scores.append(score_runner_output(case.runner_input, case.oracle, result))
        report = aggregate_qualification(scores)
        self.assertTrue(report["qualified"])
        self.assertEqual(report["expected_runs"], 60)
        self.assertEqual(report["measured_runs"], 45)

        incomplete = aggregate_qualification(scores[:-1])
        self.assertFalse(incomplete["qualified"])
        self.assertEqual(len(incomplete["missing"]), 1)


class LiveRunnerTests(unittest.TestCase):
    def _execute(self, server: FakeLlamaServer, **overrides: object) -> dict:
        options = {
            "base_url": server.base_url,
            "model": "arm-qwen38-q6-native-262k",
            "family": "depth_retrieval",
            "target_tokens": 131072,
            "phase": "measured",
            "trial_index": 1,
            "timeout": 10.0,
            "harness_file": None,
        }
        options.update(overrides)
        with mock.patch.object(
            live_runner,
            "_hardware_sample",
            return_value={"gpu_used_mib": 30000, "ram_used_mib": 24000},
        ):
            return live_runner.execute_live_case(**options)

    def test_cli_scores_one_request_and_writes_immutable_run(self) -> None:
        with FakeLlamaServer() as server, tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "run.json"
            stdout = io.StringIO()
            with mock.patch.object(
                live_runner,
                "_hardware_sample",
                return_value={"gpu_used_mib": 30000, "ram_used_mib": 24000},
            ), contextlib.redirect_stdout(stdout):
                exit_code = live_runner.main(
                    [
                        "--base-url",
                        server.base_url,
                        "--family",
                        "depth_retrieval",
                        "--target",
                        "131072",
                        "--output",
                        str(output),
                        "--timeout",
                        "10",
                    ]
                )
            self.assertEqual(exit_code, 0)
            summary = json.loads(stdout.getvalue())
            record = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "completed")
            self.assertTrue(summary["passed"])
            self.assertTrue(record["score"]["passed"])
            self.assertEqual(record["model_calls"], 1)
            self.assertNotIn("text", record["runner_input"]["prompt"])
            self.assertFalse(record["runner_input"]["prompt"]["text_stored"])
            self.assertNotIn("chat_template", record["preflight"]["props"])
            self.assertLess(output.stat().st_size, 100_000)
            self.assertEqual(server.state["completion_calls"], 1)
            self.assertGreater(server.state["token_count_calls"], 1)
            self.assertGreater(server.state["tokenize_calls"], 1)
            with self.assertRaisesRegex(live_runner.RunnerError, "refusing to overwrite"):
                live_runner.write_run(output, {"different": True})

    def test_exact_counter_falls_back_to_template_plus_tokenize(self) -> None:
        with FakeLlamaServer(input_tokens_404=True) as server:
            client = live_runner.LlamaClient(server.base_url, 5)
            request = live_runner._chat_request(
                "arm-qwen38-q6-text", "three token prompt", "two tokens", 100
            )
            self.assertEqual(client.count_chat(request), 22)
            self.assertEqual(client.token_count_mode, "apply-template+/tokenize")
            self.assertGreater(server.state["tokenize_calls"], 0)

    def test_endpoint_model_and_runtime_mismatch_stop_before_model_call(self) -> None:
        invalid_endpoint = live_runner.execute_live_case(
            base_url="https://127.0.0.1:8818",
            model="arm-qwen38-q6-text",
            family="depth_retrieval",
            target_tokens=131072,
            phase="measured",
            trial_index=1,
            timeout=1,
        )
        self.assertEqual(invalid_endpoint["error"]["code"], "endpoint_mismatch")
        self.assertEqual(invalid_endpoint["model_calls"], 0)

        for overrides, error in (
            ({"listed_model": "other-model"}, "model_mismatch"),
            ({"n_ctx": 32768}, "runtime_mismatch"),
            ({"speculative": True}, "runtime_mismatch"),
        ):
            with self.subTest(error=error, overrides=overrides), FakeLlamaServer(
                **overrides
            ) as server:
                record = self._execute(server)
                self.assertEqual(record["error"]["code"], error)
                self.assertEqual(record["model_calls"], 0)
                self.assertEqual(server.state["completion_calls"], 0)

    def test_inference_token_count_drift_is_rejected_without_retry(self) -> None:
        with FakeLlamaServer(usage_drift=1) as server:
            record = self._execute(server)
            self.assertEqual(record["error"]["code"], "token_count_drift")
            self.assertEqual(record["model_calls"], 1)
            self.assertEqual(server.state["completion_calls"], 1)

    def test_tokenizer_recount_drift_stops_before_inference(self) -> None:
        with FakeLlamaServer(recount_drift=True) as server:
            record = self._execute(server)
            self.assertEqual(record["error"]["code"], "token_count_drift")
            self.assertEqual(record["model_calls"], 0)
            self.assertEqual(server.state["completion_calls"], 0)

    def test_timeout_is_terminal_and_not_retried(self) -> None:
        with FakeLlamaServer(delay_seconds=0.2) as server:
            record = self._execute(server, timeout=0.05)
            self.assertEqual(record["error"]["code"], "timeout")
            self.assertEqual(record["model_calls"], 1)
            self.assertEqual(server.state["completion_calls"], 1)

    def test_malformed_model_response_is_terminal(self) -> None:
        with FakeLlamaServer(content="not-json") as server:
            record = self._execute(server)
            self.assertEqual(record["error"]["code"], "model_output_malformed")
            self.assertEqual(record["model_calls"], 1)
            self.assertEqual(server.state["completion_calls"], 1)

    def test_valid_but_wrong_answer_reaches_scorer_and_fails(self) -> None:
        wrong = json.dumps(_depth_candidate(correct=False), separators=(",", ":"))
        with FakeLlamaServer(content=wrong) as server:
            record = self._execute(server)
            self.assertEqual(record["status"], "completed")
            self.assertFalse(record["passed"])
            self.assertFalse(record["score"]["passed"])
            self.assertEqual(record["model_calls"], 1)
            self.assertEqual(server.state["completion_calls"], 1)


if __name__ == "__main__":
    unittest.main()
