"""Honest non-mutating DeepSeek launcher plan pending a stable headless seam."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from ..protocol import (
    ADAPTER_SCHEMA_VERSION,
    AdapterContractError,
    TaskCapsule,
    sha256_file,
    validate_loopback_endpoint,
)
from ..runner import Emitter, stdout_emitter

DEEPSEEK_PACKAGE = "@deepseek-ai/dsh"
DEEPSEEK_PACKAGE_VERSION = "0.1.0-rc.8"
DEEPSEEK_SOURCE_COMMIT = "141eb6fef83422698aef7a981029e843e8161534"
PROVIDER = "ci-loopback"
BLOCKER = (
    "DeepSeek Harness 0.1.0-rc.8 headless prints only the final assistant text "
    "and persists its internal event interval; it exposes no stable live "
    "NDJSON/tool-interception seam. The shipped base+headless composition also "
    "mounts broad filesystem, PowerShell, subagent, workflow, compaction, retry, "
    "Ralph, skill, and web rows. A wrapper therefore cannot prove per-call scope, "
    "zero retry, turn/tool budgets, or exact child cleanup before side effects. "
    "Qualification remains blocked until a reviewed DSH plugin provides the same "
    "four scoped tools and live event contract as the Pi lane."
)


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


class DeepSeekAdapter:
    def __init__(self, cache: Path | str | None = None) -> None:
        self.cache = Path(cache).resolve() if cache else _default_cache().resolve()
        package_root = self.cache / "node_modules" / "@deepseek-ai" / "dsh"
        self.cli = package_root / "lib" / "bin.js"
        self.package_manifest = package_root / "package.json"
        self.lock = self.cache / "package-lock.json"
        self.patch = Path(__file__).with_name("non_mutating_candidate.patch.yml").resolve()

    def validate_install(self) -> None:
        for path in (self.cli, self.package_manifest, self.lock, self.patch):
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

    def launch_plan(
        self,
        capsule: TaskCapsule,
        stage: Path | str,
        *,
        endpoint: str,
        model: str,
        max_turns: int = 8,
        max_tool_calls: int = 12,
    ) -> dict[str, Any]:
        self.validate_install()
        stage_path = capsule.validate_stage(stage)
        endpoint = validate_loopback_endpoint(endpoint)
        if (
            not isinstance(model, str)
            or not model.strip()
            or any(char in model for char in "\r\n\x00")
        ):
            raise AdapterContractError("model must be a non-empty single-line identifier")
        if not 1 <= max_turns <= 32 or not 1 <= max_tool_calls <= 64:
            raise AdapterContractError("turn and tool budgets are outside qualification bounds")
        runtime_home = self.cache.parent / "runtime" / "deepseek-placeholder"
        return {
            "executable": "node",
            "argv": [
                str(self.cli),
                "--profile",
                "headless",
                "--patch",
                str(self.patch),
                capsule.objective,
            ],
            "cwd": str(stage_path),
            "environment": {
                "DSH_HOME": str(runtime_home),
                "DSH_TELEMETRY_DISABLED": "1",
                "DSH_TELEMETRY_MODE": "DISABLED",
                "DSH_PERMISSION_MODE": "read-only",
                "CI_ADAPTER_ENDPOINT": endpoint,
                "CI_ADAPTER_MODEL": model,
            },
            "provider": PROVIDER,
            "model": model,
            "max_turns": max_turns,
            "max_tool_calls": max_tool_calls,
            "patch_sha256": sha256_file(self.patch),
            "runnable": False,
            "reason": BLOCKER,
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
        emit: Emitter | None = None,
    ) -> dict[str, Any]:
        output = emit or stdout_emitter()
        started = time.monotonic()
        run_id = str(uuid.uuid4())
        plan = self.launch_plan(
            capsule,
            stage,
            endpoint=endpoint,
            model=model,
            max_turns=max_turns,
            max_tool_calls=max_tool_calls,
        )
        output(
            {
                "type": "run_start",
                "schema_version": ADAPTER_SCHEMA_VERSION,
                "run_id": run_id,
                "harness": "deepseek",
                "model": model,
                "provider": PROVIDER,
                "max_turns": max_turns,
                "max_tool_calls": max_tool_calls,
            }
        )
        terminal = {
            "type": "terminal",
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "run_id": run_id,
            "harness": "deepseek",
            "status": "blocked",
            "model": model,
            "provider": PROVIDER,
            "turns": 0,
            "tool_calls": 0,
            "automatic_retries": 0,
            "stop": "unstable_headless_seam",
            "diff": {"changed": [], "out_of_scope": [], "unified": "", "sha256": ""},
            "test": {"not_run": True, "passed": False},
            "telemetry": {
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "child_started": False,
                "child_cleaned": True,
                "source_workspace_preserved": True,
            },
            "launch_plan": plan,
            "error": BLOCKER,
        }
        output(terminal)
        return terminal
