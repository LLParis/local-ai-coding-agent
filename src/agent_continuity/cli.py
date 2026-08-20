from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .checkpoint import CheckpointError, create_checkpoint, validate_checkpoint
from .endpoint import EndpointError, check_endpoint
from .local_edit import LocalEditError, run_local_edit


class CommandError(RuntimeError):
    pass


def _require_macos(command: str) -> None:
    if sys.platform != "darwin":
        raise CommandError(
            f"{command} is a macOS edge command; local-edit is available directly on Windows"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="continuity", description="General local coding-agent continuity"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser(
        "checkpoint-create", help="create a deterministic secret-free checkpoint"
    )
    create.add_argument("--workspace", required=True, type=Path)
    create.add_argument("--task", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)

    validate = commands.add_parser(
        "checkpoint-validate", help="verify checkpoint integrity and freshness"
    )
    validate.add_argument("--checkpoint", required=True, type=Path)
    validate.add_argument("--workspace", type=Path)
    validate.add_argument("--require-current", action="store_true")

    endpoint = commands.add_parser(
        "endpoint-check", help="probe OpenAI-compatible models and Responses APIs"
    )
    endpoint.add_argument("--base-url", default="http://127.0.0.1:12434/v1")
    endpoint.add_argument("--model", default="gpt-oss:20b")
    endpoint.add_argument("--timeout", type=float, default=90.0)
    endpoint.add_argument("--models-only", action="store_true")

    tunnel = commands.add_parser(
        "tunnel-ensure", help="idempotently establish the macOS-to-EXCALIBUR loopback tunnel"
    )
    tunnel.add_argument("--host", default="excalibur")
    tunnel.add_argument("--local-port", type=int, default=12434)
    tunnel.add_argument("--remote-port", type=int, default=11434)
    tunnel.add_argument("--model", default="gpt-oss:20b")
    tunnel.add_argument("--timeout", type=float, default=90.0)

    launch = commands.add_parser(
        "launch", help="launch Codex from the macOS edge against EXCALIBUR after every gate passes"
    )
    launch.add_argument("--workspace", required=True, type=Path)
    launch.add_argument("--checkpoint", required=True, type=Path)
    launch.add_argument("--task", required=True)
    launch.add_argument("--profile", default="excalibur-local")
    launch.add_argument("--codex-bin", default="codex")
    launch.add_argument("--timeout", type=float, default=90.0)
    launch.add_argument("--dry-run", action="store_true")

    emergency = commands.add_parser("emergency-triage", help="explicit read-only Mac Ollama triage")
    emergency.add_argument("--workspace", required=True, type=Path)
    emergency.add_argument("--checkpoint", required=True, type=Path)
    emergency.add_argument("--task", required=True)
    emergency.add_argument("--confirm", required=True)
    emergency.add_argument("--profile", default="mac-emergency-triage")
    emergency.add_argument("--codex-bin", default="codex")
    emergency.add_argument("--timeout", type=float, default=120.0)
    emergency.add_argument("--dry-run", action="store_true")

    local_edit = commands.add_parser(
        "local-edit", help="run one thin local-model edit in an isolated working copy"
    )
    local_edit.add_argument("--workspace", required=True, type=Path)
    local_edit.add_argument("--objective", required=True)
    local_edit.add_argument("--mutable", required=True, type=Path, action="append")
    local_edit.add_argument("--context", required=True, type=Path, action="append")
    local_edit.add_argument(
        "--verify-context",
        type=Path,
        action="append",
        default=[],
        help=(
            "copy a path into the isolated stage for tests without including it in the model prompt"
        ),
    )
    local_edit.add_argument("--base-url", default="http://127.0.0.1:12434/v1")
    local_edit.add_argument("--model", default="gpt-oss-20b:latest")
    local_edit.add_argument("--timeout", type=float, default=180.0)
    local_edit.add_argument("test_command", nargs=argparse.REMAINDER)
    return parser


def _report(**values: object) -> None:
    print(json.dumps(values, sort_keys=True, separators=(",", ":")), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "checkpoint-create":
            value = create_checkpoint(args.workspace, args.task, args.output)
            _report(
                checkpoint=str(args.output.expanduser().resolve()),
                digest=value["digest"],
                status="created",
            )
        elif args.command == "checkpoint-validate":
            value = validate_checkpoint(args.checkpoint, args.workspace, args.require_current)
            _report(digest=value["digest"], status="valid")
        elif args.command == "endpoint-check":
            report = check_endpoint(
                args.base_url, args.model, timeout=args.timeout, models_only=args.models_only
            )
            _report(status="ready", **report.__dict__)
        elif args.command == "tunnel-ensure":
            _require_macos(args.command)
            from .tunnel import TunnelError, TunnelSpec, ensure_tunnel

            try:
                report = ensure_tunnel(
                    TunnelSpec(
                        host=args.host, local_port=args.local_port, remote_port=args.remote_port
                    ),
                    model=args.model,
                    timeout=args.timeout,
                )
            except TunnelError as exc:
                raise CommandError(str(exc)) from exc
            _report(
                status="ready",
                action=report.action,
                listener=report.listener,
                **report.endpoint.__dict__,
            )
        elif args.command == "launch":
            _require_macos(args.command)
            from .launcher import LaunchError, execute, primary_plan

            try:
                plan = primary_plan(
                    workspace=args.workspace,
                    checkpoint_path=args.checkpoint,
                    task=args.task,
                    profile=args.profile,
                    codex_bin=args.codex_bin,
                    timeout=args.timeout,
                )
            except LaunchError as exc:
                raise CommandError(str(exc)) from exc
            _report(
                status="ready",
                mode="excalibur",
                profile=args.profile,
                sandbox="workspace-write",
                approval="never",
                tunnel_action=plan.tunnel_action,
                model=plan.endpoint.model,
            )
            execute(plan, dry_run=args.dry_run)
        elif args.command == "emergency-triage":
            _require_macos(args.command)
            from .launcher import LaunchError, emergency_plan, execute

            try:
                plan = emergency_plan(
                    workspace=args.workspace,
                    checkpoint_path=args.checkpoint,
                    task=args.task,
                    confirmation=args.confirm,
                    profile=args.profile,
                    codex_bin=args.codex_bin,
                    timeout=args.timeout,
                )
            except LaunchError as exc:
                raise CommandError(str(exc)) from exc
            _report(
                status="ready",
                mode="mac-emergency-triage",
                profile=args.profile,
                sandbox="read-only",
                approval="never",
                model=plan.endpoint.model,
            )
            execute(plan, dry_run=args.dry_run)
        elif args.command == "local-edit":
            command = list(args.test_command)
            if command[:1] == ["--"]:
                command = command[1:]
            report = run_local_edit(
                workspace=args.workspace,
                objective=args.objective,
                mutable=args.mutable,
                context=args.context,
                verify_context=args.verify_context,
                test_command=command,
                base_url=args.base_url,
                model=args.model,
                timeout=args.timeout,
            )
            _report(**report)
            return 0 if report["status"] == "verified" else 1
        return 0
    except (CheckpointError, EndpointError, LocalEditError, CommandError, OSError) as exc:
        print(f"continuity: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
