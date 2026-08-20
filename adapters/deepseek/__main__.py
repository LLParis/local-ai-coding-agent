from __future__ import annotations

import argparse
import time
import uuid
from pathlib import Path

from ..protocol import ADAPTER_SCHEMA_VERSION, AdapterContractError, TaskCapsule
from ..runner import stdout_emitter
from .adapter import PROVIDER, DeepSeekAdapter


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Describe the blocked DeepSeek headless qualification adapter"
    )
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--max-tool-calls", type=int, default=12)
    args = parser.parse_args()
    emit = stdout_emitter()
    started = time.monotonic()
    try:
        DeepSeekAdapter().run(
            TaskCapsule.load(args.task),
            args.stage,
            endpoint=args.endpoint,
            model=args.model,
            max_turns=args.max_turns,
            max_tool_calls=args.max_tool_calls,
            emit=emit,
        )
        return 3
    except (AdapterContractError, OSError, ValueError) as error:
        emit(
            {
                "type": "terminal",
                "schema_version": ADAPTER_SCHEMA_VERSION,
                "run_id": str(uuid.uuid4()),
                "harness": "deepseek",
                "status": "failed",
                "model": args.model,
                "provider": PROVIDER,
                "turns": 0,
                "tool_calls": 0,
                "automatic_retries": 0,
                "stop": "contract_error",
                "diff": {"changed": [], "out_of_scope": [], "unified": "", "sha256": ""},
                "test": {"not_run": True, "passed": False},
                "telemetry": {"duration_ms": round((time.monotonic() - started) * 1000, 3)},
                "error": str(error),
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
