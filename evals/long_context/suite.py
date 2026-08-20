"""Frozen, deterministic Qwen native-context qualification corpus and scorer.

This module never starts a backend or calls a model.  A live runner supplies the
exact runtime tokenizer callback, sends ``runner_input['prompt']['text']`` to the
model/harness, and returns the normalized result contract scored here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INPUT_SCHEMA = "coding-intelligence-long-context-runner-input/v1"
OUTPUT_SCHEMA = "coding-intelligence-long-context-runner-output/v1"
ORACLE_SCHEMA = "coding-intelligence-long-context-oracle/v1"
SCORE_SCHEMA = "coding-intelligence-long-context-score/v1"
QUALIFICATION_SCHEMA = "coding-intelligence-long-context-qualification/v1"

ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "manifest.json"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

TokenCounter = Callable[[str], int]
PaddingFactory = Callable[[int, str], str]

_RESPONSE_KEYS = {
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
_TELEMETRY_KEYS = {
    "request_tokens",
    "reasoning_tokens",
    "output_tokens",
    "ttft_ms",
    "wall_ms",
    "prompt_tokens_per_second",
    "decode_tokens_per_second",
    "peak_vram_mib",
    "peak_ram_mib",
    "backend_warnings",
    "backend_restarts",
    "timed_out",
}


class SuiteError(ValueError):
    pass


@dataclass(frozen=True)
class GeneratedCase:
    runner_input: dict[str, Any]
    oracle: dict[str, Any]


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


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SuiteError(f"cannot load {path}: {exc}") from exc


def load_manifest() -> dict[str, Any]:
    manifest = _load_json(MANIFEST_PATH)
    if not isinstance(manifest, dict) or manifest.get("schema") != (
        "coding-intelligence-long-context-suite/v1"
    ):
        raise SuiteError("unsupported long-context manifest")
    targets = manifest.get("fill_targets")
    families = manifest.get("families")
    if not isinstance(targets, list) or [item.get("prompt_tokens") for item in targets] != [
        131_072,
        196_608,
        235_930,
    ]:
        raise SuiteError("manifest must freeze the 50/75/90 prompt targets")
    expected_families = {
        "depth_retrieval",
        "temporal_decision",
        "repository_diagnosis",
        "tool_contract",
        "resume_compaction",
    }
    if not isinstance(families, list) or {item.get("id") for item in families} != (
        expected_families
    ):
        raise SuiteError("manifest must freeze exactly the five native-context families")
    for family in families:
        request = family.get("request")
        if not isinstance(request, str) or not request.strip():
            raise SuiteError(f"family {family.get('id')} has no explicit request")
        records = family.get("records")
        if not isinstance(records, list) or not records:
            raise SuiteError(f"family {family.get('id')} has no records")
        positions = {record.get("position") for record in records}
        if not {"early", "middle", "late"}.issubset(positions):
            raise SuiteError(f"family {family.get('id')} lacks depth-balanced records")
    return manifest


def manifest_sha256() -> str:
    return _digest_bytes(_canonical(load_manifest()))


def whitespace_token_count(text: str) -> int:
    """Test-only counter; live runs must supply the exact runtime tokenizer."""

    return len(re.findall(r"\S+", text))


def deterministic_padding(token_budget: int, seed: str) -> str:
    """Prefix-stable neutral filler containing exactly N whitespace tokens."""

    if token_budget < 0:
        raise SuiteError("padding token budget cannot be negative")
    if not token_budget:
        return ""
    salt = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:6]
    vocabulary = (
        "archived",
        "telemetry",
        "sample",
        "unrelated",
        "historical",
        "cache",
        "observation",
        "nominal",
        "background",
        "record",
        "closed",
    )
    tokens: list[str] = []
    for index in range(token_budget):
        if index % len(vocabulary) == 0:
            tokens.append(f"[DISTRACTOR-{salt}-{index // len(vocabulary):06d}]")
        else:
            tokens.append(vocabulary[index % len(vocabulary)])
    lines = [" ".join(tokens[index : index + 44]) for index in range(0, len(tokens), 44)]
    return "\n".join(lines)


def _join(*parts: str) -> str:
    return "\n".join(part for part in parts if part)


def _pad_before_suffix(
    prefix: str,
    suffix: str,
    target_tokens: int,
    token_counter: TokenCounter,
    padding_factory: PaddingFactory,
    seed: str,
) -> str:
    """Reach an exact measured target using a prefix-stable filler callback."""

    fixed = _join(prefix, suffix)
    fixed_count = token_counter(fixed)
    if fixed_count > target_tokens:
        raise SuiteError(
            f"fixed corpus uses {fixed_count} tokens, above checkpoint {target_tokens}"
        )
    if fixed_count == target_tokens:
        return fixed

    def trial(requested: int, attempt: int) -> tuple[str, int]:
        if requested <= 0:
            value = fixed
        else:
            filler = padding_factory(requested, f"{seed}-{attempt}")
            if not isinstance(filler, str) or not filler.strip():
                raise SuiteError("padding factory must return non-empty text for a positive budget")
            value = _join(prefix, filler, suffix)
        return value, token_counter(value)

    current_prefix = prefix
    for attempt in range(16):
        prefix_count = token_counter(_join(current_prefix, suffix))
        deficit = target_tokens - prefix_count
        if deficit == 0:
            return _join(current_prefix, suffix)
        if deficit < 0:
            raise SuiteError("padding crossed the requested token boundary")

        prefix = current_prefix
        direct, direct_count = trial(deficit, attempt)
        if direct_count == target_tokens:
            return direct

        high = max(1, deficit)
        high_value, high_count = direct, direct_count
        expansions = 0
        while high_count < target_tokens and expansions < 8:
            high *= 2
            high_value, high_count = trial(high, attempt)
            expansions += 1
        if high_count < target_tokens:
            raise SuiteError("padding factory could not supply enough measured tokens")

        low = 0
        best_value = _join(prefix, suffix)
        best_count = token_counter(best_value)
        while low <= high:
            middle = (low + high) // 2
            value, measured = trial(middle, attempt)
            if measured == target_tokens:
                return value
            if measured < target_tokens:
                if measured >= best_count:
                    best_value, best_count = value, measured
                low = middle + 1
            else:
                high = middle - 1

        if best_count <= token_counter(_join(current_prefix, suffix)):
            raise SuiteError(
                "exact token target is unreachable with this counter/filler pair; "
                "supply a tokenizer-aware padding factory"
            )
        if suffix:
            # Intermediate rounds must remain before the fixed suffix.
            marker = "\n" + suffix
            if not best_value.endswith(marker):
                raise SuiteError("internal suffix placement failure")
            current_prefix = best_value[: -len(marker)]
        else:
            current_prefix = best_value

    raise SuiteError("exact token target was not reached within 16 deterministic rounds")


def _family(manifest: Mapping[str, Any], family_id: str) -> dict[str, Any]:
    for family in manifest["families"]:
        if family["id"] == family_id:
            return family
    raise SuiteError(f"unknown long-context family: {family_id}")


def _target(manifest: Mapping[str, Any], target_tokens: int) -> dict[str, Any]:
    for target in manifest["fill_targets"]:
        if target["prompt_tokens"] == target_tokens:
            return target
    raise SuiteError(f"unsupported prompt target: {target_tokens}")


def _record_text(records: Sequence[Mapping[str, Any]], position: str) -> str:
    selected = [record for record in records if record["position"] == position]
    return "\n".join(
        json.dumps(
            {"source_id": record["source_id"], "content": record["text"]},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        for record in selected
    )


def _response_contract(family_id: str) -> str:
    base = (
        "Return one JSON response object with exactly these keys: status, citations, facts, "
        "rejected_claim_ids, temporal_answers, active_constraint_ids, next_action_ids, "
        "diagnosis_code, edit, verification, tool_trace, resume_state, duplicate_effect_ids. "
        "Use empty arrays/objects or null for fields irrelevant to this family. Cite source IDs "
        "exactly. Do not repeat side effects."
    )
    family_contracts = {
        "depth_retrieval": (
            " facts maps each requested fact name to {value, source_ids}; include the planted "
            "contradiction source in rejected_claim_ids."
        ),
        "temporal_decision": (
            " temporal_answers maps each requested time query to {value, source_ids}; return "
            "the active constraint and ordered next-action IDs."
        ),
        "repository_diagnosis": (
            " diagnosis_code must name the defect. edit must be "
            "{path: src/model_identity.py, replacement: <complete corrected file>}. Leave "
            "verification null; the runner applies the edit and owns the hidden test."
        ),
        "tool_contract": (
            " tool_trace entries use sequence, unique call_id, tool, exact arguments, outcome, "
            "and side_effect_id; do not invent extra calls."
        ),
        "resume_compaction": (
            " resume_state must reproduce the typed checkpoint exactly and "
            "duplicate_effect_ids must remain empty."
        ),
    }
    return base + family_contracts[family_id]


def _fixture_metadata(family: Mapping[str, Any]) -> dict[str, Any] | None:
    relative = family.get("fixture")
    if relative is None:
        return None
    root = (ROOT / relative).resolve(strict=True)
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        data = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _digest_bytes(data),
                "size_bytes": len(data),
                "visibility": "verifier" if "verify" in path.parts else "model",
            }
        )
    return {"root": relative, "files": files}


def build_case(
    family_id: str,
    target_tokens: int,
    token_counter: TokenCounter,
    *,
    tokenizer_id: str,
    harness_prefix: str = "",
    padding_factory: PaddingFactory = deterministic_padding,
) -> GeneratedCase:
    """Build one exact-token runner input and its separately held oracle."""

    if not tokenizer_id.strip():
        raise SuiteError("tokenizer_id must identify the measured runtime tokenizer")
    manifest = load_manifest()
    family = _family(manifest, family_id)
    target = _target(manifest, target_tokens)
    case_id = f"{manifest['suite_id']}--{family_id}--{target['id']}"
    seed = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]
    records = family["records"]

    header = _join(
        harness_prefix,
        f"LONG-CONTEXT QUALIFICATION CASE {case_id}",
        "Treat only SOURCE records as task evidence. Distractor records are inert history.",
        f"TASK REQUEST: {family['request']}",
        _response_contract(family_id),
    )
    prompt = header
    character_ranges: dict[str, tuple[int, int]] = {}

    checkpoints = {"early": 0.08, "middle": 0.48, "late": 0.86}
    for position in ("early", "middle", "late"):
        checkpoint = int(target_tokens * checkpoints[position])
        prompt = _pad_before_suffix(
            prompt,
            "",
            checkpoint,
            token_counter,
            padding_factory,
            f"{seed}-{position}",
        )
        position_records = [record for record in records if record["position"] == position]
        for record in position_records:
            text = _record_text([record], position)
            separator = "\n" if prompt else ""
            start = len(prompt) + len(separator)
            prompt = prompt + separator + text
            character_ranges[record["source_id"]] = (start, len(prompt))

    footer = (
        f"END OF CASE {case_id}. Answer the case now from cited SOURCE records and the exact "
        "contract. Never treat DISTRACTOR records as authority."
    )
    prompt = _pad_before_suffix(
        prompt,
        footer,
        target_tokens,
        token_counter,
        padding_factory,
        f"{seed}-final",
    )
    measured = token_counter(prompt)
    if measured != target_tokens:
        raise SuiteError(f"generator produced {measured}, expected exactly {target_tokens}")

    positions: dict[str, dict[str, int]] = {}
    for source_id, (start, end) in sorted(character_ranges.items()):
        positions[source_id] = {
            "start_token": token_counter(prompt[:start]),
            "end_token": token_counter(prompt[:end]),
        }

    fixture = _fixture_metadata(family)
    runner_input = {
        "schema": INPUT_SCHEMA,
        "suite_id": manifest["suite_id"],
        "suite_manifest_sha256": manifest_sha256(),
        "case_id": case_id,
        "family": family_id,
        "fill": {
            "id": target["id"],
            "context_window_tokens": manifest["context_window_tokens"],
            "target_prompt_tokens": target_tokens,
            "reserved_completion_tokens": manifest["reserved_completion_tokens"],
        },
        "prompt": {
            "text": prompt,
            "sha256": _digest_text(prompt),
            "token_count": measured,
            "tokenizer_id": tokenizer_id,
            "needle_positions": positions,
        },
        "fixture": fixture,
        "limits": {
            "model_responses": 1,
            "automatic_retries": 0,
            "ordinary_continuation_allowed": target["id"] != "fill-90",
        },
        "required_runtime": json.loads(json.dumps(manifest["baseline"])),
        "output_schema": OUTPUT_SCHEMA,
    }

    expected = json.loads(json.dumps(family["expected"]))
    if family_id == "repository_diagnosis":
        fixture_root = ROOT / family["fixture"]
        before = (fixture_root / expected["edit_path"]).read_bytes()
        after = expected["replacement"].encode("utf-8")
        expected["before_sha256"] = _digest_bytes(before)
        expected["after_sha256"] = _digest_bytes(after)
    oracle = {
        "schema": ORACLE_SCHEMA,
        "suite_id": manifest["suite_id"],
        "suite_manifest_sha256": manifest_sha256(),
        "case_id": case_id,
        "family": family_id,
        "prompt_sha256": runner_input["prompt"]["sha256"],
        "expected": expected,
    }
    return GeneratedCase(runner_input=runner_input, oracle=oracle)


def write_case(root: Path, case: GeneratedCase) -> tuple[Path, Path]:
    """Write immutable runner/oracle files; the live runner receives only input."""

    root.mkdir(parents=True, exist_ok=True)
    input_path = root / "input.json"
    oracle_path = root / "oracle.json"
    _write_immutable(input_path, case.runner_input)
    _write_immutable(oracle_path, case.oracle)
    return input_path, oracle_path


def _write_immutable(path: Path, value: Any) -> None:
    encoded = _canonical(value)
    if path.exists():
        if path.read_bytes() == encoded:
            return
        raise SuiteError(f"refusing to overwrite different frozen artifact: {path}")
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
            raise SuiteError(f"refusing to overwrite frozen artifact: {path}") from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def generate_suite(
    output_root: Path,
    token_counter: TokenCounter,
    *,
    tokenizer_id: str,
    harness_prefix: str = "",
    padding_factory: PaddingFactory = deterministic_padding,
) -> list[tuple[Path, Path]]:
    """Materialize all fifteen runner/oracle pairs without model inference."""

    manifest = load_manifest()
    written: list[tuple[Path, Path]] = []
    for family in manifest["families"]:
        for target in manifest["fill_targets"]:
            case = build_case(
                family["id"],
                target["prompt_tokens"],
                token_counter,
                tokenizer_id=tokenizer_id,
                harness_prefix=harness_prefix,
                padding_factory=padding_factory,
            )
            written.append(write_case(output_root / case.runner_input["case_id"], case))
    return written


def _exact_keys(value: Any, keys: set[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise SuiteError(f"{path} must be an object")
    missing = sorted(keys - set(value))
    unknown = sorted(set(value) - keys)
    if missing:
        raise SuiteError(f"{path} missing keys: {', '.join(missing)}")
    if unknown:
        raise SuiteError(f"{path} unknown keys: {', '.join(unknown)}")
    return value


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SuiteError(f"{path} must be a list of strings")
    if len(value) != len(set(value)):
        raise SuiteError(f"{path} must not contain duplicates")
    return value


def validate_runner_output(value: Any) -> dict[str, Any]:
    """Strictly validate the normalized live-runner result contract."""

    top = _exact_keys(
        value,
        {
            "schema",
            "suite_id",
            "case_id",
            "phase",
            "trial_index",
            "model",
            "runtime",
            "prompt",
            "response",
            "telemetry",
        },
        "result",
    )
    if top["schema"] != OUTPUT_SCHEMA:
        raise SuiteError(f"result.schema must be {OUTPUT_SCHEMA}")
    for key in ("suite_id", "case_id"):
        if not isinstance(top[key], str) or not top[key]:
            raise SuiteError(f"result.{key} must be a non-empty string")
    if top["phase"] not in {"warmup", "measured"}:
        raise SuiteError("result.phase must be warmup or measured")
    if isinstance(top["trial_index"], bool) or not isinstance(top["trial_index"], int):
        raise SuiteError("result.trial_index must be an integer")
    if top["phase"] == "warmup" and top["trial_index"] != 0:
        raise SuiteError("warmup trial_index must be zero")
    if top["phase"] == "measured" and top["trial_index"] not in {1, 2, 3}:
        raise SuiteError("measured trial_index must be 1, 2, or 3")

    model = _exact_keys(
        top["model"], {"family", "provider", "model_id", "quantization"}, "model"
    )
    if any(not isinstance(model[key], str) or not model[key] for key in model):
        raise SuiteError("model fields must be non-empty strings")
    runtime = _exact_keys(
        top["runtime"],
        {"backend", "build", "context_tokens", "kv_cache", "mtp"},
        "runtime",
    )
    runtime_strings = ("backend", "build", "kv_cache")
    if any(not isinstance(runtime[key], str) or not runtime[key] for key in runtime_strings):
        raise SuiteError("runtime string fields must be non-empty")
    if runtime["context_tokens"] != 262_144 or not isinstance(runtime["mtp"], bool):
        raise SuiteError("runtime must report the native 262144 context and boolean mtp")
    prompt = _exact_keys(top["prompt"], {"sha256", "token_count"}, "prompt")
    if not isinstance(prompt["sha256"], str) or not _SHA256_RE.fullmatch(prompt["sha256"]):
        raise SuiteError("prompt.sha256 must be canonical")
    if isinstance(prompt["token_count"], bool) or not isinstance(prompt["token_count"], int):
        raise SuiteError("prompt.token_count must be an integer")

    response = _exact_keys(top["response"], _RESPONSE_KEYS, "response")
    if response["status"] not in {"completed", "failed", "timed_out"}:
        raise SuiteError("response.status is invalid")
    for key in (
        "citations",
        "rejected_claim_ids",
        "active_constraint_ids",
        "next_action_ids",
        "duplicate_effect_ids",
    ):
        _string_list(response[key], f"response.{key}")
    if not isinstance(response["facts"], dict) or not isinstance(
        response["temporal_answers"], dict
    ):
        raise SuiteError("response facts and temporal_answers must be objects")
    for field in ("facts", "temporal_answers"):
        for key, answer in response[field].items():
            if not isinstance(key, str):
                raise SuiteError(f"response.{field} keys must be strings")
            item = _exact_keys(answer, {"value", "source_ids"}, f"response.{field}.{key}")
            _string_list(item["source_ids"], f"response.{field}.{key}.source_ids")
    if response["diagnosis_code"] is not None and not isinstance(
        response["diagnosis_code"], str
    ):
        raise SuiteError("response.diagnosis_code must be a string or null")
    if response["edit"] is not None:
        edit = _exact_keys(
            response["edit"], {"path", "before_sha256", "after_sha256"}, "response.edit"
        )
        if not isinstance(edit["path"], str) or not edit["path"]:
            raise SuiteError("response.edit.path must be non-empty")
        for key in ("before_sha256", "after_sha256"):
            if not isinstance(edit[key], str) or not _SHA256_RE.fullmatch(edit[key]):
                raise SuiteError(f"response.edit.{key} must be canonical")
    if response["verification"] is not None:
        verification = _exact_keys(
            response["verification"], {"command_id", "passed", "exit_code"}, "verification"
        )
        if not isinstance(verification["command_id"], str):
            raise SuiteError("verification.command_id must be a string")
        if not isinstance(verification["passed"], bool):
            raise SuiteError("verification.passed must be boolean")
        if isinstance(verification["exit_code"], bool) or not isinstance(
            verification["exit_code"], int
        ):
            raise SuiteError("verification.exit_code must be an integer")
    if not isinstance(response["tool_trace"], list):
        raise SuiteError("response.tool_trace must be a list")
    for index, call in enumerate(response["tool_trace"]):
        item = _exact_keys(
            call,
            {"sequence", "call_id", "tool", "arguments", "outcome", "side_effect_id"},
            f"tool_trace[{index}]",
        )
        if item["sequence"] != index + 1:
            raise SuiteError("tool_trace sequence must be contiguous and one-based")
        if any(not isinstance(item[key], str) or not item[key] for key in ("call_id", "tool")):
            raise SuiteError("tool_trace call_id and tool must be non-empty strings")
        if not isinstance(item["arguments"], dict):
            raise SuiteError("tool_trace arguments must be an object")
        if item["outcome"] not in {"success", "failed", "not_started", "unknown"}:
            raise SuiteError("tool_trace outcome is invalid")
        if item["side_effect_id"] is not None and not isinstance(item["side_effect_id"], str):
            raise SuiteError("tool_trace side_effect_id must be a string or null")
    if response["resume_state"] is not None and not isinstance(response["resume_state"], dict):
        raise SuiteError("response.resume_state must be an object or null")

    telemetry = _exact_keys(top["telemetry"], _TELEMETRY_KEYS, "telemetry")
    integer_telemetry = {
        "request_tokens",
        "reasoning_tokens",
        "output_tokens",
        "peak_vram_mib",
        "peak_ram_mib",
        "backend_restarts",
    }
    for key in integer_telemetry:
        if isinstance(telemetry[key], bool) or not isinstance(telemetry[key], int):
            raise SuiteError(f"telemetry.{key} must be an integer")
        if telemetry[key] < 0:
            raise SuiteError(f"telemetry.{key} cannot be negative")
    for key in _TELEMETRY_KEYS - integer_telemetry - {"backend_warnings", "timed_out"}:
        if isinstance(telemetry[key], bool) or not isinstance(telemetry[key], (int, float)):
            raise SuiteError(f"telemetry.{key} must be numeric")
        if telemetry[key] < 0:
            raise SuiteError(f"telemetry.{key} cannot be negative")
    _string_list(telemetry["backend_warnings"], "telemetry.backend_warnings")
    if not isinstance(telemetry["timed_out"], bool):
        raise SuiteError("telemetry.timed_out must be boolean")
    return json.loads(json.dumps(value))


def _append_check(
    checks: list[Check], check_id: str, expected: Any, observed: Any, *, critical: bool = True
) -> None:
    checks.append(
        Check(
            check_id=check_id,
            passed=observed == expected,
            expected=expected,
            observed=observed,
            critical=critical,
        )
    )


def _score_citations(
    checks: list[Check], required: Sequence[str], observed: Sequence[str]
) -> None:
    _append_check(
        checks,
        "required_citations",
        sorted(required),
        sorted(set(required) & set(observed)),
    )


def _score_family(
    family: str,
    expected: Mapping[str, Any],
    response: Mapping[str, Any],
    checks: list[Check],
) -> None:
    _score_citations(checks, expected["required_citations"], response["citations"])
    if family == "depth_retrieval":
        for key, answer in sorted(expected["facts"].items()):
            _append_check(checks, f"fact.{key}", answer, response["facts"].get(key))
        _append_check(
            checks,
            "contradiction_rejected",
            sorted(expected["rejected_claim_ids"]),
            sorted(response["rejected_claim_ids"]),
        )
    elif family == "temporal_decision":
        for key, answer in sorted(expected["temporal_answers"].items()):
            _append_check(
                checks, f"temporal.{key}", answer, response["temporal_answers"].get(key)
            )
        _append_check(
            checks,
            "active_constraints",
            sorted(expected["active_constraint_ids"]),
            sorted(response["active_constraint_ids"]),
        )
        _append_check(
            checks,
            "next_actions",
            expected["next_action_ids"],
            response["next_action_ids"],
        )
    elif family == "repository_diagnosis":
        _append_check(
            checks, "diagnosis", expected["diagnosis_code"], response["diagnosis_code"]
        )
        wanted_edit = {
            "path": expected["edit_path"],
            "before_sha256": expected["before_sha256"],
            "after_sha256": expected["after_sha256"],
        }
        _append_check(checks, "scoped_edit", wanted_edit, response["edit"])
        verification = response["verification"] or {}
        _append_check(
            checks,
            "hidden_verification",
            {"command_id": expected["verification_command_id"], "passed": True, "exit_code": 0},
            verification,
        )
    elif family == "tool_contract":
        observed_calls = response["tool_trace"]
        normalized = [
            {
                "tool": call["tool"],
                "arguments": call["arguments"],
                "side_effect_id": call["side_effect_id"],
            }
            for call in observed_calls
        ]
        _append_check(checks, "ordered_tools", expected["tool_calls"], normalized)
        _append_check(
            checks,
            "terminal_tool_results",
            ["success"] * len(expected["tool_calls"]),
            [call["outcome"] for call in observed_calls],
        )
        call_ids = [call["call_id"] for call in observed_calls]
        _append_check(checks, "unique_call_ids", len(call_ids), len(set(call_ids)))
        effects = [call["side_effect_id"] for call in observed_calls if call["side_effect_id"]]
        _append_check(checks, "unique_side_effects", len(effects), len(set(effects)))
        _append_check(checks, "no_duplicate_effects", [], response["duplicate_effect_ids"])
    elif family == "resume_compaction":
        _append_check(
            checks, "resume_state_equality", expected["resume_state"], response["resume_state"]
        )
        _append_check(checks, "no_duplicate_effects", [], response["duplicate_effect_ids"])
    else:
        raise SuiteError(f"unsupported score family: {family}")


def score_runner_output(
    runner_input: Mapping[str, Any], oracle: Mapping[str, Any], raw_result: Any
) -> dict[str, Any]:
    """Produce a deterministic per-trial score; malformed output stays failed."""

    base = {
        "schema": SCORE_SCHEMA,
        "suite_id": runner_input["suite_id"],
        "case_id": runner_input["case_id"],
        "family": runner_input["family"],
        "fill_id": runner_input["fill"]["id"],
        "target_prompt_tokens": runner_input["fill"]["target_prompt_tokens"],
    }
    try:
        if runner_input.get("schema") != INPUT_SCHEMA:
            raise SuiteError("unsupported runner input schema")
        if oracle.get("schema") != ORACLE_SCHEMA:
            raise SuiteError("unsupported oracle schema")
        if oracle.get("case_id") != runner_input.get("case_id"):
            raise SuiteError("oracle case_id mismatch")
        if oracle.get("prompt_sha256") != runner_input.get("prompt", {}).get("sha256"):
            raise SuiteError("oracle prompt_sha256 mismatch")
        result = validate_runner_output(raw_result)
    except (AttributeError, KeyError, SuiteError, TypeError) as exc:
        return {
            **base,
            "phase": raw_result.get("phase") if isinstance(raw_result, dict) else None,
            "trial_index": raw_result.get("trial_index") if isinstance(raw_result, dict) else None,
            "passed": False,
            "critical_passed": False,
            "earned": 0,
            "possible": 1,
            "fraction": 0.0,
            "checks": [
                Check("output_contract", False, "valid runner output", str(exc)).as_dict()
            ],
        }

    checks: list[Check] = []
    _append_check(checks, "suite_id", runner_input["suite_id"], result["suite_id"])
    _append_check(checks, "case_id", runner_input["case_id"], result["case_id"])
    _append_check(
        checks, "prompt_sha256", runner_input["prompt"]["sha256"], result["prompt"]["sha256"]
    )
    _append_check(
        checks,
        "prompt_tokens",
        runner_input["prompt"]["token_count"],
        result["prompt"]["token_count"],
    )
    _append_check(
        checks,
        "telemetry_request_tokens",
        runner_input["prompt"]["token_count"],
        result["telemetry"]["request_tokens"],
    )
    _append_check(checks, "completed", "completed", result["response"]["status"])
    _append_check(checks, "not_timed_out", False, result["telemetry"]["timed_out"])
    _append_check(checks, "no_backend_restart", 0, result["telemetry"]["backend_restarts"])
    required_runtime = runner_input["required_runtime"]
    _append_check(
        checks, "model_family", required_runtime["model_family"], result["model"]["family"]
    )
    _append_check(
        checks,
        "weight_quantization",
        required_runtime["weight_quantization"],
        result["model"]["quantization"],
    )
    _append_check(checks, "backend", required_runtime["backend"], result["runtime"]["backend"])
    _append_check(
        checks, "backend_build", required_runtime["backend_build"], result["runtime"]["build"]
    )
    _append_check(checks, "kv_cache", required_runtime["kv_cache"], result["runtime"]["kv_cache"])
    _append_check(checks, "mtp", required_runtime["mtp"], result["runtime"]["mtp"])
    _score_family(
        runner_input["family"], oracle["expected"], result["response"], checks
    )
    earned = sum(check.passed for check in checks)
    critical_passed = all(check.passed for check in checks if check.critical)
    return {
        **base,
        "phase": result["phase"],
        "trial_index": result["trial_index"],
        "passed": critical_passed,
        "critical_passed": critical_passed,
        "earned": earned,
        "possible": len(checks),
        "fraction": earned / len(checks),
        "checks": [check.as_dict() for check in checks],
    }


def aggregate_qualification(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply the documented warmup/three-trial and fill-degradation gates."""

    manifest = load_manifest()
    case_ids = {
        f"{manifest['suite_id']}--{family['id']}--{target['id']}"
        for family in manifest["families"]
        for target in manifest["fill_targets"]
    }
    observed: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    duplicates: list[str] = []
    for score in scores:
        case_id = score.get("case_id")
        phase = score.get("phase")
        trial = score.get("trial_index")
        if case_id not in case_ids or phase not in {"warmup", "measured"}:
            continue
        key = (str(case_id), str(phase), int(trial))
        if key in observed:
            duplicates.append(f"{key[0]}:{key[1]}:{key[2]}")
        else:
            observed[key] = score

    expected_keys = {
        (case_id, phase, trial)
        for case_id in case_ids
        for phase, trials in (("warmup", (0,)), ("measured", (1, 2, 3)))
        for trial in trials
    }
    missing = sorted(
        f"{case_id}:{phase}:{trial}"
        for case_id, phase, trial in expected_keys - set(observed)
    )
    missing_warmups = [item for item in missing if ":warmup:" in item]
    missing_measured = [item for item in missing if ":measured:" in item]
    measured = [score for key, score in observed.items() if key[1] == "measured"]
    fill_scores: dict[str, float] = {}
    for target in manifest["fill_targets"]:
        values = [
            float(score.get("fraction", 0.0))
            for score in measured
            if score.get("fill_id") == target["id"]
        ]
        fill_scores[target["id"]] = sum(values) / len(values) if values else 0.0
    degradation = max(0.0, fill_scores["fill-50"] - fill_scores["fill-90"])
    all_passed = bool(measured) and all(bool(score.get("passed")) for score in measured)
    qualified = not missing and not duplicates and all_passed and degradation <= 0.05
    return {
        "schema": QUALIFICATION_SCHEMA,
        "suite_id": manifest["suite_id"],
        "qualified": qualified,
        "expected_runs": len(expected_keys),
        "observed_runs": len(observed),
        "measured_runs": len(measured),
        "missing": missing,
        "duplicates": sorted(duplicates),
        "fill_scores": {key: fill_scores[key] for key in sorted(fill_scores)},
        "fill_50_to_90_degradation": degradation,
        "gates": {
            "all_critical_checks_passed": all_passed,
            "three_measured_trials_per_case": not missing_measured,
            "one_warmup_per_case": not missing_warmups,
            "max_five_point_degradation": degradation <= 0.05,
        },
    }
