"""Command surface for installing, inspecting, or running the Qwen Code harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..protocol import AdapterContractError, canonical_json
from .adapter import QwenCodeAdapter, json_emitter
from .events import QwenCodeEventError
from .runtime import QwenCodeRuntime


def _objective(args: argparse.Namespace) -> str:
    if args.objective is not None:
        return args.objective
    if args.objective_file is not None:
        return args.objective_file.read_text(encoding="utf-8")
    raise AdapterContractError("run requires --objective or --objective-file")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coding Intelligence Qwen Code harness")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install-config", help="Deploy and pin isolated per-user settings")
    commands.add_parser("status", help="Report exact installed runtime/config identity")
    probe = commands.add_parser("probe-config", help="Load official config without a model call")
    probe.add_argument("--cwd", type=Path, default=Path.cwd())
    run = commands.add_parser("run", help="Run one real objective in an isolated stage")
    run.add_argument("--stage", type=Path, required=True)
    objective = run.add_mutually_exclusive_group(required=True)
    objective.add_argument("--objective")
    objective.add_argument("--objective-file", type=Path)
    run.add_argument("--max-session-turns", type=int, default=48)
    run.add_argument("--max-tool-calls", type=int, default=80)
    run.add_argument("--max-wall-time", type=int, default=900)
    return parser


def main() -> int:
    args = _parser().parse_args()
    runtime = QwenCodeRuntime.default()
    try:
        if args.command == "install-config":
            print(canonical_json(runtime.deploy_config()))
            return 0
        if args.command == "status":
            print(canonical_json(runtime.validate_deployed()))
            return 0
        if args.command == "probe-config":
            result = runtime.probe_config_load(cwd=args.cwd.resolve())
            print(canonical_json(result))
            return 0 if result["loaded"] else 1
        result = QwenCodeAdapter(runtime).run(
            stage=args.stage,
            objective=_objective(args),
            max_session_turns=args.max_session_turns,
            max_tool_calls=args.max_tool_calls,
            max_wall_time_seconds=args.max_wall_time,
            emit=json_emitter,
        )
        return 0 if result["status"] == "completed" else 1
    except (
        AdapterContractError,
        QwenCodeEventError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(canonical_json({"type": "harness_terminal", "status": "failed", "error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
