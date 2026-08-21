"""Live, evidence-bound route planning for the sovereign production worker.

The module is intentionally a caller of :mod:`agent_continuity.routing`, not a
second routing policy.  It detects the repository task, converts current doctor
and edge probes into ``RuntimeState``, asks the immutable selector for one
route, and translates that decision into values the production worker can
execute.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from adapters.deepseek import DeepSeekAdapter
from adapters.protocol import AdapterContractError

from .routing import (
    RoutingDecision,
    RoutingRequest,
    RuntimeState,
    load_routing_manifest,
    select_route,
)

PLAN_SCHEMA = "coding-intelligence.production-route-plan/v1"
MAC_INVOCATION_SCHEMA = "coding-intelligence.mac-swift-invocation/v1"
MAC_SNAPSHOT_SCHEMA = "coding-intelligence.mac-swift-source-snapshot/v1"
MAC_RESULT_SCHEMA = "coding-intelligence.mac-swift-verifier-result/v1"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = PACKAGE_ROOT / "config" / "routing-v1.json"
DOCTOR = PACKAGE_ROOT / "bin" / "doctor.ps1"
MAC_VERIFIER = PACKAGE_ROOT / "bin" / "mac-swift-verifier.py"
SWITCHER = PACKAGE_ROOT / "windows" / "Switch-ExcaliburBackend.ps1"
TARGET_BACKEND = "Qwen38Native"
TARGET_MODEL = "arm-qwen38-q6-native-262k"
TARGET_ENDPOINT = "http://127.0.0.1:8818/v1"
DEEPSEEK_ROUTE = "candidate-qwen38-deepseek"
DEEPSEEK_SWIFT_ROUTE = "candidate-qwen38-deepseek-apple"
_ID_SAFE = re.compile(r"[^a-z0-9._-]+")
_XCODE_SCHEME_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}$")
_XCODE_SDK_MAP = {
    "macosx": "macosx",
    "iphoneos": "iphoneos",
    "iphonesimulator": "iphonesimulator",
    "appletvos": "appletvos",
    "appletvsimulator": "appletvsimulator",
    "watchos": "watchos",
    "watchsimulator": "watchsimulator",
    "xros": "xros",
    "xrsimulator": "xrsimulator",
    "driverkit": "driverkit",
}
_APPLE_IGNORED_COMPONENTS = {
    ".build",
    ".git",
    ".swiftpm",
    "DerivedData",
    "Pods",
    "build",
    "node_modules",
}
_APPLE_SENSITIVE_NAMES = {
    ".env",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "service-account.json",
}
_APPLE_CODE_SUFFIXES = {".c", ".cc", ".cpp", ".h", ".m", ".metal", ".mm", ".swift"}
_PLAIN_SWIFT_MAX_FILES = 128


class ProductionRoutingError(RuntimeError):
    """A live route plan or verifier invocation could not be formed truthfully."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ProductionRoutingError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProductionRoutingError(f"{label} did not return unique-key UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ProductionRoutingError(f"{label} did not return one JSON object")
    return value


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sequence(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


@dataclass(frozen=True)
class DetectedTask:
    language: str
    task_kind: str
    role: str
    capability: str
    context_tokens: int
    tool_needs: tuple[str, ...]
    signals: tuple[str, ...]
    apple_mode: str | None = None
    apple_command: tuple[str, ...] = ()
    swift_mutable: tuple[str, ...] = ()
    swift_context: tuple[str, ...] = ()
    swift_verify_context: tuple[str, ...] = ()

    @property
    def is_swift(self) -> bool:
        return self.language == "swift"

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "task_kind": self.task_kind,
            "role": self.role,
            "capability": self.capability,
            "context_tokens": self.context_tokens,
            "tool_needs": list(self.tool_needs),
            "signals": list(self.signals),
            "apple_mode": self.apple_mode,
            "apple_command": list(self.apple_command),
            "swift_defaults": {
                "mutable": list(self.swift_mutable),
                "context": list(self.swift_context),
                "verify_context": list(self.swift_verify_context),
            }
            if self.is_swift
            else None,
        }


def _objective_kind(objective: str) -> tuple[str, str]:
    lowered = objective.casefold()
    mutation_words = (
        "add",
        "build",
        "change",
        "create",
        "fix",
        "implement",
        "make",
        "remove",
        "refactor",
        "update",
        "wire",
    )
    inspection_words = ("analyze", "audit", "diagnose", "explain", "inspect", "review")
    if any(word in lowered for word in inspection_words) and not any(
        word in lowered for word in mutation_words
    ):
        return "inspection", "inspect"
    return "implementation", "implement"


