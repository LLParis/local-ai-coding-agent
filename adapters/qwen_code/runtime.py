"""Pinned per-user Qwen Code runtime and configuration identity."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..protocol import AdapterContractError, canonical_json

QWEN_CODE_PACKAGE = "@qwen-code/qwen-code"
QWEN_CODE_VERSION = "0.21.15"
QWEN_CODE_GIT_HEAD = "5dce2515a778f9cf2013168962b4fbc3454636e3"
QWEN_CODE_NPM_INTEGRITY = (
    "sha512-f4ER/SRVLpwhcqzuytK3Qeq8bG9HnVhv7f7wsf3cpE/"
    "AkRfzKSvaeURnW7s7zI3nWkEqA7DM6njSLYS2s6DWDg=="
)
QWEN_CODE_TARBALL_SHA256 = "8d405b065888b7000a6989d99c2d79257cd8f9f5b68e9078fb76484527351b9a"

NODE_VERSION = "v24.18.0"
BASEDPYRIGHT_VERSION = "1.39.10"
TYPESCRIPT_LANGUAGE_SERVER_VERSION = "6.0.0"
TYPESCRIPT_VERSION = "7.0.2"

PINNED_FILE_HASHES = {
    "package-lock.json": "a76dd39fe1444d63592a9ff3eac633b7ebd2f9e250af7dd8a6de3f6483b78785",
    "node.exe": "9a4eb5f1c29c6a2e93852ead46b999e284a6a5ca8bab4d4e241d587d025a52de",
    "node_modules/@qwen-code/qwen-code/cli-entry.js": (
        "68cb29eb7ccc936d78ece5564ef55cae41a55b630e6657dc417c1f2e561cf4c9"
    ),
    "node_modules/@qwen-code/qwen-code/package.json": (
        "dc7b0c825626dd3d6f8cecbdced167bad83e5529e3ce958597ed8691569f5711"
    ),
    "node_modules/basedpyright/langserver.index.js": (
        "f200762078eb9880faf421a5b30f9ef3055b1d85c6f10780d75db4da8d8499f1"
    ),
    "node_modules/typescript-language-server/lib/cli.mjs": (
        "146b113929fd41f12fbe7342ba493adce2bf82a07cdc791d0b9678a17572bc46"
    ),
    "node_modules/typescript/bin/tsc": (
        "2219f428a7e55aaf1f7ad85b9b0f0cf5078aeb76ccc9a7c6036c92d48f492ffd"
    ),
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AdapterContractError(
            f"cannot read Qwen Code runtime metadata {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise AdapterContractError(f"Qwen Code runtime metadata is not an object: {path}")
    return value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _default_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise AdapterContractError("LOCALAPPDATA is required for the Qwen Code adapter")
    return Path(local) / "CodingIntelligence" / "Adapters" / "QwenCode"


@dataclass(frozen=True)
class QwenCodeRuntime:
    root: Path

    @classmethod
    def default(cls) -> QwenCodeRuntime:
        return cls(_default_root().resolve())

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def node(self) -> Path:
        return self.runtime / "node.exe"

    @property
    def cli(self) -> Path:
        return self.runtime / "node_modules" / "@qwen-code" / "qwen-code" / "cli-entry.js"

    @property
    def settings(self) -> Path:
        return self.home / "settings.json"

    @property
    def system_defaults(self) -> Path:
        return self.home / "system-defaults.json"

    @property
    def manifest(self) -> Path:
        return self.root / "install-manifest.json"

    @property
    def tarball(self) -> Path:
        return self.artifacts / f"qwen-code-qwen-code-{QWEN_CODE_VERSION}.tgz"

    def _package_manifest(self, relative: str) -> dict[str, Any]:
        return _strict_json(self.runtime / "node_modules" / relative / "package.json")

    def validate(self) -> dict[str, Any]:
        missing = [
            str(self.runtime / relative)
            for relative in PINNED_FILE_HASHES
            if not (self.runtime / relative).is_file()
        ]
        if missing:
            raise AdapterContractError(
                "Qwen Code runtime is incomplete: " + ", ".join(sorted(missing))
            )
        mismatched: list[str] = []
        actual_hashes: dict[str, str] = {}
        for relative, expected in PINNED_FILE_HASHES.items():
            actual = _sha256_file(self.runtime / relative)
            actual_hashes[relative] = actual
            if actual != expected:
                mismatched.append(f"{relative}={actual}")
        if mismatched:
            raise AdapterContractError(
                "Qwen Code pinned runtime hash mismatch: " + ", ".join(mismatched)
            )

        identities = {
            QWEN_CODE_PACKAGE: ("@qwen-code/qwen-code", QWEN_CODE_VERSION),
            "basedpyright": ("basedpyright", BASEDPYRIGHT_VERSION),
            "typescript-language-server": (
                "typescript-language-server",
                TYPESCRIPT_LANGUAGE_SERVER_VERSION,
            ),
            "typescript": ("typescript", TYPESCRIPT_VERSION),
        }
        installed: dict[str, str] = {}
        for relative, (name, expected_version) in identities.items():
            manifest = self._package_manifest(relative)
            version = manifest.get("version")
            if manifest.get("name") != name or version != expected_version:
                raise AdapterContractError(
                    f"Qwen Code dependency identity mismatch for {relative}: "
                    f"{manifest.get('name')}@{version}"
                )
            installed[name] = expected_version

        lock = _strict_json(self.runtime / "package-lock.json")
        lock_entry = lock.get("packages", {}).get("node_modules/@qwen-code/qwen-code", {})
        if (
            not isinstance(lock_entry, dict)
            or lock_entry.get("version") != QWEN_CODE_VERSION
            or lock_entry.get("integrity") != QWEN_CODE_NPM_INTEGRITY
        ):
            raise AdapterContractError("Qwen Code lock identity/integrity does not match the pin")

        version = subprocess.run(
            [str(self.node), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if version.returncode != 0 or version.stdout.strip() != NODE_VERSION:
            raise AdapterContractError(
                f"pinned Node runtime is not {NODE_VERSION}: {version.stdout.strip()}"
            )

        if not self.tarball.is_file():
            raise AdapterContractError(f"Qwen Code pinned npm tarball is missing: {self.tarball}")
        tarball_hash = _sha256_file(self.tarball)
        if tarball_hash != QWEN_CODE_TARBALL_SHA256:
            raise AdapterContractError(f"Qwen Code npm tarball hash mismatch: {tarball_hash}")

        return {
            "qwen_code": {
                "package": QWEN_CODE_PACKAGE,
                "version": QWEN_CODE_VERSION,
                "git_head": QWEN_CODE_GIT_HEAD,
                "npm_integrity": QWEN_CODE_NPM_INTEGRITY,
                "tarball_sha256": tarball_hash,
            },
            "node": {"version": NODE_VERSION, "sha256": actual_hashes["node.exe"]},
            "dependencies": installed,
            "files": actual_hashes,
        }

    def deploy_config(self, template: Path | None = None) -> dict[str, Any]:
        source = template or Path(__file__).with_name("settings.json")
        settings = _strict_json(source)
        runtime = self.validate()
        self.home.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        _atomic_json(self.settings, settings)
        _atomic_json(self.system_defaults, {})
        installed = {
            "schema_version": 1,
            "installed_at": datetime.now(UTC).isoformat(),
            "root": str(self.root),
            "runtime": runtime,
            "settings": {
                "path": str(self.settings),
                "sha256": _sha256_file(self.settings),
                "source_sha256": _sha256_file(source),
            },
            "policy": {
                "endpoint": "http://127.0.0.1:8818/v1",
                "model": "arm-qwen38-q6-text",
                "context_tokens": 32768,
                "output_tokens": 8192,
                "transport_retries": 0,
                "qwen_auto_memory": False,
                "cloud_fallback": False,
                "telemetry": False,
                "extensions": False,
                "background_agents": False,
            },
        }
        _atomic_json(self.manifest, installed)
        return installed

    def environment(self, runtime_state: Path) -> dict[str, str]:
        self.validate_deployed()
        environment = dict(os.environ)
        for name in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "QWEN_API_KEY",
            "DASHSCOPE_API_KEY",
            "OPENROUTER_API_KEY",
            "AZURE_OPENAI_API_KEY",
            "QWEN_CODE_UNATTENDED_RETRY",
            "QWEN_CODE_ENABLE_AGENT_TEAM",
        ):
            environment.pop(name, None)
        current_path = environment.get("PATH", "")
        runtime_bin = self.runtime / "node_modules" / ".bin"
        environment.update(
            {
                "QWEN_HOME": str(self.home),
                "QWEN_RUNTIME_DIR": str(runtime_state),
                "QWEN_CODE_SYSTEM_DEFAULTS_PATH": str(self.system_defaults),
                "QWEN_CODE_SYSTEM_SETTINGS_PATH": str(self.settings),
                "QWEN_CODE_UNATTENDED_RETRY": "0",
                "QWEN_CODE_ENABLE_AGENT_TEAM": "0",
                "QWEN_CODE_DISABLE_CRON": "1",
                "QWEN_CODE_DISABLE_ARTIFACT": "1",
                "QWEN_CODE_EMIT_TOOL_USE_SUMMARIES": "0",
                "QWEN_CODE_MAX_OUTPUT_TOKENS": "8192",
                "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
                "QWEN_DISABLED_SLASH_COMMANDS": (
                    "auth,mcp,extensions,skills,agents,memory,remember,dream,goal,loop,"
                    "workflows,hooks,permissions,settings,update,quit"
                ),
                "CODING_INTELLIGENCE_LOCAL_API_KEY": "local-only",
                "NO_COLOR": "1",
                "NO_PROXY": "127.0.0.1,localhost,::1",
                "no_proxy": "127.0.0.1,localhost,::1",
                "PATH": f"{runtime_bin}{os.pathsep}{current_path}",
            }
        )
        return environment

    def validate_deployed(self) -> dict[str, Any]:
        runtime = self.validate()
        if not self.settings.is_file() or not self.system_defaults.is_file():
            raise AdapterContractError("Qwen Code isolated configuration has not been deployed")
        configured = _strict_json(self.settings)
        source = _strict_json(Path(__file__).with_name("settings.json"))
        if configured != source:
            raise AdapterContractError("Qwen Code deployed settings drifted from the pinned source")
        return {
            **runtime,
            "settings_sha256": _sha256_file(self.settings),
            "manifest": str(self.manifest),
        }

    def probe_config_load(self, *, cwd: Path) -> dict[str, Any]:
        self.validate_deployed()
        self.state.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="config-probe-", dir=self.state) as temporary:
            command = [
                str(self.node),
                str(self.cli),
                "--list-extensions",
                "--extensions",
                "none",
            ]
            result = subprocess.run(
                command,
                cwd=cwd,
                env=self.environment(Path(temporary)),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        return {
            "command": command,
            "exit_code": result.returncode,
            "loaded": result.returncode == 0,
            "stdout": result.stdout[-8192:],
            "stderr": result.stderr[-8192:],
        }
