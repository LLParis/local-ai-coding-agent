from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_continuity.research_radar import (
    ResearchRadarError,
    load_sync_config,
    sync_research_radar,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-radar-runtime")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--as-of")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--validate-config", action="store_true")
    return parser


def _report(value: object) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_sync_config(args.config)
        if args.validate_config:
            if args.root is not None or args.apply or args.offline or args.as_of:
                raise ResearchRadarError(
                    "--validate-config cannot be combined with runtime execution options"
                )
            _report(
                {
                    "status": "valid",
                    "mode": "read_only_no_network_no_writes",
                    "config_sha256": config.sha256,
                }
            )
            return 0
        if args.root is None:
            raise ResearchRadarError("--root is required for radar sync")
        as_of = args.as_of or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        result = sync_research_radar(
            args.root,
            config,
            as_of=as_of,
            apply=args.apply,
            offline=args.offline,
        )
        _report(result)
        return 1 if result.get("status") == "runtime_blocked" else 0
    except (ResearchRadarError, OSError) as exc:
        print(f"research-radar-runtime: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
