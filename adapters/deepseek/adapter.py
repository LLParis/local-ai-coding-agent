"""Pinned DeepSeek Harness adapter with a reviewed live four-tool plugin."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ..protocol import (
    AdapterContractError,
    TaskCapsule,
    canonical_json,
    sha256_file,
    validate_loopback_endpoint,
)
from ..runner import Emitter, HarnessProcessSpec, run_harness_process, stdout_emitter
from .event_bridge import EventAudit, EventProtocolError, strict_json_loads

DEEPSEEK_PACKAGE = "@deepseek-ai/dsh"
DEEPSEEK_PACKAGE_VERSION = "0.1.0-rc.8"
DEEPSEEK_SOURCE_COMMIT = "141eb6fef83422698aef7a981029e843e8161534"
DEEPSEEK_LOCK_SHA256 = "22b379a26a108f2d92d6142806ae8645bd1514768bc305b28473a9605c260dfb"
DEEPSEEK_CLI_SHA256 = "c0226687bb20f45c603ec6fe50f3de16d1c3510c3a803304ec575ef9bc366c62"
DEEPSEEK_MANIFEST_SHA256 = "5ae28ba7d9606711d35fa0d76cb279f3cff66de667b800322c0b0c4b29771e43"
SUPPORTED_NODE_MAJOR = 24
PROVIDER = "ci-loopback"


def _default_cache() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise AdapterContractError("LOCALAPPDATA is required for the per-user adapter cache")
    return (
        Path(local)
        / "CodingIntelligence"
        / "adapters"
        / "deepseek-harness"
        / DEEPSEEK_PACKAGE_VERSION
    )


def _valid_model(model: str) -> str:
    if (
        not isinstance(model, str)
        or not model.strip()
        or any(character in model for character in "\r\n\x00")
    ):
        raise AdapterContractError("model must be a non-empty single-line identifier")
    return model.strip()


def _denial_stop(reasons: list[str]) -> str | None:
    joined = "\n".join(reasons).casefold()
    if "duplicate object key" in joined:
        return "malformed_tool_arguments"
    if "duplicate side-effect identity" in joined:
        return "duplicate_side_effect"
    if "successful edit first" in joined:
        return "tool_order_violation"
    if "at most one edit" in joined or "at most one test" in joined:
        return "repeated_mutation_or_test"
    return None


class DeepSeekAdapter:
    """One DeepSeek agent, one frozen task, four tools, and no invisible retry."""

    def __init__(self, cache: Path | str | None = None) -> None:
        self.cache = Path(cache).resolve() if cache else _default_cache().resolve()
        package_root = self.cache / "node_modules" / "@deepseek-ai" / "dsh"
        self.cli = package_root / "lib" / "bin.js"
        self.package_manifest = package_root / "package.json"
        self.lock = self.cache / "package-lock.json"
        self.patch = Path(__file__).with_name("bounded_profile.patch.yml").resolve()
        self.plugin = Path(__file__).with_name("scoped_plugin.mjs").resolve()
        self.bridge = Path(__file__).with_name("event_bridge.py").resolve()

    def validate_install(self) -> dict[str, str]:
        for path in (
            self.cli,
            self.package_manifest,
            self.lock,
            self.patch,
            self.plugin,
            self.bridge,
        ):
            if not path.is_file():
                raise AdapterContractError(f"DeepSeek adapter dependency is missing: {path}")
        manifest = json.loads(self.package_manifest.read_text(encoding="utf-8"))
        if (
            manifest.get("name") != DEEPSEEK_PACKAGE
            or manifest.get("version") != DEEPSEEK_PACKAGE_VERSION
        ):
            raise AdapterContractError(
                "DeepSeek adapter cache does not match the pinned package/version"
            )
        identities = {
            "manifest": sha256_file(self.package_manifest),
            "lock": sha256_file(self.lock),
            "cli": sha256_file(self.cli),
            "patch": sha256_file(self.patch),
            "plugin": sha256_file(self.plugin),
            "bridge": sha256_file(self.bridge),
        }
        expected = {
            "manifest": DEEPSEEK_MANIFEST_SHA256,
            "lock": DEEPSEEK_LOCK_SHA256,
            "cli": DEEPSEEK_CLI_SHA256,
        }
        mismatches = [name for name, digest in expected.items() if identities[name] != digest]
        if mismatches:
            raise AdapterContractError(
                "DeepSeek pinned install identity mismatch: " + ", ".join(mismatches)
            )
        node = shutil.which("node")
        if node is None:
            raise AdapterContractError("node is missing from PATH")
        try:
            version = subprocess.run(
                [node, "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise AdapterContractError(f"cannot validate Node runtime: {error}") from error
        try:
            major = int(version.removeprefix("v").split(".", 1)[0])
        except (ValueError, IndexError) as error:
            raise AdapterContractError(f"invalid Node runtime version: {version!r}") from error
        if major != SUPPORTED_NODE_MAJOR:
            raise AdapterContractError(
                f"DeepSeek runtime requires supported Node major {SUPPORTED_NODE_MAJOR}; "
                f"found {version}"
            )
        return {**identities, "node": node, "node_version": version}

    @staticmethod
    def _validate_budgets(
        max_turns: int, max_tool_calls: int, max_output_tokens: int
    ) -> None:
        if not isinstance(max_turns, int) or not 1 <= max_turns <= 32:
            raise AdapterContractError("max_turns must be an integer from 1 through 32")
        if not isinstance(max_tool_calls, int) or not 1 <= max_tool_calls <= 64:
            raise AdapterContractError("max_tool_calls must be an integer from 1 through 64")
        if not isinstance(max_output_tokens, int) or not 1 <= max_output_tokens <= 65_536:
            raise AdapterContractError(
                "max_output_tokens must be an integer from 1 through 65536"
            )

    def launch_plan(
        self,
        capsule: TaskCapsule,
        stage: Path | str,
        *,
        endpoint: str,
        model: str,
        max_turns: int = 8,
        max_tool_calls: int = 12,
        max_output_tokens: int = 8192,
    ) -> dict[str, Any]:
        install = self.validate_install()
        stage_path = capsule.validate_stage(stage)
        endpoint = validate_loopback_endpoint(endpoint)
        model = _valid_model(model)
        self._validate_budgets(max_turns, max_tool_calls, max_output_tokens)
        return {
            "executable": install["node"],
            "argv_template": [
                str(self.cli),
                "--profile",
                "headless",
                "--patch",
                "<per-run-rendered-bounded-patch>",
            ],
            "cwd": str(stage_path),
            "provider": PROVIDER,
            "model": model,
            "endpoint": endpoint,
            "max_turns": max_turns,
            "max_tool_calls": max_tool_calls,
            "max_output_tokens": max_output_tokens,
            "runtime_identity": {
                "package": DEEPSEEK_PACKAGE,
                "version": DEEPSEEK_PACKAGE_VERSION,
                "source_commit": DEEPSEEK_SOURCE_COMMIT,
                "node_version": install["node_version"],
                "manifest_sha256": install["manifest"],
                "lock_sha256": install["lock"],
                "cli_sha256": install["cli"],
                "patch_sha256": install["patch"],
                "plugin_sha256": install["plugin"],
                "event_bridge_sha256": install["bridge"],
            },
            "runnable": True,
            "tool_surface": ["read", "search", "edit", "test"],
            "automatic_retries": 0,
            "automatic_compaction": False,
            "dynamic_plugins": False,
            "event_contract": {
                "types": ["run_start", "turn_start", "tool_call", "tool_result", "terminal"],
                "exactly_one_terminal": True,
                "tool_call_ids": "non-empty and globally unique",
                "side_effect_intents": "fsynced before edit/test dispatch",
            },
        }

    def run(
        self,
        capsule: TaskCapsule,
        stage: Path | str,
        *,
        endpoint: str,
        model: str,
        max_turns: int = 8,
        max_tool_calls: int = 12,
        max_output_tokens: int = 8192,
        emit: Emitter | None = None,
    ) -> dict[str, Any]:
        output = emit or stdout_emitter()
        plan = self.launch_plan(
            capsule,
            stage,
            endpoint=endpoint,
            model=model,
            max_turns=max_turns,
            max_tool_calls=max_tool_calls,
            max_output_tokens=max_output_tokens,
        )
        stage_path = Path(plan["cwd"])
        runtime_root = self.cache / "runtime"
        runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ci-deepseek-", dir=runtime_root) as temporary:
            runtime = Path(temporary).resolve()
            runtime_plugin = runtime / "scoped_plugin.mjs"
            shutil.copyfile(self.plugin, runtime_plugin)
            runtime_patch = runtime / "bounded.patch.yml"
            patch_text = self.patch.read_text(encoding="utf-8")
            placeholder = "__CI_DSH_PLUGIN_URL__"
            if patch_text.count(placeholder) != 1:
                raise AdapterContractError("DeepSeek patch plugin placeholder is invalid")
            runtime_patch.write_text(
                patch_text.replace(placeholder, json.dumps(runtime_plugin.as_uri())),
                encoding="utf-8",
            )
            dsh_home = runtime / "home"
            dsh_home.mkdir()
            event_ledger = runtime / "events.ndjson"
            run_id = str(uuid.uuid4())
            env = dict(os.environ)
            env.update(
                {
                    "DSH_HOME": str(dsh_home),
                    "DSH_TELEMETRY_DISABLED": "1",
                    "DSH_TELEMETRY_MODE": "DISABLED",
                    "DSH_PERMISSION_MODE": "read-only",
                    "DSH_TOOLS_MODE": "native",
                    "NO_COLOR": "1",
                    "CI_ADAPTER_RUN_ID": run_id,
                    "CI_ADAPTER_ENDPOINT": plan["endpoint"],
                    "CI_ADAPTER_API_KEY": "local-only",
                    "CI_ADAPTER_MODEL": plan["model"],
                    "CI_ADAPTER_STAGE": str(stage_path),
                    "CI_ADAPTER_OBJECTIVE": capsule.objective,
                    "CI_ADAPTER_READABLE_JSON": canonical_json(list(capsule.readable)),
                    "CI_ADAPTER_MUTABLE_JSON": canonical_json(list(capsule.mutable)),
                    "CI_ADAPTER_TEST_COMMAND_JSON": canonical_json(list(capsule.test_command)),
                    "CI_ADAPTER_TEST_TIMEOUT_MS": str(min(capsule.timeout * 1000, 300_000)),
                    "CI_ADAPTER_MAX_TURNS": str(max_turns),
                    "CI_ADAPTER_MAX_TOOL_CALLS": str(max_tool_calls),
                    "CI_ADAPTER_MAX_OUTPUT_TOKENS": str(max_output_tokens),
                    "CI_ADAPTER_EVENT_LEDGER": str(event_ledger),
                }
            )
            child_command = (
                plan["executable"],
                str(self.cli),
                "--profile",
                "headless",
                "--patch",
                str(runtime_patch),
            )
            spec = HarnessProcessSpec(
                harness="deepseek",
                model=plan["model"],
                provider=PROVIDER,
                command=(
                    sys.executable,
                    str(self.bridge),
                    "--max-turns",
                    str(max_turns),
                    "--max-tool-calls",
                    str(max_tool_calls),
                    "--",
                    *child_command,
                ),
                env=env,
                cwd=stage_path,
                max_turns=max_turns,
                max_tool_calls=max_tool_calls,
                timeout_seconds=min(float(capsule.timeout), 300.0),
            )
            captured_terminal: list[dict[str, Any]] = []

            def relay(event: dict[str, Any]) -> None:
                if event.get("type") == "terminal":
                    captured_terminal.append(event)
                else:
                    output(event)

            result = run_harness_process(
                spec,
                capsule,
                stage_path,
                emit=relay,
                run_verifier=True,
            )
            if len(captured_terminal) != 1 or captured_terminal[0] is not result:
                raise AdapterContractError(
                    "DeepSeek runner did not produce exactly one owned terminal result"
                )
            evidence: dict[str, Any] = {
                "ledger_exists": event_ledger.is_file(),
                "ledger_sha256": sha256_file(event_ledger) if event_ledger.is_file() else None,
                "events": 0,
                "model_requests": 0,
                "tool_call_ids": [],
                "effect_ids": [],
                "calls": [],
                "pre_side_effect_intents": 0,
                "agent_end": False,
                "denied_calls": 0,
                "denial_reasons": [],
                "edit_succeeded": False,
                "test_succeeded": False,
                "usage": {"input": 0, "output": 0, "total": 0},
            }
            try:
                if not event_ledger.is_file():
                    raise EventProtocolError("durable live-event ledger is missing")
                lines = event_ledger.read_text(encoding="utf-8").splitlines()
                events = [strict_json_loads(line) for line in lines]
                audit = EventAudit(max_turns=max_turns, max_tool_calls=max_tool_calls)
                for event in events:
                    audit.accept(event)
                evidence.update(
                    {
                        "events": len(events),
                        "model_requests": len(audit.turns),
                        "tool_call_ids": list(audit.calls),
                        "effect_ids": sorted(audit.effect_ids),
                        "calls": [
                            {
                                "call_id": event["toolCallId"],
                                "tool": event["toolName"],
                                "effect_id": event["effectId"],
                                "side_effect": event["intent"]["side_effect"],
                                "denied": event["denied"],
                            }
                            for event in events
                            if event.get("type") == "tool_execution_start"
                        ],
                        "pre_side_effect_intents": sum(
                            1
                            for event in events
                            if event.get("type") == "tool_execution_start"
                            and event.get("intent", {}).get("side_effect") is True
                        ),
                        "denied_calls": sum(
                            1
                            for event in events
                            if event.get("type") == "tool_execution_start"
                            and event.get("denied") is not None
                        ),
                        "denial_reasons": [
                            event["denied"]
                            for event in events
                            if event.get("type") == "tool_execution_start"
                            and isinstance(event.get("denied"), str)
                        ],
                        "agent_end": audit.agent_end,
                        "edit_succeeded": audit.edit_succeeded,
                        "test_succeeded": audit.test_succeeded,
                        "usage": audit.usage,
                    }
                )
                if result["status"] == "passed":
                    audit.finish(0)
            except (EventProtocolError, OSError, UnicodeError) as error:
                evidence["audit_error"] = str(error)
                if result["status"] == "passed":
                    result["status"] = "failed"
                    result["stop"] = "protocol_error"
                    result["error"] = f"DeepSeek durable event audit failed: {error}"
            result["live_event_evidence"] = evidence
            denial_stop = _denial_stop(evidence["denial_reasons"])
            if result["status"] == "failed" and denial_stop is not None:
                result["stop"] = denial_stop
                result["error"] = evidence["denial_reasons"][0]
            result["runtime_identity"] = plan["runtime_identity"]
            result["runtime_identity"]["rendered_patch_sha256"] = sha256_file(runtime_patch)
            result["plugin_run_id"] = run_id
            output(result)
            return result
