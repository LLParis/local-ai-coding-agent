"""One-shot live runner for the frozen native-262K qualification suite."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from suite import (
    OUTPUT_SCHEMA,
    ROOT,
    GeneratedCase,
    SuiteError,
    build_case,
    load_manifest,
    score_runner_output,
    validate_runner_output,
)

LIVE_RUN_SCHEMA = "coding-intelligence-long-context-live-run/v1"
MAX_HTTP_BYTES = 32 * 1024 * 1024
SYSTEM_PROMPT = (
    "You are executing one frozen long-context qualification task. Follow the supplied "
    "evidence and return exactly one JSON object matching the response contract. Do not "
    "include Markdown or commentary outside JSON."
)


class RunnerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        model_calls: int = 0,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.model_calls = model_calls
        self.details = dict(details or {})


class HttpStatusError(RunnerError):
    def __init__(self, path: str, status: int, body: bytes) -> None:
        super().__init__(
            "http_error",
            f"{path} returned HTTP {status}",
            details={
                "path": path,
                "status": status,
                "body_head": body[:500].decode("utf-8", "replace"),
            },
        )
        self.status = status


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _exact_keys(value: Any, keys: set[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RunnerError("model_output_malformed", f"{path} must be an object", model_calls=1)
    missing = sorted(keys - set(value))
    unknown = sorted(set(value) - keys)
    if missing:
        raise RunnerError(
            "model_output_malformed",
            f"{path} missing keys: {', '.join(missing)}",
            model_calls=1,
        )
    if unknown:
        raise RunnerError(
            "model_output_malformed",
            f"{path} unknown keys: {', '.join(unknown)}",
            model_calls=1,
        )
    return value


class LlamaClient:
    """Minimal loopback-only client with no retry behavior."""

    def __init__(self, base_url: str, timeout: float) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise RunnerError(
                "endpoint_mismatch",
                "base URL must be an HTTP loopback origin without credentials or a path",
            )
        if timeout <= 0:
            raise RunnerError("invalid_configuration", "timeout must be positive")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.base_url = f"http://{self.host}:{self.port}"
        self.timeout = timeout
        self.token_count_mode: str | None = None

    def request(self, method: str, path: str, payload: Any | None = None) -> dict[str, Any]:
        body = None if payload is None else _json_bytes(payload)
        headers = {} if body is None else {"Content-Type": "application/json"}
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_HTTP_BYTES + 1)
        except TimeoutError as exc:
            raise RunnerError("timeout", f"{method} {path} timed out") from exc
        except OSError as exc:
            raise RunnerError("endpoint_unreachable", f"{method} {path} failed: {exc}") from exc
        finally:
            connection.close()
        if len(raw) > MAX_HTTP_BYTES:
            raise RunnerError("response_too_large", f"{path} exceeded {MAX_HTTP_BYTES} bytes")
        if response.status != 200:
            raise HttpStatusError(path, response.status, raw)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunnerError("endpoint_malformed", f"{path} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RunnerError("endpoint_malformed", f"{path} must return a JSON object")
        return value

    def tokenize_text(self, text: str) -> list[Any]:
        value = self.request(
            "POST",
            "/tokenize",
            {"content": text, "add_special": False, "parse_special": True},
        )
        tokens = value.get("tokens")
        if not isinstance(tokens, list):
            raise RunnerError("tokenizer_malformed", "/tokenize response has no token list")
        return tokens

    def count_chat(self, request: Mapping[str, Any]) -> int:
        count_body = dict(request)
        try:
            value = self.request("POST", "/v1/chat/completions/input_tokens", count_body)
        except HttpStatusError as exc:
            if exc.status != 404:
                raise
            template = self.request(
                "POST", "/apply-template", {"messages": count_body.get("messages")}
            )
            prompt = template.get("prompt")
            if not isinstance(prompt, str):
                raise RunnerError(
                    "tokenizer_malformed", "/apply-template response has no prompt"
                )
            self.token_count_mode = "apply-template+/tokenize"
            return len(self.tokenize_text(prompt))
        tokens = value.get("input_tokens")
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise RunnerError(
                "tokenizer_malformed", "chat input-token response is not a non-negative integer"
            )
        self.token_count_mode = "v1/chat/completions/input_tokens"
        return tokens

    def padding_factory(self) -> Any:
        """Discover stable single-token pieces for efficient exact padding."""

        candidates = (
            " archived",
            " telemetry",
            " sample",
            " unrelated",
            " historical",
            " cache",
            " observation",
            " nominal",
            " background",
            " record",
            " context",
        )
        pieces = [candidate for candidate in candidates if len(self.tokenize_text(candidate)) == 1]
        if not pieces:
            raise RunnerError(
                "tokenizer_padding_unavailable",
                "no safe single-token padding pieces were found",
            )
        probe = "".join(pieces * 4)
        if len(self.tokenize_text(probe)) != len(pieces) * 4:
            raise RunnerError(
                "tokenizer_padding_unavailable",
                "single-token pieces were not stable when concatenated",
            )

        def make_padding(token_budget: int, seed: str) -> str:
            offset = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % len(pieces)
            return "".join(pieces[(offset + index) % len(pieces)] for index in range(token_budget))

        return make_padding


def _model_response_schema() -> dict[str, Any]:
    answer = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "value": {"type": ["string", "number", "boolean", "null"]},
            "source_ids": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
        },
        "required": ["value", "source_ids"],
    }
    tool_call = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "sequence": {"type": "integer", "minimum": 1},
            "call_id": {"type": "string", "minLength": 1},
            "tool": {"type": "string", "minLength": 1},
            "arguments": {"type": "object"},
            "outcome": {"enum": ["success", "failed", "not_started", "unknown"]},
            "side_effect_id": {"type": ["string", "null"]},
        },
        "required": ["sequence", "call_id", "tool", "arguments", "outcome", "side_effect_id"],
    }
    resume_tool_call = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "call_id": {"type": "string", "minLength": 1},
            "outcome": {"enum": ["not_started", "unknown"]},
        },
        "required": ["call_id", "outcome"],
    }
    resume_artifact = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        },
        "required": ["path", "sha256"],
    }
    resume_state = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "objective_id": {"type": "string", "minLength": 1},
            "open_success_criteria_ids": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "active_constraint_ids": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "active_blocker_ids": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "open_tool_calls": {"type": "array", "items": resume_tool_call},
            "artifacts": {"type": "array", "items": resume_artifact},
            "next_action_id": {"type": "string", "minLength": 1},
        },
        "required": [
            "objective_id",
            "open_success_criteria_ids",
            "active_constraint_ids",
            "active_blocker_ids",
            "open_tool_calls",
            "artifacts",
            "next_action_id",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"const": "completed"},
            "citations": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "facts": {"type": "object", "additionalProperties": answer},
            "rejected_claim_ids": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "temporal_answers": {"type": "object", "additionalProperties": answer},
            "active_constraint_ids": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "next_action_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "diagnosis_code": {"type": ["string", "null"]},
            "edit": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "path": {"type": "string"},
                            "replacement": {"type": "string"},
                        },
                        "required": ["path", "replacement"],
                    },
                ]
            },
            "verification": {"type": ["object", "null"]},
            "tool_trace": {"type": "array", "items": tool_call},
            "resume_state": {"anyOf": [{"type": "null"}, resume_state]},
            "duplicate_effect_ids": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
        },
        "required": sorted(
            {
                "status",
                "citations",
                "facts",
                "rejected_claim_ids",
                "temporal_answers",
                "active_constraint_ids",
                "next_action_ids",
                "diagnosis_code",
                "edit",
                "verification",
                "tool_trace",
                "resume_state",
                "duplicate_effect_ids",
            }
        ),
    }


def _chat_request(model: str, prompt: str, system_prompt: str, max_tokens: int) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "seed": 8675309,
        "max_tokens": max_tokens,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "long_context_response",
                "strict": True,
                "schema": _model_response_schema(),
            },
        },
    }


def _preflight(client: LlamaClient, model: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    health = client.request("GET", "/health")
    if health.get("status") != "ok":
        raise RunnerError("endpoint_unhealthy", "llama.cpp health status is not ok")
    models = client.request("GET", "/v1/models")
    data = models.get("data")
    model_ids = (
        sorted(
            item.get("id")
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
        if isinstance(data, list)
        else []
    )
    if model not in model_ids:
        raise RunnerError(
            "model_mismatch",
            f"requested model {model!r} is not the exact loaded alias",
            details={"available_model_ids": model_ids},
        )
    props = client.request("GET", "/props")
    defaults = props.get("default_generation_settings")
    if not isinstance(defaults, dict):
        raise RunnerError("runtime_mismatch", "/props lacks default_generation_settings")
    baseline = manifest["baseline"]
    mismatches: dict[str, Any] = {}
    if defaults.get("n_ctx") != manifest["context_window_tokens"]:
        mismatches["context_tokens"] = defaults.get("n_ctx")
    params = defaults.get("params")
    speculative_value = defaults.get("speculative")
    if isinstance(params, dict) and "speculative.types" in params:
        speculative_value = params["speculative.types"]
    speculative_enabled = speculative_value not in {False, "none"}
    if speculative_enabled is not baseline["mtp"]:
        mismatches["speculative"] = speculative_value
    if props.get("total_slots") != 1:
        mismatches["total_slots"] = props.get("total_slots")
    build_info = props.get("build_info")
    if not isinstance(build_info, str) or not build_info.startswith(baseline["backend_build"]):
        mismatches["build_info"] = build_info
    model_path = props.get("model_path")
    if not isinstance(model_path, str) or "qwen3.8-27b-q6_k.gguf" not in model_path.lower():
        mismatches["model_path"] = model_path
    modalities = props.get("modalities")
    if isinstance(modalities, dict) and modalities.get("vision") is not False:
        mismatches["vision"] = modalities.get("vision")
    if mismatches:
        raise RunnerError(
            "runtime_mismatch",
            "loaded llama.cpp runtime does not match the frozen native-Q6 lane",
            details=mismatches,
        )
    return {
        "health": health,
        "model_ids": model_ids,
        "props": props,
        "props_sha256": _sha256(_json_bytes(props)),
        "kv_cache_evidence": {
            "expected": baseline["kv_cache"],
            "source": "suite expectation; llama.cpp /props does not expose KV type",
        },
    }


def _recorded_preflight(preflight: Mapping[str, Any]) -> dict[str, Any]:
    """Keep exact runtime evidence without copying the full chat template per trial."""

    props = preflight["props"]
    defaults = props.get("default_generation_settings", {})
    params = defaults.get("params", {}) if isinstance(defaults, dict) else {}
    chat_template = str(props.get("chat_template", ""))
    return {
        "health": preflight["health"],
        "model_ids": preflight["model_ids"],
        "props_sha256": preflight["props_sha256"],
        "kv_cache_evidence": preflight["kv_cache_evidence"],
        "props": {
            "build_info": props.get("build_info"),
            "model_alias": props.get("model_alias"),
            "model_ftype": props.get("model_ftype"),
            "model_path": props.get("model_path"),
            "total_slots": props.get("total_slots"),
            "modalities": props.get("modalities"),
            "n_ctx": defaults.get("n_ctx") if isinstance(defaults, dict) else None,
            "speculative_types": (
                params.get("speculative.types") if isinstance(params, dict) else None
            ),
            "chat_template_sha256": _sha256(chat_template.encode()),
        },
    }


def _recorded_runner_input(runner_input: Mapping[str, Any]) -> dict[str, Any]:
    """Record reproducible prompt identity without duplicating megabytes of filler."""

    recorded = json.loads(json.dumps(runner_input))
    prompt = recorded["prompt"]
    prompt.pop("text", None)
    prompt["text_stored"] = False
    return recorded


def _hardware_sample() -> dict[str, int | None]:
    gpu_used: int | None = None
    with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            gpu_used = max(int(line.strip()) for line in result.stdout.splitlines() if line.strip())

    ram_used: int | None = None
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        with contextlib.suppress(OSError, ValueError):
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                ram_used = round((status.total_physical - status.available_physical) / 1024**2)
    return {"gpu_used_mib": gpu_used, "ram_used_mib": ram_used}


def _extract_model_text(response: Mapping[str, Any]) -> tuple[str, str]:
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise RunnerError(
            "model_response_malformed", "completion must contain exactly one choice", model_calls=1
        )
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise RunnerError(
            "model_response_malformed", "completion choice has no text content", model_calls=1
        )
    reasoning = message.get("reasoning_content")
    if reasoning is None:
        reasoning = ""
    if not isinstance(reasoning, str):
        raise RunnerError(
            "model_response_malformed", "reasoning_content must be text", model_calls=1
        )
    return message["content"].strip(), reasoning


def _normalize_model_response(
    case: GeneratedCase,
    candidate: Any,
    *,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    response = dict(_exact_keys(candidate, set(_model_response_schema()["required"]), "response"))
    verification_evidence: dict[str, Any] | None = None
    if case.runner_input["family"] != "repository_diagnosis":
        if response["edit"] is not None:
            raise RunnerError(
                "model_output_malformed",
                "only the repository family may return an edit",
                model_calls=1,
            )
        return response, verification_evidence

    edit = _exact_keys(response["edit"], {"path", "replacement"}, "response.edit")
    expected = case.oracle["expected"]
    if edit["path"] != expected["edit_path"]:
        raise RunnerError(
            "out_of_scope_edit",
            f"model attempted edit path {edit['path']!r}",
            model_calls=1,
        )
    replacement = edit["replacement"]
    if (
        not isinstance(replacement, str)
        or not replacement
        or len(replacement.encode("utf-8")) > 65536
    ):
        raise RunnerError("model_output_malformed", "replacement is invalid", model_calls=1)
    fixture_root = ROOT / "fixtures" / "repository_diagnosis"
    with tempfile.TemporaryDirectory(prefix="long-context-code-") as directory:
        stage = Path(directory) / "fixture"
        shutil.copytree(fixture_root, stage)
        target = stage / edit["path"]
        before = target.read_bytes()
        target.write_text(replacement, encoding="utf-8")
        after = target.read_bytes()
        try:
            test = subprocess.run(
                [sys.executable, "-m", "unittest", "verify.test_hidden", "-v"],
                cwd=stage,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerError(
                "verification_timeout", "hidden verifier timed out", model_calls=1
            ) from exc
    response["edit"] = {
        "path": edit["path"],
        "before_sha256": _sha256(before),
        "after_sha256": _sha256(after),
    }
    response["verification"] = {
        "command_id": expected["verification_command_id"],
        "passed": test.returncode == 0,
        "exit_code": test.returncode,
    }
    verification_evidence = {
        "command": [sys.executable, "-m", "unittest", "verify.test_hidden", "-v"],
        "exit_code": test.returncode,
        "output_sha256": _sha256(test.stdout.encode("utf-8")),
        "output": test.stdout,
    }
    return response, verification_evidence


def _safe_harness(path: Path | None) -> tuple[str, dict[str, Any] | None]:
    if path is None:
        return SYSTEM_PROMPT, None
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size > 1024 * 1024:
        raise RunnerError("invalid_configuration", "harness file must be a file <= 1 MiB")
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise RunnerError("invalid_configuration", "harness file must be UTF-8") from exc
    combined = SYSTEM_PROMPT + "\n\n" + text
    return combined, {"path": str(resolved), "sha256": _sha256(text.encode("utf-8"))}


def execute_live_case(
    *,
    base_url: str,
    model: str,
    family: str,
    target_tokens: int,
    phase: str,
    trial_index: int,
    timeout: float,
    harness_file: Path | None = None,
) -> dict[str, Any]:
    """Execute exactly one live model request and return a complete run record."""

    captured = dt.datetime.now(dt.UTC).isoformat()
    trace: dict[str, Any] = {
        "schema": LIVE_RUN_SCHEMA,
        "captured_at_utc": captured,
        "status": "running",
        "model_calls": 0,
        "automatic_retries": 0,
        "configuration": {
            "base_url": base_url,
            "model": model,
            "family": family,
            "target_tokens": target_tokens,
            "phase": phase,
            "trial_index": trial_index,
            "timeout_seconds": timeout,
        },
    }
    try:
        if phase not in {"warmup", "measured"}:
            raise RunnerError("invalid_configuration", "phase must be warmup or measured")
        if (phase == "warmup" and trial_index != 0) or (
            phase == "measured" and trial_index not in {1, 2, 3}
        ):
            raise RunnerError("invalid_configuration", "phase and trial_index do not agree")
        manifest = load_manifest()
        client = LlamaClient(base_url, timeout)
        preflight = _preflight(client, model, manifest)
        trace["preflight"] = _recorded_preflight(preflight)
        system_prompt, harness = _safe_harness(harness_file)
        isolation_marker = hashlib.sha256(
            f"{family}|{target_tokens}|{phase}|{trial_index}".encode()
        ).hexdigest()
        system_prompt = (
            "Run-isolation marker (non-semantic; do not cite): "
            f"{isolation_marker}\n{system_prompt}"
        )
        trace["harness"] = harness
        trace["configuration"]["isolation_marker_sha256"] = _sha256(
            isolation_marker.encode("ascii")
        )
        max_tokens = manifest["reserved_completion_tokens"]

        def exact_count(prompt: str) -> int:
            return client.count_chat(_chat_request(model, prompt, system_prompt, max_tokens))

        tokenizer_id = "|".join(
            (
                str(preflight["props"].get("build_info")),
                str(preflight["props"].get("model_path")),
                _sha256(str(preflight["props"].get("chat_template", "")).encode()),
            )
        )
        case = build_case(
            family,
            target_tokens,
            exact_count,
            tokenizer_id=tokenizer_id,
            padding_factory=client.padding_factory(),
        )
        request = _chat_request(
            model, case.runner_input["prompt"]["text"], system_prompt, max_tokens
        )
        final_count = client.count_chat(request)
        generated_count = case.runner_input["prompt"]["token_count"]
        if final_count != target_tokens or final_count != generated_count:
            raise RunnerError(
                "token_count_drift",
                "exact final chat token count changed after materialization",
                details={
                    "generated": generated_count,
                    "recounted": final_count,
                    "target": target_tokens,
                },
            )
        trace["tokenization"] = {
            "mode": client.token_count_mode,
            "tokenizer_id": tokenizer_id,
            "exact_prompt_tokens": final_count,
        }
        trace["runner_input"] = _recorded_runner_input(case.runner_input)
        request_bytes = _json_bytes(request)
        trace["request"] = {
            "sha256": _sha256(request_bytes),
            "size_bytes": len(request_bytes),
            "stream": False,
            "max_tokens": max_tokens,
        }
        hardware_before = _hardware_sample()
        started = time.perf_counter()
        trace["model_calls"] = 1
        try:
            raw_response = client.request("POST", "/v1/chat/completions", request)
        except RunnerError as exc:
            exc.model_calls = 1
            raise
        wall_ms = round((time.perf_counter() - started) * 1000, 3)
        hardware_after = _hardware_sample()

        response_model = raw_response.get("model")
        fingerprint = raw_response.get("system_fingerprint")
        expected_build = manifest["baseline"]["backend_build"]
        if response_model != model:
            raise RunnerError(
                "model_mismatch",
                "completion response model differs from requested model",
                model_calls=1,
                details={"requested": model, "observed": response_model},
            )
        if not isinstance(fingerprint, str) or not fingerprint.startswith(expected_build):
            raise RunnerError(
                "runtime_mismatch",
                "completion fingerprint differs from the frozen runtime",
                model_calls=1,
                details={"system_fingerprint": fingerprint},
            )
        content, reasoning = _extract_model_text(raw_response)
        try:
            candidate = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RunnerError(
                "model_output_malformed", "model returned non-JSON content", model_calls=1
            ) from exc
        normalized_response, verification_evidence = _normalize_model_response(
            case, candidate, timeout=timeout
        )
        usage = raw_response.get("usage")
        timings = raw_response.get("timings")
        if not isinstance(usage, dict) or not isinstance(timings, dict):
            raise RunnerError(
                "model_response_malformed", "completion lacks usage or timings", model_calls=1
            )
        prompt_tokens = usage.get("prompt_tokens")
        if prompt_tokens != target_tokens:
            raise RunnerError(
                "token_count_drift",
                "inference usage disagrees with exact preflight token count",
                model_calls=1,
                details={"preflight": target_tokens, "inference": prompt_tokens},
            )
        visible_tokens = len(client.tokenize_text(content))
        reasoning_tokens = len(client.tokenize_text(reasoning)) if reasoning else 0
        prompt_ms = timings.get("prompt_ms")
        first_decode_ms = timings.get("predicted_per_token_ms")
        ttft_ms = (
            float(prompt_ms) + float(first_decode_ms)
            if isinstance(prompt_ms, (int, float)) and isinstance(first_decode_ms, (int, float))
            else 0.0
        )
        warnings: list[str] = []
        if ttft_ms == 0:
            warnings.append("ttft_unavailable")
        peak_vram = max(
            value
            for value in (hardware_before["gpu_used_mib"], hardware_after["gpu_used_mib"], 0)
            if value is not None
        )
        peak_ram = max(
            value
            for value in (hardware_before["ram_used_mib"], hardware_after["ram_used_mib"], 0)
            if value is not None
        )
        if not peak_vram:
            warnings.append("gpu_memory_sample_unavailable")
        if not peak_ram:
            warnings.append("ram_sample_unavailable")
        normalized = {
            "schema": OUTPUT_SCHEMA,
            "suite_id": case.runner_input["suite_id"],
            "case_id": case.runner_input["case_id"],
            "phase": phase,
            "trial_index": trial_index,
            "model": {
                "family": manifest["baseline"]["model_family"],
                "provider": "llama.cpp",
                "model_id": model,
                "quantization": manifest["baseline"]["weight_quantization"],
            },
            "runtime": {
                "backend": manifest["baseline"]["backend"],
                "build": expected_build,
                "context_tokens": manifest["context_window_tokens"],
                "kv_cache": manifest["baseline"]["kv_cache"],
                "mtp": manifest["baseline"]["mtp"],
            },
            "prompt": {
                "sha256": case.runner_input["prompt"]["sha256"],
                "token_count": target_tokens,
            },
            "response": normalized_response,
            "telemetry": {
                "request_tokens": prompt_tokens,
                "reasoning_tokens": reasoning_tokens,
                "output_tokens": visible_tokens,
                "ttft_ms": ttft_ms,
                "wall_ms": wall_ms,
                "prompt_tokens_per_second": float(timings.get("prompt_per_second", 0.0)),
                "decode_tokens_per_second": float(timings.get("predicted_per_second", 0.0)),
                "peak_vram_mib": peak_vram,
                "peak_ram_mib": peak_ram,
                "backend_warnings": warnings,
                "backend_restarts": 0,
                "timed_out": False,
            },
        }
        try:
            validate_runner_output(normalized)
        except SuiteError as exc:
            raise RunnerError(
                "model_output_malformed",
                f"normalized model response violates the runner contract: {exc}",
                model_calls=1,
            ) from exc
        score = score_runner_output(case.runner_input, case.oracle, normalized)
        trace.update(
            {
                "status": "completed",
                "passed": score["passed"],
                "raw_response_sha256": _sha256(_json_bytes(raw_response)),
                "raw_response": raw_response,
                "normalized_result": normalized,
                "verification_evidence": verification_evidence,
                "telemetry_sources": {
                    "ttft_ms": "server prompt_ms + predicted_per_token_ms",
                    "memory": "pre/post observed host samples; not a continuous peak monitor",
                },
                "score": score,
            }
        )
        return trace
    except (RunnerError, SuiteError) as exc:
        if isinstance(exc, RunnerError):
            error = exc
        else:
            error = RunnerError("suite_error", str(exc), model_calls=trace["model_calls"])
        trace.update(
            {
                "status": "failed",
                "passed": False,
                "model_calls": max(trace["model_calls"], error.model_calls),
                "error": {
                    "code": error.code,
                    "message": str(error),
                    "details": error.details,
                },
            }
        )
        return trace


def write_run(path: Path, record: Mapping[str, Any]) -> None:
    encoded = _json_bytes(record) + b"\n"
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_file() and path.read_bytes() == encoded:
            return
        raise RunnerError("output_exists", f"refusing to overwrite run record: {path}")
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
            raise RunnerError("output_exists", f"refusing to overwrite run record: {path}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def main(argv: Sequence[str] | None = None) -> int:
    manifest = load_manifest()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8818")
    parser.add_argument("--model", default="arm-qwen38-q6-native-262k")
    parser.add_argument(
        "--family", choices=[item["id"] for item in manifest["families"]], required=True
    )
    parser.add_argument(
        "--target",
        type=int,
        choices=[item["prompt_tokens"] for item in manifest["fill_targets"]],
        required=True,
    )
    parser.add_argument("--phase", choices=("warmup", "measured"), default="measured")
    parser.add_argument("--trial-index", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--harness-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    record = execute_live_case(
        base_url=args.base_url,
        model=args.model,
        family=args.family,
        target_tokens=args.target,
        phase=args.phase,
        trial_index=args.trial_index,
        timeout=args.timeout,
        harness_file=args.harness_file,
    )
    try:
        write_run(args.output, record)
    except RunnerError as exc:
        print(json.dumps({"status": "failed", "error": exc.code}), file=sys.stderr)
        return 1
    summary = {
        "schema": LIVE_RUN_SCHEMA,
        "status": record["status"],
        "passed": record.get("passed", False),
        "model_calls": record["model_calls"],
        "output": str(args.output.resolve()),
    }
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    if record["status"] != "completed":
        return 1
    return 0 if record.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
