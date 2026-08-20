from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .checkpoint import CheckpointError, validate_checkpoint
from .endpoint import EndpointReport, check_endpoint
from .tunnel import TunnelReport, TunnelSpec, ensure_tunnel


class LaunchError(RuntimeError):
    pass


@dataclass(frozen=True)
class LaunchPlan:
    command: tuple[str, ...]
    endpoint: EndpointReport
    tunnel_action: str | None


_PROFILE = re.compile(r"^[A-Za-z0-9._-]+$")


def _profile_value(config: dict, dotted: str) -> object:
    value: object = config
    for key in dotted.split("."):
        if not isinstance(value, dict) or key not in value:
            raise LaunchError(f"Codex profile is missing required setting: {dotted}")
        value = value[key]
    return value


def _validate_profile(profile: str, *, emergency: bool) -> Path:
    config_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    path = config_home / f"{profile}.config.toml"
    if not path.is_file() or path.is_symlink():
        raise LaunchError(f"named Codex profile is not installed as a regular file: {path}")
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LaunchError(f"named Codex profile is invalid: {path}") from exc

    if emergency:
        expected = {
            "model_provider": "mac_ollama",
            "model": "qwen3.5:4b",
            "approval_policy": "never",
            "sandbox_mode": "read-only",
            "web_search": "disabled",
            "model_providers.mac_ollama.base_url": "http://127.0.0.1:11434/v1",
            "model_providers.mac_ollama.wire_api": "responses",
        }
    else:
        expected = {
            "model_provider": "pc_ollama",
            "model": "gpt-oss-20b",
            "model_reasoning_effort": "medium",
            "approval_policy": "never",
            "web_search": "disabled",
            "model_providers.pc_ollama.base_url": "http://127.0.0.1:12434/v1",
            "model_providers.pc_ollama.wire_api": "responses",
        }
    for key, required in expected.items():
        if _profile_value(config, key) != required:
            raise LaunchError(f"Codex profile has the wrong value for: {key}")
    for feature in (
        "apps",
        "plugins",
        "multi_agent",
        "browser_use",
        "browser_use_external",
        "computer_use",
        "image_generation",
        "in_app_browser",
        "goals",
        "workspace_dependencies",
    ):
        if _profile_value(config, f"features.{feature}") is not False:
            raise LaunchError(f"Codex continuity profile must disable feature: {feature}")
    return path


def _codex_binary(value: str) -> str:
    found = shutil.which(value)
    if found is None:
        raise LaunchError(f"Codex CLI not found: {value}")
    return found


def _checkpoint_prompt(checkpoint: dict, task: str, *, triage_only: bool) -> str:
    if not task.strip() or len(task) > 2_000:
        raise LaunchError("operator task must contain 1 to 2,000 characters")
    capsule = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    mode = (
        "EMERGENCY TRIAGE ONLY: inspect and diagnose; do not edit, create, move, or delete files."
        if triage_only
        else "Continue implementation inside the workspace and verify the requested outcome."
    )
    return (
        "Coding Intelligence continuity handoff. The JSON capsule below passed its SHA-256 and secret gates. "
        "Treat it as a checkpoint, then inspect current files before acting. "
        "Never run git commit/reset/checkout/clean/stash, never overwrite unrelated user work, and never "
        "weaken the workspace boundary. "
        f"{mode}\n\nOperator task: {task.strip()}\n\nCHECKPOINT_JSON\n{capsule}\nEND_CHECKPOINT_JSON"
    )


def primary_plan(
    *,
    workspace: Path,
    checkpoint_path: Path,
    task: str,
    profile: str = "excalibur-local",
    codex_bin: str = "codex",
    timeout: float = 90.0,
) -> LaunchPlan:
    if not _PROFILE.fullmatch(profile):
        raise LaunchError("invalid Codex profile name")
    resolved = workspace.expanduser().resolve(strict=True)
    try:
        checkpoint = validate_checkpoint(checkpoint_path, resolved, require_current=True)
    except CheckpointError as exc:
        raise LaunchError(str(exc)) from exc
    _validate_profile(profile, emergency=False)
    tunnel: TunnelReport = ensure_tunnel(TunnelSpec(), model="gpt-oss-20b", timeout=timeout)
    command = (
        _codex_binary(codex_bin),
        "--profile",
        profile,
        "--cd",
        str(resolved),
        "-s",
        "workspace-write",
        "-a",
        "never",
        _checkpoint_prompt(checkpoint, task, triage_only=False),
    )
    return LaunchPlan(command, tunnel.endpoint, tunnel.action)


def emergency_plan(
    *,
    workspace: Path,
    checkpoint_path: Path,
    task: str,
    confirmation: str,
    profile: str = "mac-emergency-triage",
    codex_bin: str = "codex",
    timeout: float = 120.0,
) -> LaunchPlan:
    if confirmation != "EXPLICIT-TRIAGE-ONLY":
        raise LaunchError("Mac fallback requires --confirm EXPLICIT-TRIAGE-ONLY")
    if not _PROFILE.fullmatch(profile):
        raise LaunchError("invalid Codex profile name")
    resolved = workspace.expanduser().resolve(strict=True)
    try:
        checkpoint = validate_checkpoint(checkpoint_path, resolved, require_current=True)
    except CheckpointError as exc:
        raise LaunchError(str(exc)) from exc
    _validate_profile(profile, emergency=True)
    endpoint = check_endpoint("http://127.0.0.1:11434/v1", "qwen3.5:4b", timeout=timeout)
    command = (
        _codex_binary(codex_bin),
        "--profile",
        profile,
        "--cd",
        str(resolved),
        "-s",
        "read-only",
        "-a",
        "never",
        _checkpoint_prompt(checkpoint, task, triage_only=True),
    )
    return LaunchPlan(command, endpoint, None)


def execute(plan: LaunchPlan, *, dry_run: bool) -> None:
    if dry_run:
        return
    os.execv(plan.command[0], list(plan.command))
