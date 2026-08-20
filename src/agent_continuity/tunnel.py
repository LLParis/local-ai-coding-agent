from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .endpoint import EndpointReport, check_endpoint

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by the Windows CLI test
    fcntl = None  # type: ignore[assignment]


class TunnelError(RuntimeError):
    pass


@dataclass(frozen=True)
class TunnelSpec:
    host: str = "excalibur"
    local_port: int = 12434
    remote_host: str = "127.0.0.1"
    remote_port: int = 11434


@dataclass(frozen=True)
class TunnelReport:
    action: str
    endpoint: EndpointReport
    listener: str


_HOST_ALIAS = re.compile(r"^[A-Za-z0-9._-]+$")
_LSOF = "/usr/sbin/lsof"
_SSH = "/usr/bin/ssh"


def _runtime_dir() -> Path:
    if not hasattr(os, "getuid"):
        raise TunnelError("the SSH tunnel owner is available only on macOS")
    override = os.environ.get("CODING_INTELLIGENCE_RUNTIME_DIR") or os.environ.get(
        "ANIME_FRONTIER_CONTINUITY_RUNTIME_DIR"
    )
    path = Path(override) if override else Path(f"/tmp/coding-intelligence-continuity-{os.getuid()}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise TunnelError(f"unsafe runtime path: {path}")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise TunnelError(f"runtime directory must be owned by this user and mode 0700: {path}")
    return path


def _run(command: list[str], *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TunnelError(f"command failed: {command[0]} ({type(exc).__name__})") from exc


def listener_names(port: int) -> list[str]:
    result = _run([_LSOF, "-nP", "-a", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "cn"])
    if result.returncode not in {0, 1}:
        raise TunnelError("could not inspect the local listening socket")
    command: str | None = None
    names: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("c"):
            command = line[1:]
        elif line.startswith("n"):
            if command != "ssh":
                raise TunnelError(f"port {port} is not owned by an SSH forward")
            names.append(line[1:])
    return sorted(set(names))


def require_loopback_listener(port: int) -> str:
    names = listener_names(port)
    if not names:
        raise TunnelError(f"nothing is listening on local port {port}")
    for name in names:
        host = name.rsplit(":", 1)[0].strip("[]")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise TunnelError(f"port {port} is exposed beyond loopback; refusing to continue")
    return ",".join(names)


def _validate_spec(spec: TunnelSpec) -> None:
    if not _HOST_ALIAS.fullmatch(spec.host):
        raise TunnelError("SSH host must be a simple configured alias")
    if spec.remote_host not in {"127.0.0.1", "localhost", "::1"}:
        raise TunnelError("remote endpoint must also be loopback-only")
    for value in (spec.local_port, spec.remote_port):
        if value < 1024 or value > 65535:
            raise TunnelError("tunnel ports must be between 1024 and 65535")


def ensure_tunnel(
    spec: TunnelSpec,
    *,
    model: str = "gpt-oss:20b",
    timeout: float = 90.0,
) -> TunnelReport:
    if sys.platform != "darwin" or fcntl is None:
        raise TunnelError("tunnel-ensure is a macOS edge command; use local-edit directly on Windows")
    _validate_spec(spec)
    runtime = _runtime_dir()
    control = runtime / f"{spec.host}.sock"
    lock_path = runtime / f"{spec.host}-{spec.local_port}.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = listener_names(spec.local_port)
        if existing:
            master = _run([_SSH, "-S", str(control), "-O", "check", spec.host])
            if master.returncode != 0:
                raise TunnelError(
                    f"port {spec.local_port} is in use but the ControlMaster for {spec.host} is not alive; "
                    "refusing to probe or modify an unrelated listener"
                )
            listener = require_loopback_listener(spec.local_port)
            endpoint = check_endpoint(f"http://127.0.0.1:{spec.local_port}/v1", model, timeout=timeout)
            return TunnelReport("reused", endpoint, listener)

        forward = f"127.0.0.1:{spec.local_port}:{spec.remote_host}:{spec.remote_port}"
        master = _run([_SSH, "-S", str(control), "-O", "check", spec.host])
        if master.returncode == 0:
            opened = _run([_SSH, "-S", str(control), "-O", "forward", "-L", forward, spec.host])
            action = "forward-added"
        else:
            if control.exists():
                if not stat.S_ISSOCK(control.lstat().st_mode):
                    raise TunnelError(f"refusing to replace non-socket control path: {control}")
                control.unlink()
            opened = _run(
                [
                    _SSH,
                    "-M",
                    "-S",
                    str(control),
                    "-o",
                    "ControlMaster=auto",
                    "-o",
                    "ControlPersist=600",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "-f",
                    "-N",
                    "-L",
                    forward,
                    spec.host,
                ]
            )
            action = "master-started"
        if opened.returncode:
            raise TunnelError(f"SSH tunnel setup failed (exit {opened.returncode})")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not listener_names(spec.local_port):
            time.sleep(0.1)
        listener = require_loopback_listener(spec.local_port)
        endpoint = check_endpoint(f"http://127.0.0.1:{spec.local_port}/v1", model, timeout=timeout)
        return TunnelReport(action, endpoint, listener)
