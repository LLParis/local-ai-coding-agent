"""Pinned Pi launcher with a reviewed four-tool extension and isolated config."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..protocol import AdapterContractError, TaskCapsule, canonical_json, validate_loopback_endpoint
from ..runner import Emitter, HarnessProcessSpec, run_harness_process

PI_PACKAGE = "@earendil-works/pi-coding-agent"
PI_PACKAGE_VERSION = "0.84.2"
PI_SOURCE_COMMIT = "5cd93f688aaab89dbb6dfa4aca535f21796ae185"
PROVIDER = "ci-loopback"


def _default_cache() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise AdapterContractError("LOCALAPPDATA is required for the per-user adapter cache")
    return Path(local) / "CodingIntelligence" / "adapters" / "pi" / PI_PACKAGE_VERSION


class PiAdapter:
    def __init__(self, cache: Path | str | None = None) -> None:
        self.cache = Path(cache).resolve() if cache else _default_cache().resolve()
        package_root = self.cache / "node_modules" / "@earendil-works" / "pi-coding-agent"
        self.cli = package_root / "dist" / "cli.js"
        self.package_manifest = package_root / "package.json"
        self.lock = self.cache / "package-lock.json"
        self.extension = Path(__file__).with_name("scoped_tools.ts").resolve()

    def validate_install(self) -> None:
        for path in (self.cli, self.package_manifest, self.lock, self.extension):
            if not path.is_file():
                raise AdapterContractError(f"Pi adapter dependency is missing: {path}")
        manifest = json.loads(self.package_manifest.read_text(encoding="utf-8"))
        if manifest.get("name") != PI_PACKAGE or manifest.get("version") != PI_PACKAGE_VERSION:
            raise AdapterContractError("Pi adapter cache does not match the pinned package/version")

    @staticmethod
    def _prompt(capsule: TaskCapsule) -> str:
        return "\n".join(
            (
                "Complete this frozen coding task in the disposable stage.",
                f"Objective: {capsule.objective}",
                f"Readable paths: {canonical_json(list(capsule.readable))}",
                f"Mutable paths: {canonical_json(list(capsule.mutable))}",
                "Use only read, search, edit, and test. Do not guess that a tool succeeded.",
                "Make the smallest correct edit, run test, and finish with a concise "
                "diagnosis and result.",
            )
        )

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
        self.validate_install()
        stage_path = capsule.validate_stage(stage)
        endpoint = validate_loopback_endpoint(endpoint)
        if (
            not isinstance(model, str)
            or not model.strip()
            or any(char in model for char in "\r\n\x00")
        ):
            raise AdapterContractError("model must be a non-empty single-line identifier")
        runtime_root = self.cache.parent / "runtime"
        runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="pi-", dir=runtime_root) as temporary:
            config = Path(temporary)
            settings = {
                "compaction": {"enabled": False},
                "retry": {
                    "enabled": False,
                    "maxRetries": 0,
                    "provider": {"maxRetries": 0, "maxRetryDelayMs": 1},
                },
                "defaultProjectTrust": "never",
                "enableInstallTelemetry": False,
                "defaultTools": [],
                "packages": [],
                "extensions": [],
                "skills": [],
                "prompts": [],
                "themes": [],
            }
            models = {
                "providers": {
                    PROVIDER: {
                        "baseUrl": endpoint,
                        "api": "openai-completions",
                        "apiKey": "local-only",
                        "authHeader": False,
                        "compat": {
                            "supportsDeveloperRole": False,
                            "supportsReasoningEffort": False,
                        },
                        "models": [
                            {
                                "id": model,
                                "name": f"Local {model}",
                                "reasoning": False,
                                "input": ["text"],
                                "contextWindow": 32768,
                                "maxTokens": 8192,
                                "cost": {
                                    "input": 0,
                                    "output": 0,
                                    "cacheRead": 0,
                                    "cacheWrite": 0,
                                },
                            }
                        ],
                    }
                }
            }
            (config / "settings.json").write_text(canonical_json(settings) + "\n", encoding="utf-8")
            (config / "models.json").write_text(canonical_json(models) + "\n", encoding="utf-8")
            env = dict(os.environ)
            env.update(
                {
                    "PI_CODING_AGENT_DIR": str(config),
                    "PI_CODING_AGENT_SESSION_DIR": str(config / "sessions"),
                    "PI_OFFLINE": "1",
                    "PI_SKIP_VERSION_CHECK": "1",
                    "PI_TELEMETRY": "0",
                    "NO_COLOR": "1",
                    "CI_ADAPTER_STAGE": str(stage_path),
                    "CI_ADAPTER_READABLE_JSON": canonical_json(list(capsule.readable)),
                    "CI_ADAPTER_MUTABLE_JSON": canonical_json(list(capsule.mutable)),
                    "CI_ADAPTER_TEST_COMMAND_JSON": canonical_json(list(capsule.test_command)),
                    "CI_ADAPTER_TEST_TIMEOUT_MS": str(min(capsule.timeout * 1000, 300_000)),
                    "CI_ADAPTER_MAX_TOOL_CALLS": str(max_tool_calls),
                }
            )
            system_prompt = (
                "You are a bounded local coding agent in a disposable stage. "
                "Use only the four supplied tools. Never access paths outside their "
                "declared scope, never retry a failed model request, and never claim "
                "a test passed without the test result."
            )
            command = (
                "node",
                str(self.cli),
                "--mode",
                "json",
                "--no-session",
                "--provider",
                PROVIDER,
                "--model",
                model,
                "--api-key",
                "local-only",
                "--thinking",
                "off",
                "--no-builtin-tools",
                "--tools",
                "read,search,edit,test",
                "--no-extensions",
                "--extension",
                str(self.extension),
                "--no-skills",
                "--no-prompt-templates",
                "--no-themes",
                "--no-context-files",
                "--no-approve",
                "--system-prompt",
                system_prompt,
                self._prompt(capsule),
            )
            spec = HarnessProcessSpec(
                harness="pi",
                model=model,
                provider=PROVIDER,
                command=command,
                env=env,
                cwd=stage_path,
                max_turns=max_turns,
                max_tool_calls=max_tool_calls,
                timeout_seconds=min(float(capsule.timeout), 300.0),
            )
            return run_harness_process(spec, capsule, stage_path, emit=emit, run_verifier=True)