def _top_level_suffixes(repository: Path) -> set[str]:
    suffixes: set[str] = set()
    for root in (repository, repository / "src"):
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())[:4096]
        except OSError:
            continue
        suffixes.update(child.suffix.casefold() for child in children if child.is_file())
    return suffixes


def _real_sorted(root: Path, pattern: str, *, directories: bool = False) -> list[Path]:
    values: list[Path] = []
    for path in root.glob(pattern):
        if path.is_symlink():
            raise ProductionRoutingError(f"Apple project marker is a symlink: {path.name}")
        if (path.is_dir() if directories else path.is_file()):
            values.append(path)
    return sorted(values, key=lambda item: item.name.casefold())


def _source_derived_xcode_sdk(repository: Path, projects: Sequence[Path]) -> str:
    settings = [project / "project.pbxproj" for project in projects]
    settings.extend(
        path
        for path in repository.rglob("*.xcconfig")
        if not any(part in _APPLE_IGNORED_COMPONENTS for part in path.parts)
    )
    sdk_values: set[str] = set()
    platform_values: set[str] = set()
    for path in sorted(set(settings), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ProductionRoutingError(
                f"cannot read Xcode build settings: {path.name}"
            ) from error
        sdk_values.update(
            match.casefold()
            for match in re.findall(r"\bSDKROOT\s*=\s*[\"']?([A-Za-z0-9._-]+)", text)
            if "$" not in match
        )
        for quoted, plain in re.findall(
            r"\bSUPPORTED_PLATFORMS\s*=\s*(?:\"([^\"]+)\"|([^;]+));", text
        ):
            platform_values.update((quoted or plain).casefold().split())
    normalized = {
        _XCODE_SDK_MAP[value]
        for value in sdk_values
        if value in _XCODE_SDK_MAP
    }
    if not normalized:
        normalized = {
            _XCODE_SDK_MAP[value]
            for value in platform_values
            if value in _XCODE_SDK_MAP
        }
    if len(normalized) != 1:
        raise ProductionRoutingError(
            "Xcode SDK is absent or ambiguous in source SDKROOT/SUPPORTED_PLATFORMS settings"
        )
    return next(iter(normalized))


def _apple_top_level_scopes(repository: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    readable: list[str] = []
    hidden: list[str] = []
    for path in sorted(repository.iterdir(), key=lambda item: item.name.casefold()):
        name = path.name
        lowered = name.casefold()
        if (
            name in _APPLE_IGNORED_COMPONENTS
            or name in _APPLE_SENSITIVE_NAMES
            or name.startswith(".env.")
            or path.is_symlink()
        ):
            continue
        if path.is_dir() and (lowered == "tests" or lowered.endswith("tests")):
            hidden.append(name)
            continue
        if path.is_dir() or path.is_file():
            readable.append(name)
    if not readable:
        raise ProductionRoutingError("Apple repository has no bounded model-readable source")
    return tuple(readable), tuple(hidden)


def _xcode_contract(
    repository: Path,
) -> tuple[tuple[str, ...], str, tuple[str, ...], tuple[str, ...]] | None:
    workspaces = _real_sorted(repository, "*.xcworkspace", directories=True)
    projects = _real_sorted(repository, "*.xcodeproj", directories=True)
    if not workspaces and not projects:
        return None
    if len(workspaces) > 1 or (not workspaces and len(projects) != 1):
        raise ProductionRoutingError("Xcode container selection is ambiguous")
    container = workspaces[0] if workspaces else projects[0]
    container_flag = "-workspace" if workspaces else "-project"
    scheme_roots = [container, *projects]
    scheme_files = sorted(
        {
            path.resolve(): path
            for root in scheme_roots
            for path in root.glob("xcshareddata/xcschemes/*.xcscheme")
            if path.is_file() and not path.is_symlink()
        }.values(),
        key=lambda item: item.as_posix().casefold(),
    )
    if len(scheme_files) != 1:
        raise ProductionRoutingError("Xcode route requires exactly one shared scheme")
    scheme = scheme_files[0].stem
    if _XCODE_SCHEME_SAFE.fullmatch(scheme) is None:
        raise ProductionRoutingError("Xcode shared scheme name is outside the argv contract")
    try:
        scheme_raw = scheme_files[0].read_bytes()
    except OSError as error:
        raise ProductionRoutingError("cannot read the shared Xcode scheme") from error
    if (
        len(scheme_raw) > 1024 * 1024
        or b"<!DOCTYPE" in scheme_raw.upper()
        or b"<!ENTITY" in scheme_raw.upper()
    ):
        raise ProductionRoutingError("shared Xcode scheme XML is outside the bounded contract")
    try:
        scheme_xml = ET.fromstring(scheme_raw)
    except ET.ParseError as error:
        raise ProductionRoutingError("shared Xcode scheme is not well-formed XML") from error
    if scheme_xml.tag != "Scheme":
        raise ProductionRoutingError("shared Xcode scheme root differs")
    referenced = {
        item.attrib.get("ReferencedContainer", "")
        for item in scheme_xml.iter("BuildableReference")
    }
    if not referenced:
        raise ProductionRoutingError("shared Xcode scheme has no buildable reference")
    for reference in referenced:
        if not reference.startswith("container:"):
            raise ProductionRoutingError("shared Xcode scheme reference is not container-bound")
        relative = _relative_scope((reference.removeprefix("container:"),), "scheme reference")[0]
        if not (repository / Path(relative)).exists():
            raise ProductionRoutingError("shared Xcode scheme references a missing container")
    sdk = _source_derived_xcode_sdk(repository, projects)
    command = (
        "xcodebuild",
        container_flag,
        container.name,
        "-scheme",
        scheme,
        "-sdk",
        sdk,
        "-derivedDataPath",
        ".ci-derived-data",
        "-quiet",
        "CODE_SIGNING_ALLOWED=NO",
        "CODE_SIGNING_REQUIRED=NO",
        "build",
    )
    readable, hidden = _apple_top_level_scopes(repository)
    return command, sdk, readable, hidden


def _plain_swift_files(repository: Path) -> tuple[str, ...]:
    output: list[str] = []
    for directory, directories, files in os.walk(repository, topdown=True, followlinks=False):
        directories[:] = sorted(
            name
            for name in directories
            if name not in _APPLE_IGNORED_COMPONENTS
            and not name.startswith(".")
            and not name.casefold().endswith("tests")
        )
        directory_path = Path(directory)
        for name in sorted(files):
            path = directory_path / name
            if path.suffix.casefold() != ".swift" or path.is_symlink():
                continue
            output.append(path.relative_to(repository).as_posix())
            if len(output) > _PLAIN_SWIFT_MAX_FILES:
                raise ProductionRoutingError("plain Swift source exceeds the 128-file argv bound")
    return tuple(sorted(output))


def detect_repository_task(
    repository: Path | str,
    objective: str,
    *,
    context_tokens: int = 262_144,
) -> DetectedTask:
    """Detect the production language/task without reading model-generated claims."""

    root = Path(repository).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ProductionRoutingError("repository must be a directory")
    if not isinstance(objective, str) or not objective.strip():
        raise ProductionRoutingError("objective must be non-empty")
    if (
        not isinstance(context_tokens, int)
        or isinstance(context_tokens, bool)
        or context_tokens < 1
    ):
        raise ProductionRoutingError("context_tokens must be a positive integer")

    signals: list[str] = []
    apple_mode: str | None = None
    apple_command: tuple[str, ...] = ()
    mutable: list[str] = []
    context: list[str] = []
    verify_context: list[str] = []
    xcode = None if (root / "Package.swift").is_file() else _xcode_contract(root)
    if (root / "Package.swift").is_file():
        language = "swift"
        tests_present = (root / "Tests").is_dir() and any(
            path.is_file() and not path.is_symlink()
            for path in (root / "Tests").rglob("*.swift")
        )
        apple_mode = "swift-package-test" if tests_present else "swift-package-build"
        apple_command = ("swift", "test" if tests_present else "build")
        signals.append("Package.swift")
        mutable = [
            candidate
            for candidate in ("Sources", "Package.swift")
            if (root / candidate).exists()
        ]
        context = [
            candidate
            for candidate in ("Package.swift", "Package.resolved", "Sources", "Plugins")
            if (root / candidate).exists()
        ]
        verify_context = ["Tests"] if tests_present else []
    elif xcode is not None:
        apple_command, sdk, mutable, verify_context = xcode
        language = "swift"
        apple_mode = "xcode-shared-scheme-build"
        context = list(mutable)
        mutable = list(mutable)
        verify_context = list(verify_context)
        signals.extend(("xcode-shared-scheme", f"source-sdk:{sdk}"))
    elif (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
        language = "python"
        signals.append("pyproject-or-setup")
    elif (root / "package.json").is_file():
        language = "typescript"
        signals.append("package.json")
    elif (root / "Cargo.toml").is_file():
        language = "rust"
        signals.append("Cargo.toml")
    elif (root / "go.mod").is_file():
        language = "go"
        signals.append("go.mod")
    elif list(root.glob("*.sln")):
        language = "dotnet"
        signals.append("solution")
    else:
        suffixes = _top_level_suffixes(root)
        if ".ps1" in suffixes:
            language = "powershell"
            signals.append("powershell-source")
        elif ".py" in suffixes:
            language = "python"
            signals.append("python-source")
        elif suffixes & {".ts", ".tsx", ".js", ".jsx"}:
            language = "typescript"
            signals.append("javascript-or-typescript-source")
        else:
            swift_files = _plain_swift_files(root)
            if swift_files:
                language = "swift"
                apple_mode = "swift-typecheck"
                apple_command = ("xcrun", "swiftc", "-typecheck", *swift_files)
                mutable = list(swift_files)
                context = list(swift_files)
                verify_context = []
                signals.append("swift-source-without-root-manifest")
            else:
                language = "unknown"
                signals.append("no-supported-language-marker")

    task_kind, role = _objective_kind(objective)
    tools = ["edit", "list", "pwsh", "read", "search", "test"]
    if language == "swift":
        tools.extend(("swift_toolchain", "xcode"))
        if not mutable or not context or not apple_mode or not apple_command:
            raise ProductionRoutingError("Swift repository lacks an executable Apple command")

    return DetectedTask(
        language=language,
        task_kind=task_kind,
        role=role,
        capability="interactive_agent",
        context_tokens=context_tokens,
        tool_needs=tuple(sorted(tools)),
        signals=tuple(signals),
        apple_mode=apple_mode,
        apple_command=apple_command,
        swift_mutable=tuple(mutable),
        swift_context=tuple(context),
        swift_verify_context=tuple(verify_context),
    )


@dataclass(frozen=True)
class RuntimeCapture:
    routing: RuntimeState
    doctor_sha256: str | None
    active_backend: str | None
    target_backend: str
    target_model: str
    observed_model: str | None
    endpoint: str
    backend_switch_required: bool
    deepseek: dict[str, Any]
    mac_edge: dict[str, Any]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "routing": self.routing.canonical(),
            "doctor_sha256": self.doctor_sha256,
            "active_backend": self.active_backend,
            "target_backend": self.target_backend,
            "target_model": self.target_model,
            "observed_model": self.observed_model,
            "endpoint": self.endpoint,
            "backend_switch_required": self.backend_switch_required,
            "deepseek": self.deepseek,
            "mac_edge": self.mac_edge,
            "errors": list(self.errors),
        }


def _run_doctor(timeout_seconds: int) -> tuple[dict[str, Any], str]:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None or not DOCTOR.is_file():
        raise ProductionRoutingError("read-only production doctor is unavailable")
    command = [
        powershell,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(DOCTOR),
        "-Json",
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProductionRoutingError(f"production doctor could not run: {error}") from error
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 4 * 1024 * 1024:
        raise ProductionRoutingError("production doctor did not return a bounded success report")
    value = _json_object(result.stdout, "production doctor")
    operation = _mapping(value.get("operation"))
    if operation.get("readOnly") is not True or operation.get("changesMade") is not False:
        raise ProductionRoutingError("production doctor did not prove a read-only capture")
    return value, _sha256(result.stdout)


def _model_is_installed(doctor: dict[str, Any], name: str) -> bool:
    for candidate in _sequence(doctor.get("models")):
        item = _mapping(candidate)
        if item.get("name") != name or item.get("installed") is not True:
            continue
        location = item.get("location")
        return isinstance(location, str) and bool(location) and Path(location).exists()
    return False


def _task_is_installed(backend: dict[str, Any]) -> bool:
    task = _mapping(backend.get("task"))
    return task.get("installed") is True and task.get("state") in {"Ready", "Running"}


def _native_is_live(backend: dict[str, Any]) -> bool:
    listener = _mapping(backend.get("listener"))
    health = _mapping(backend.get("health"))
    ownership = _mapping(backend.get("ownership"))
    return (
        backend.get("liveReady") is True
        and listener.get("count") == 1
        and listener.get("exactIpv4Loopback") is True
        and listener.get("anyNonLoopback") is False
        and health.get("status") == "ready"
        and health.get("exactModelPresent") is True
        and ownership.get("status") == "exact"
        and ownership.get("exact") is True
    )


def _load_mac_verifier_module() -> Any:
    spec = importlib.util.spec_from_file_location("_ci_mac_swift_verifier_probe", MAC_VERIFIER)
    if spec is None or spec.loader is None:
        raise ProductionRoutingError("trusted Mac verifier cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _probe_mac_edge(timeout_seconds: int) -> dict[str, Any]:
    try:
        module = _load_mac_verifier_module()
        ssh = shutil.which("ssh")
        if ssh is None:
            raise ProductionRoutingError("OpenSSH client is unavailable")
        resolved = str(Path(ssh).resolve())
        if os.name == "nt":
            expected = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/OpenSSH/ssh.exe"
            if os.path.normcase(resolved) != os.path.normcase(str(expected.resolve())):
                raise ProductionRoutingError("trusted Mac probe requires Windows OpenSSH")
        configuration = module._ssh_configuration(resolved)
        options = (
            "BatchMode=yes",
            "ConnectionAttempts=1",
            f"ConnectTimeout={max(1, min(timeout_seconds, 10))}",
            "NumberOfPasswordPrompts=0",
            "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no",
            "PreferredAuthentications=publickey",
            "IdentitiesOnly=yes",
            "StrictHostKeyChecking=yes",
            "HostKeyAlgorithms=ssh-ed25519",
            "UpdateHostKeys=no",
            "ClearAllForwardings=yes",
            "ForwardAgent=no",
            "ForwardX11=no",
            "PermitLocalCommand=no",
            "ControlMaster=no",
            "ControlPath=none",
            "ControlPersist=no",
            "RequestTTY=no",
            "LogLevel=ERROR",
        )
        command = [resolved]
        for option in options:
            command.extend(("-o", option))
        command.extend(("-T", module.SSH_ALIAS, "/usr/bin/true"))
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise ProductionRoutingError(
                f"trusted Mac authentication probe exited {result.returncode}"
            )
        return {
            "status": "ready",
            "configuration": configuration,
            "probe": "pinned-key-batch-authenticated",
            "stdout_sha256": _sha256(result.stdout),
            "stderr_sha256": _sha256(result.stderr),
        }
    except Exception as error:
        # Mac verifier errors come from a dynamically loaded module and cannot be
        # named statically here.  The error text is bounded and never includes key material.
        return {"status": "unavailable", "error": str(error)[:1000]}


def capture_runtime_state(
    task: DetectedTask,
    *,
    cloud_available: bool = False,
    doctor_timeout_seconds: int = 30,
    mac_probe_timeout_seconds: int = 15,
) -> RuntimeCapture:
    """Capture current operability; cloud authority is explicit and off by default."""

    if not isinstance(cloud_available, bool):
        raise ProductionRoutingError("cloud_available must be boolean")
    errors: list[str] = []
    doctor: dict[str, Any] = {}
    doctor_sha256: str | None = None
    try:
        doctor, doctor_sha256 = _run_doctor(doctor_timeout_seconds)
    except ProductionRoutingError as error:
        errors.append(str(error))

    backends = _mapping(doctor.get("backends"))
    native = _mapping(backends.get("qwen38Native"))
    ollama = _mapping(backends.get("ollama"))
    active_backend = doctor.get("activeBackend")
    if not isinstance(active_backend, str):
        active_backend = None
    native_live = _native_is_live(native)
    native_health = _mapping(native.get("health"))
    observed_model = native_health.get("modelAlias") if native_live else None
    if not isinstance(observed_model, str):
        observed_model = None
    alias_matches = not native_live or observed_model == TARGET_MODEL
    if native_live and not alias_matches:
        errors.append(f"native backend exposes unexpected model alias {observed_model!r}")

    qwen_installed = _model_is_installed(doctor, "Qwen3.8 27B Q6")
    devstral_installed = _model_is_installed(doctor, "Devstral Small 2 24B")
    qwen_operable = (
        qwen_installed and _task_is_installed(native) and SWITCHER.is_file() and alias_matches
    )
    devstral_operable = devstral_installed and _task_is_installed(ollama) and SWITCHER.is_file()

    models: set[str] = set()
    harnesses: set[str] = set()
    edges: set[str] = set()
    if qwen_operable:
        models.add("qwen3.8-27b-q6")
        edges.add("excalibur")
    if devstral_operable:
        models.add("devstral-small-2-24b")
        harnesses.add("local-structured-verifier")

    deepseek: dict[str, Any]
    try:
        identity = DeepSeekAdapter().validate_install()
        harnesses.add("deepseek-four-tool-adapter")
        deepseek = {
            "status": "ready",
            "node_version": identity["node_version"],
            "manifest_sha256": identity["manifest"],
            "cli_sha256": identity["cli"],
            "tool_surface": ["edit", "list", "pwsh", "read", "search", "test"],
        }
    except (AdapterContractError, OSError, ValueError) as error:
        deepseek = {"status": "unavailable", "error": str(error)[:1000]}
        errors.append("DeepSeek production harness is unavailable")

    thin_runner = PACKAGE_ROOT / "src" / "agent_continuity" / "local_edit.py"
    if thin_runner.is_file():
        harnesses.add("thin-structured-edit")

    if task.is_swift:
        mac_edge = _probe_mac_edge(mac_probe_timeout_seconds)
        if mac_edge.get("status") == "ready":
            edges.add("mac-apple")
    else:
        mac_edge = {"status": "not_required"}

    if cloud_available:
        models.add("hosted-codex-5.6-sol")
        harnesses.add("hosted-codex-app")
        edges.add("hosted-cloud")

    state = RuntimeState(
        cloud_available=cloud_available,
        operable_model_ids=frozenset(models),
        operable_harness_ids=frozenset(harnesses),
        available_edge_ids=frozenset(edges),
        satisfied_runtime_tags=frozenset(),
    )
    return RuntimeCapture(
        routing=state,
        doctor_sha256=doctor_sha256,
        active_backend=active_backend,
        target_backend=TARGET_BACKEND,
        target_model=TARGET_MODEL,
        observed_model=observed_model,
        endpoint=TARGET_ENDPOINT,
        backend_switch_required=active_backend != TARGET_BACKEND or not native_live,
        deepseek=deepseek,
        mac_edge=mac_edge,
        errors=tuple(errors),
    )


@dataclass(frozen=True)
class ProductionRoutePlan:
    repository: Path
    objective_sha256: str
    task: DetectedTask
    runtime: RuntimeCapture
    decision: RoutingDecision
    manifest_path: Path
    implementation: dict[str, Any] | None
    verification: dict[str, Any] | None

    @property
    def executable(self) -> bool:
        return self.implementation is not None and self.decision.status == "routed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "status": "planned" if self.executable else "unroutable",
            "executable": self.executable,
            "repository": str(self.repository),
            "objective_sha256": self.objective_sha256,
            "task": self.task.to_dict(),
            "runtime": self.runtime.to_dict(),
            "routing_manifest": {
                "path": str(self.manifest_path),
                "sha256": self.decision.manifest_sha256,
            },
            "decision": self.decision.to_dict(),
            "implementation": self.implementation,
            "verification": self.verification,
            "automatic_retries": 0,
            "cloud_authority": "explicit-only",
        }


def _task_id(repository: Path, objective: str) -> str:
    stem = _ID_SAFE.sub("-", repository.name.casefold()).strip("-._") or "repository"
    digest = hashlib.sha256((str(repository) + "\0" + objective).encode("utf-8")).hexdigest()[:16]
    return f"production-{stem[:32]}-{digest}"


def build_production_route_plan(
    repository: Path | str,
    objective: str,
    *,
    manifest_path: Path | str = DEFAULT_MANIFEST,
    context_tokens: int = 262_144,
    cloud_available: bool = False,
) -> ProductionRoutePlan:
    """Return one evidence-bound, executable local route plan."""

    root = Path(repository).expanduser().resolve(strict=True)
    task = detect_repository_task(root, objective, context_tokens=context_tokens)
    runtime = capture_runtime_state(task, cloud_available=cloud_available)
    resolved_manifest = Path(manifest_path).expanduser().resolve(strict=True)
    manifest = load_routing_manifest(resolved_manifest)
    request = RoutingRequest(
        task_id=_task_id(root, objective),
        role=task.role,
        capability=task.capability,
        language=task.language,
        context_tokens=task.context_tokens,
        tool_needs=task.tool_needs,
    )
    decision = select_route(manifest, request, runtime.routing)

    expected_route = DEEPSEEK_SWIFT_ROUTE if task.is_swift else DEEPSEEK_ROUTE
    implementation: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    if decision.selected_route_id == expected_route:
        implementation = {
            "kind": "deepseek-native-local",
            "worker": "agent_continuity.production_worker.run",
            "worker_kwargs": {
                "harness": "deepseek",
                "backend": runtime.target_backend,
                "endpoint": runtime.endpoint,
                "model": runtime.target_model,
            },
            "harness_id": decision.harness_id,
            "inference_edge_id": decision.inference_edge_id,
            "execution_edge_id": decision.execution_edge_id,
            "backend_switch_required": runtime.backend_switch_required,
            "backend_switch_argv": [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SWITCHER),
                "-Backend",
                runtime.target_backend,
            ],
        }
        if task.is_swift:
            verification = {
                "kind": "mac-swift",
                "authority": "deterministic-execution",
                "verifier_program": str(MAC_VERIFIER),
                "prepare_helper": (
                    "agent_continuity.production_routing.prepare_mac_swift_verification"
                ),
                "run_helper": "agent_continuity.production_routing.run_mac_swift_verifier",
                "execution_verifier": "mac-swift",
                "command_mode": task.apple_mode,
                "test_command": list(task.apple_command),
                "mutable": list(task.swift_mutable),
                "context": list(task.swift_context),
                "verify_context": list(task.swift_verify_context),
                "model_location": "pc-excalibur",
                "execution_location": "trusted-mac-edge",
            }
        else:
            verification = {
                "kind": "repository-native",
                "authority": "deterministic-execution",
                "command_source": "agent_continuity.production_worker._verification_command",
                "advisory_verifier_id": decision.verifier_id,
            }

    return ProductionRoutePlan(
        repository=root,
        objective_sha256=_sha256(objective.encode("utf-8")),
        task=task,
        runtime=runtime,
        decision=decision,
        manifest_path=resolved_manifest,
        implementation=implementation,
        verification=verification,
    )


def _relative_scope(
    values: Sequence[str], label: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value:
            raise ProductionRoutingError(f"{label} contains an invalid relative path")
        posix = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if (
            posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or value in {".", ".."}
            or ".." in posix.parts
            or any(part in {"", "."} for part in posix.parts)
        ):
            raise ProductionRoutingError(f"{label} must stay inside the repository")
        normalized = posix.as_posix()
        if normalized not in output:
            output.append(normalized)
    if not output and not allow_empty:
        raise ProductionRoutingError(f"{label} must be non-empty")
    return tuple(output)


def _scope_contains(child: str, parent: str) -> bool:
    child_path = PurePosixPath(child)
    parent_path = PurePosixPath(parent)
    return child_path == parent_path or parent_path in child_path.parents


def _run_verifier_json(command: Sequence[str], *, timeout_seconds: int) -> dict[str, Any]:
    try:
        result = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProductionRoutingError(f"Mac Swift verifier could not run: {error}") from error
    if not result.stdout or len(result.stdout) > 2 * 1024 * 1024:
        raise ProductionRoutingError("Mac Swift verifier returned no bounded report")
    report = _json_object(result.stdout, "Mac Swift verifier")
    if result.returncode != 0 or report.get("status") != "verified":
        nested = _mapping(report.get("result"))
        test = _mapping(nested.get("test"))
        error = str(
            report.get("error")
            or nested.get("error")
            or test.get("error")
            or "authoritative Mac Swift verification failed"
        )
        raise ProductionRoutingError(error[:2000])
    return report


@dataclass(frozen=True)
class MacSwiftVerifierInvocation:
    task_path: Path
    task_sha256: str
    source_snapshot_sha256: str
    verifier_path: Path
    remote_timeout_seconds: int
    tool_timeout_seconds: int
    command_mode: str
    test_command: tuple[str, ...]

    def command(self, stage: Path | str = ".") -> tuple[str, ...]:
        return (
            sys.executable,
            str(self.verifier_path),
            "verify",
            "--task",
            str(self.task_path),
            "--expected-task-sha256",
            self.task_sha256,
            "--stage",
            str(stage),
            "--expected-source-snapshot-sha256",
            self.source_snapshot_sha256,
            "--timeout",
            str(self.remote_timeout_seconds),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": MAC_INVOCATION_SCHEMA,
            "task_path": str(self.task_path),
            "task_sha256": self.task_sha256,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "remote_timeout_seconds": self.remote_timeout_seconds,
            "tool_timeout_seconds": self.tool_timeout_seconds,
            "command_mode": self.command_mode,
            "test_command": list(self.test_command),
            "stage_command_template": list(self.command(".")),
            "automatic_retries": 0,
        }


def prepare_mac_swift_verification(
    *,
    repository: Path | str,
    objective: str,
    mutable: Sequence[str],
    context: Sequence[str],
    verify_context: Sequence[str],
    run_root: Path | str,
    timeout_seconds: int = 1800,
) -> MacSwiftVerifierInvocation:
    """Checkpoint an Apple source tree and return the worker's real verifier argv."""

    source = Path(repository).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ProductionRoutingError("Mac Swift verification requires a repository")
    if not isinstance(objective, str) or not objective.strip():
        raise ProductionRoutingError("Mac Swift verification objective must be non-empty")
    if not 60 <= timeout_seconds <= 1800:
        raise ProductionRoutingError("Swift verification timeout must be 60..1800 seconds")
    mutable_paths = _relative_scope(mutable, "mutable")
    context_paths = _relative_scope(context, "context")
    hidden_paths = _relative_scope(verify_context, "verify_context", allow_empty=True)
    detected = detect_repository_task(source, objective)
    if not detected.is_swift or not detected.apple_mode or not detected.apple_command:
        raise ProductionRoutingError("repository has no deterministic Apple verification command")
    readable_paths = (*mutable_paths, *context_paths)
    for required in detected.swift_context:
        if not any(_scope_contains(required, selected) for selected in readable_paths):
            raise ProductionRoutingError(
                f"Apple command source is outside the checkpoint scope: {required}"
            )
    for required in detected.swift_verify_context:
        if not any(_scope_contains(required, selected) for selected in hidden_paths):
            raise ProductionRoutingError(
                f"Apple verifier source is outside hidden scope: {required}"
            )
    if any(
        _scope_contains(readable, hidden) or _scope_contains(hidden, readable)
        for readable in (*mutable_paths, *context_paths)
        for hidden in hidden_paths
    ):
        raise ProductionRoutingError("hidden Swift verifier scope overlaps model-readable scope")
    for item in set(mutable_paths + context_paths + hidden_paths):
        if not (source / Path(item)).exists():
            raise ProductionRoutingError(f"Swift verifier path is missing: {item}")

    owned_root = Path(run_root).expanduser().resolve()
    try:
        owned_root.relative_to(source)
    except ValueError:
        pass
    else:
        raise ProductionRoutingError("Mac Swift task checkpoint must stay outside the repository")
    owned_root.mkdir(parents=True, exist_ok=True)
    task_path = owned_root / "mac-swift-task.json"
    task = {
        "schema_version": 1,
        "backend": TARGET_BACKEND,
        "workspace": str(source),
        "objective": objective,
        "mutable": list(mutable_paths),
        "context": list(context_paths),
        "verify_context": list(hidden_paths),
        "test_command": list(detected.apple_command),
        "timeout": timeout_seconds,
        "execution_verifier": "mac-swift",
    }
    raw = _canonical_bytes(task) + b"\n"
    try:
        with task_path.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as error:
        raise ProductionRoutingError("Mac Swift task checkpoint already exists") from error
    task_sha256 = _sha256(raw)
    snapshot = _run_verifier_json(
        (
            sys.executable,
            str(MAC_VERIFIER),
            "snapshot",
            "--task",
            str(task_path),
            "--expected-task-sha256",
            task_sha256,
            "--root",
            str(source),
        ),
        timeout_seconds=30,
    )
    if snapshot.get("schema") != MAC_SNAPSHOT_SCHEMA or snapshot.get("task_sha256") != task_sha256:
        raise ProductionRoutingError("Mac Swift source checkpoint identity differs")
    snapshot_sha256 = snapshot.get("manifest_sha256")
    if not isinstance(snapshot_sha256, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", snapshot_sha256
    ):
        raise ProductionRoutingError("Mac Swift source checkpoint hash is invalid")
    return MacSwiftVerifierInvocation(
        task_path=task_path,
        task_sha256=task_sha256,
        source_snapshot_sha256=snapshot_sha256,
        verifier_path=MAC_VERIFIER,
        remote_timeout_seconds=timeout_seconds - 30,
        tool_timeout_seconds=timeout_seconds,
        command_mode=detected.apple_mode,
        test_command=detected.apple_command,
    )


def run_mac_swift_verifier(
    invocation: MacSwiftVerifierInvocation,
    *,
    stage: Path | str,
) -> dict[str, Any]:
    """Run exactly one authoritative Mac verifier session and validate its envelope."""

    report = _run_verifier_json(
        invocation.command(Path(stage).expanduser().resolve(strict=True)),
        timeout_seconds=invocation.remote_timeout_seconds + 30,
    )
    if report.get("schema") != MAC_RESULT_SCHEMA:
        raise ProductionRoutingError("Mac Swift verifier result schema differs")
    checkpoint = _mapping(report.get("checkpoint"))
    if (
        checkpoint.get("task_sha256") != invocation.task_sha256
        or checkpoint.get("source_snapshot_before_sha256")
        != invocation.source_snapshot_sha256
        or checkpoint.get("source_snapshot_after_sha256")
        != invocation.source_snapshot_sha256
        or checkpoint.get("source_workspace_mutated") is not False
        or report.get("model_calls") != 0
        or report.get("automatic_retries") != 0
    ):
        raise ProductionRoutingError("Mac Swift verifier result identity differs")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("objective", nargs="+")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--context-tokens", type=int, default=262_144)
    parser.add_argument(
        "--allow-cloud-route",
        action="store_true",
        help="Explicit operator authority; omitted by the sovereign coding command.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = build_production_route_plan(
            args.repository,
            " ".join(args.objective),
            manifest_path=args.manifest,
            context_tokens=args.context_tokens,
            cloud_available=args.allow_cloud_route,
        )
        value = plan.to_dict()
    except Exception as error:
        value = {"schema": PLAN_SCHEMA, "status": "failed", "error": str(error)[:2000]}
        sys.stdout.write(_canonical_bytes(value).decode("ascii") + "\n")
        return 1
    sys.stdout.write(_canonical_bytes(value).decode("ascii") + "\n")
    return 0 if plan.executable else 2


if __name__ == "__main__":
    raise SystemExit(main())
