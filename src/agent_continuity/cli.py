from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointError, create_checkpoint, validate_checkpoint
from .compaction import (
    CompactionValidationResult,
    FrozenSourceRange,
    TaskStateError,
    load_task_state,
    validate_compaction_proposal,
    write_task_state,
)
from .endpoint import EndpointError, check_endpoint
from .local_edit import LocalEditError, run_local_edit
from .memory_store import MemoryStore, MemoryStoreError
from .research_radar import (
    DEFAULT_MAX_IDS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_PAGE_SIZE,
    DEFAULT_TIMEOUT_SECONDS,
    RadarConflictError,
    ResearchRadarError,
    apply_plan,
    apply_transition,
    fetch_arxiv_batches,
    load_sync_config,
    plan_ingestion,
    plan_transition,
    read_atom_file,
    sync_research_radar,
)
from .run_memory import (
    RUN_FINALIZATION_SCHEMA,
    RunIdentity,
    RunMemoryError,
    RunMemoryRecorder,
    default_memory_root,
    file_sha256,
)
from .working_set import (
    LlamaCppTokenCounter,
    OfflineWhitespaceTokenCounter,
    QueryInput,
    WorkingSetError,
    pack_working_set,
)


class CommandError(RuntimeError):
    pass


SEMANTIC_VERIFICATION_SCHEMA = "coding-intelligence-compaction-semantic-verification/v1"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise CommandError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(
            path.expanduser().read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except CommandError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CommandError(f"cannot load {label}: {exc}") from exc


def _object(
    value: Any, label: str, *, keys: set[str], required: set[str] | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CommandError(f"{label} must be a JSON object")
    missing = sorted((keys if required is None else required) - set(value))
    unknown = sorted(set(value) - keys)
    if missing:
        raise CommandError(f"{label} is missing keys: {', '.join(missing)}")
    if unknown:
        raise CommandError(f"{label} has unknown keys: {', '.join(unknown)}")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise CommandError(f"{label} must be a canonical sha256 digest")
    return value


class _ManifestResolver:
    """Resolve compaction citations from a frozen, hash-pinned JSON manifest."""

    def __init__(self, value: Any):
        manifest = _object(value, "citation manifest", keys={"events", "evidence", "artifacts"})
        events = manifest["events"]
        evidence = manifest["evidence"]
        artifacts = manifest["artifacts"]
        if not isinstance(events, dict):
            raise CommandError("citation manifest events must be an object")
        if not isinstance(evidence, list) or any(
            not isinstance(item, str) or not item for item in evidence
        ):
            raise CommandError("citation manifest evidence must be a list of identifiers")
        if len(evidence) != len(set(evidence)):
            raise CommandError("citation manifest evidence contains duplicates")
        if not isinstance(artifacts, dict):
            raise CommandError("citation manifest artifacts must be an object")
        if any(not isinstance(key, str) or not key for key in events):
            raise CommandError("citation manifest event identifiers must be non-empty strings")
        if any(not isinstance(key, str) or not key for key in artifacts):
            raise CommandError("citation manifest artifact paths must be non-empty strings")
        self.events = {
            key: _sha256(digest, f"citation manifest events.{key}")
            for key, digest in events.items()
        }
        self.evidence = frozenset(evidence)
        self.artifacts = {
            key: _sha256(digest, f"citation manifest artifacts.{key}")
            for key, digest in artifacts.items()
        }

    def event_record_sha256(self, event_id: str) -> str | None:
        return self.events.get(event_id)

    def evidence_exists(self, evidence_id: str) -> bool:
        return evidence_id in self.evidence

    def artifact_sha256(self, path: str) -> str | None:
        return self.artifacts.get(path)


def _source_range(path: Path) -> FrozenSourceRange:
    value = _object(
        _load_json(path, "source range"),
        "source range",
        keys={
            "first_event_id",
            "first_record_sha256",
            "last_event_id",
            "last_record_sha256",
            "retained_tail_event_id",
        },
    )
    return FrozenSourceRange(**value)


def _validate_compaction(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], CompactionValidationResult]:
    previous = load_task_state(args.previous.expanduser())
    candidate = load_task_state(args.candidate.expanduser())
    resolver = _ManifestResolver(_load_json(args.citations, "citation manifest"))
    result = validate_compaction_proposal(
        previous, candidate, _source_range(args.source_range), resolver
    )
    return candidate, result


def _semantic_verifier_id(path: Path, candidate_sha256: str) -> str:
    value = _object(
        _load_json(path, "semantic verifier report"),
        "semantic verifier report",
        keys={"schema", "candidate_state_sha256", "verdict", "verifier_id", "checks"},
    )
    if value["schema"] != SEMANTIC_VERIFICATION_SCHEMA:
        raise CommandError(
            f"semantic verifier report schema must be {SEMANTIC_VERIFICATION_SCHEMA}"
        )
    if (
        _sha256(value["candidate_state_sha256"], "semantic verifier candidate_state_sha256")
        != candidate_sha256
    ):
        raise CommandError("semantic verifier report is for a different candidate")
    if value["verdict"] != "accepted":
        raise CommandError("semantic verifier did not accept the candidate")
    verifier_id = value["verifier_id"]
    if not isinstance(verifier_id, str) or not verifier_id.strip():
        raise CommandError("semantic verifier_id must be non-empty text")
    checks = value["checks"]
    required_checks = {"semantic_omissions", "semantic_contradictions"}
    if not isinstance(checks, list) or set(checks) != required_checks or len(checks) != 2:
        raise CommandError(
            "semantic verifier checks must contain semantic_omissions and "
            "semantic_contradictions exactly once"
        )
    return verifier_id


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
    local_edit.add_argument("--memory-root", type=Path, help=argparse.SUPPRESS)
    local_edit.add_argument("--task-id", help=argparse.SUPPRESS)
    local_edit.add_argument("--session-id", help=argparse.SUPPRESS)
    local_edit.add_argument("test_command", nargs=argparse.REMAINDER)

    run_memory_start = commands.add_parser("run-memory-start", help=argparse.SUPPRESS)
    run_memory_start.add_argument("--root", required=True, type=Path)
    run_memory_start.add_argument("--task-id", required=True)
    run_memory_start.add_argument("--session-id", required=True)
    run_memory_start.add_argument("--workspace", required=True, type=Path)
    run_memory_start.add_argument("--objective", required=True)
    run_memory_start.add_argument("--model", required=True)
    run_memory_start.add_argument("--manifest", type=Path)

    run_memory_finalize = commands.add_parser("run-memory-finalize", help=argparse.SUPPRESS)
    run_memory_finalize.add_argument("--root", required=True, type=Path)
    run_memory_finalize.add_argument("--task-id", required=True)
    run_memory_finalize.add_argument("--session-id", required=True)
    run_memory_finalize.add_argument("--workspace", required=True, type=Path)
    run_memory_finalize.add_argument("--objective", required=True)
    run_memory_finalize.add_argument("--model", required=True)
    run_memory_finalize.add_argument("--input", required=True, type=Path)

    memory_init = commands.add_parser(
        "memory-init", help="initialize or reopen a sovereign memory store"
    )
    memory_init.add_argument("--root", required=True, type=Path)

    memory_append = commands.add_parser(
        "memory-append", help="append one canonical event from a JSON object"
    )
    memory_append.add_argument("--root", required=True, type=Path)
    memory_append.add_argument("--event", required=True, type=Path)

    memory_verify = commands.add_parser(
        "memory-verify", help="verify canonical event chains and referenced blobs"
    )
    memory_verify.add_argument("--root", required=True, type=Path)

    memory_rebuild = commands.add_parser(
        "memory-rebuild", help="rebuild the SQLite projection from canonical JSONL"
    )
    memory_rebuild.add_argument("--root", required=True, type=Path)

    memory_search = commands.add_parser(
        "memory-search", help="search one or more exact memory scopes"
    )
    memory_search.add_argument("--root", required=True, type=Path)
    memory_search.add_argument("--query", required=True)
    memory_search.add_argument("--scope", required=True, type=Path, action="append")
    memory_search.add_argument("--as-of")
    memory_search.add_argument("--kind", action="append")
    memory_search.add_argument("--top-k", type=int, default=8)

    memory_get = commands.add_parser("memory-get", help="get one memory with provenance")
    memory_get.add_argument("--root", required=True, type=Path)
    memory_get.add_argument("--memory-id", required=True)

    memory_pack = commands.add_parser(
        "memory-pack", help="build one deterministic state-first model working set"
    )
    memory_pack.add_argument("--root", required=True, type=Path)
    memory_pack.add_argument("--task-id", required=True)
    memory_pack.add_argument("--request", required=True)
    memory_pack.add_argument("--scope", required=True, type=Path, action="append")
    memory_pack.add_argument("--file-symbol", action="append")
    memory_pack.add_argument("--error", action="append")
    memory_pack.add_argument("--tool", action="append")
    memory_pack.add_argument("--blocker", action="append")
    counter = memory_pack.add_mutually_exclusive_group(required=True)
    counter.add_argument("--tokenize-url", help="exact loopback llama.cpp URL ending in /tokenize")
    counter.add_argument(
        "--offline-counter",
        choices=("whitespace-v1",),
        help="explicit deterministic test counter; not a production model tokenizer",
    )
    memory_pack.add_argument("--tokenize-timeout", type=float, default=10.0)

    def add_compaction_inputs(command: argparse.ArgumentParser) -> None:
        command.add_argument("--previous", required=True, type=Path)
        command.add_argument("--candidate", required=True, type=Path)
        command.add_argument("--source-range", required=True, type=Path)
        command.add_argument("--citations", required=True, type=Path)

    compaction_validate = commands.add_parser(
        "compaction-validate",
        help="run deterministic loss checks and produce a semantic-verifier handoff",
    )
    add_compaction_inputs(compaction_validate)

    compaction_commit = commands.add_parser(
        "compaction-commit",
        help="write an immutable state revision after deterministic and semantic approval",
    )
    add_compaction_inputs(compaction_commit)
    compaction_commit.add_argument("--semantic-verifier", required=True, type=Path)
    compaction_commit.add_argument("--output", required=True, type=Path)

    radar_ingest = commands.add_parser(
        "radar-ingest",
        help="dry-run bounded primary-arXiv metadata intake; --apply writes immutable records",
    )
    radar_ingest.add_argument("--root", required=True, type=Path)
    radar_source = radar_ingest.add_mutually_exclusive_group(required=True)
    radar_source.add_argument("--arxiv-id", action="append")
    radar_source.add_argument("--atom-file", type=Path, action="append")
    radar_ingest.add_argument("--max-ids", type=int, default=DEFAULT_MAX_IDS)
    radar_ingest.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    radar_ingest.add_argument("--max-response-bytes", type=int, default=DEFAULT_MAX_RESPONSE_BYTES)
    radar_ingest.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    radar_ingest.add_argument("--apply", action="store_true")

    radar_transition = commands.add_parser(
        "radar-transition",
        help="dry-run one immutable Research Radar lifecycle transition",
    )
    radar_transition.add_argument("--root", required=True, type=Path)
    radar_transition.add_argument("--arxiv-id", required=True)
    radar_transition.add_argument("--from-status", required=True)
    radar_transition.add_argument("--to-status", required=True)
    radar_transition.add_argument("--owner", required=True)
    radar_transition.add_argument("--reason", required=True)
    radar_transition.add_argument("--evidence", action="append", default=[])
    radar_transition.add_argument("--revisit-trigger")
    radar_transition.add_argument("--occurred-at", required=True)
    radar_transition.add_argument("--apply", action="store_true")

    radar_sync = commands.add_parser(
        "radar-sync",
        help="run one bounded daily discovery / weekly version-recheck cycle",
    )
    radar_sync.add_argument("--root", required=True, type=Path)
    radar_sync.add_argument("--config", required=True, type=Path)
    radar_sync.add_argument(
        "--as-of",
        help="ISO-8601 evaluation time; defaults to the current UTC time",
    )
    radar_sync.add_argument(
        "--offline",
        action="store_true",
        help="read only the persistent daily cache; a cache miss is runtime_blocked",
    )
    radar_sync.add_argument("--apply", action="store_true")

    radar_config_validate = commands.add_parser(
        "radar-config-validate",
        help="validate one strict Research Radar sync configuration without network or writes",
    )
    radar_config_validate.add_argument("--config", required=True, type=Path)
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
            if (args.task_id is None) != (args.session_id is None):
                raise CommandError("task-id and session-id must be supplied together")
            identity = (
                RunIdentity(args.task_id, args.session_id)
                if args.task_id is not None
                else RunIdentity(str(uuid.uuid4()), str(uuid.uuid4()))
            )
            memory_root = args.memory_root or default_memory_root()
            try:
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
                    memory_root=memory_root,
                    task_id=identity.task_id,
                    session_id=identity.session_id,
                )
            except LocalEditError as error:
                try:
                    recorder = RunMemoryRecorder(
                        root=memory_root,
                        identity=identity,
                        workspace=args.workspace,
                        objective=args.objective,
                        model=args.model,
                    )
                    report = recorder.failure_report(str(error), test_command=command)
                except (RunMemoryError, MemoryStoreError, OSError):
                    raise error
            _report(**report)
            return 0 if report["status"] == "verified" else 1
        elif args.command == "run-memory-start":
            recorder = RunMemoryRecorder(
                root=args.root,
                identity=RunIdentity(args.task_id, args.session_id),
                workspace=args.workspace,
                objective=args.objective,
                model=args.model,
            )
            started = recorder.ensure_started(
                manifest_sha256=(file_sha256(args.manifest) if args.manifest else None)
            )
            _report(status="started", **started)
        elif args.command == "run-memory-finalize":
            value = _object(
                _load_json(args.input, "run finalization"),
                "run finalization",
                keys={
                    "schema",
                    "status",
                    "process_exit",
                    "command_exit",
                    "model_calls",
                    "automatic_retries",
                    "failure_sha256",
                    "implementation",
                    "verifier",
                    "restore",
                },
            )
            if value["schema"] != RUN_FINALIZATION_SCHEMA:
                raise CommandError(f"run finalization schema must be {RUN_FINALIZATION_SCHEMA}")
            recorder = RunMemoryRecorder(
                root=args.root,
                identity=RunIdentity(args.task_id, args.session_id),
                workspace=args.workspace,
                objective=args.objective,
                model=args.model,
            )
            _report(**recorder.finalize(value))
        elif args.command == "memory-init":
            store = MemoryStore(args.root.expanduser())
            report = store.verify()
            _report(
                status="ready",
                root=str(store.root),
                database=str(store.database_path),
                **asdict(report),
            )
        elif args.command == "memory-append":
            event = _object(
                _load_json(args.event, "memory event"),
                "memory event",
                keys={
                    "host_id",
                    "session_id",
                    "event_type",
                    "actor",
                    "scope",
                    "payload",
                    "task_id",
                    "parent_event_id",
                    "event_id",
                    "occurred_at",
                    "retention_class",
                },
                required={"host_id", "session_id", "event_type", "actor", "scope", "payload"},
            )
            record = MemoryStore(args.root.expanduser()).append(**event)
            _report(
                status="appended",
                event_id=record["event_id"],
                host_id=record["host_id"],
                session_id=record["session_id"],
                seq=record["seq"],
                payload_sha256=record["payload_sha256"],
                record_sha256=record["record_sha256"],
            )
        elif args.command == "memory-verify":
            store = MemoryStore(args.root.expanduser())
            _report(status="valid", root=str(store.root), **asdict(store.verify()))
        elif args.command == "memory-rebuild":
            store = MemoryStore(args.root.expanduser(), rebuild_on_open=True)
            _report(status="rebuilt", root=str(store.root), **asdict(store.verify()))
        elif args.command == "memory-search":
            scopes = [_load_json(path, "memory scope") for path in args.scope]
            results = MemoryStore(args.root.expanduser()).search(
                args.query,
                scopes,
                as_of=args.as_of,
                kinds=args.kind,
                top_k=args.top_k,
            )
            _report(status="ok", count=len(results), results=results)
        elif args.command == "memory-get":
            memory = MemoryStore(args.root.expanduser()).get(args.memory_id)
            if memory is None:
                _report(status="not_found", memory_id=args.memory_id)
                return 1
            _report(status="ok", memory=memory)
        elif args.command == "memory-pack":
            scopes = [_load_json(path, "memory scope") for path in args.scope]
            token_counter = (
                LlamaCppTokenCounter(args.tokenize_url, timeout=args.tokenize_timeout)
                if args.tokenize_url is not None
                else OfflineWhitespaceTokenCounter()
            )
            working_set = pack_working_set(
                MemoryStore(args.root.expanduser()),
                args.task_id,
                QueryInput(
                    request=args.request,
                    file_symbols=tuple(args.file_symbol or ()),
                    errors=tuple(args.error or ()),
                    tools=tuple(args.tool or ()),
                    blockers=tuple(args.blocker or ()),
                ),
                scopes,
                token_counter,
            )
            _report(status="ok", **working_set.as_dict())
        elif args.command == "compaction-validate":
            _, result = _validate_compaction(args)
            _report(**result.as_dict())
            return 0 if result.accepted else 1
        elif args.command == "compaction-commit":
            candidate, result = _validate_compaction(args)
            if not result.accepted or result.candidate_state_sha256 is None:
                _report(**result.as_dict())
                return 1
            verifier_id = _semantic_verifier_id(
                args.semantic_verifier, result.candidate_state_sha256
            )
            write_task_state(args.output, candidate)
            _report(
                status="committed",
                storage="immutable-state-file",
                output=str(args.output.expanduser().resolve()),
                candidate_state_sha256=result.candidate_state_sha256,
                semantic_verifier_id=verifier_id,
            )
        elif args.command == "radar-ingest":
            if (
                not 1 <= args.max_ids <= DEFAULT_MAX_IDS
                or not 1 <= args.page_size <= DEFAULT_PAGE_SIZE
                or not 1 <= args.max_response_bytes <= DEFAULT_MAX_RESPONSE_BYTES
                or not 0 < args.timeout <= DEFAULT_TIMEOUT_SECONDS
            ):
                raise CommandError(
                    "Research Radar bounds must be positive and no larger than v1 defaults"
                )
            if args.atom_file:
                if len(args.atom_file) > args.max_ids:
                    raise CommandError("Atom file count exceeds the aggregate v1 limit")
                batches = []
                remaining_bytes = args.max_response_bytes
                for path in args.atom_file:
                    batch = read_atom_file(path, max_response_bytes=remaining_bytes)
                    batches.append(batch)
                    remaining_bytes -= len(batch.content)
            else:
                batches = fetch_arxiv_batches(
                    args.arxiv_id,
                    max_ids=args.max_ids,
                    page_size=args.page_size,
                    max_response_bytes=args.max_response_bytes,
                    timeout_seconds=args.timeout,
                )
            plan = plan_ingestion(args.root, batches, max_entries_total=args.max_ids)
            counts = {
                state: sum(item.state == state for item in plan)
                for state in ("create", "unchanged")
            }
            if args.apply:
                result = apply_plan(args.root, plan)
                _report(
                    status="applied",
                    mode="explicit_apply",
                    root=str(args.root.expanduser().resolve()),
                    source_batches=len(batches),
                    **result,
                )
            else:
                _report(
                    status="planned",
                    mode="dry_run_no_writes",
                    root=str(args.root.expanduser().resolve()),
                    source_batches=len(batches),
                    counts=counts,
                    actions=[item.report() for item in plan],
                )
        elif args.command == "radar-transition":
            transition = plan_transition(
                args.root,
                args.arxiv_id,
                expected_from=args.from_status,
                to_status=args.to_status,
                owner=args.owner,
                reason=args.reason,
                evidence=args.evidence,
                revisit_trigger=args.revisit_trigger,
                occurred_at=args.occurred_at,
            )
            if args.apply:
                state = apply_transition(args.root, transition, expected_from=args.from_status)
                _report(
                    status=state,
                    mode="explicit_apply",
                    path=transition.relative_path,
                    sha256=transition.sha256,
                )
            else:
                _report(
                    status="planned",
                    mode="dry_run_no_writes",
                    action=transition.report(),
                )
        elif args.command == "radar-sync":
            config = load_sync_config(args.config)
            as_of = args.as_of
            if as_of is None:
                as_of = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            result = sync_research_radar(
                args.root,
                config,
                as_of=as_of,
                apply=args.apply,
                offline=args.offline,
            )
            _report(**result)
            if result.get("status") == "runtime_blocked":
                return 1
        elif args.command == "radar-config-validate":
            config = load_sync_config(args.config)
            _report(
                status="valid",
                mode="read_only_no_network_no_writes",
                config=str(args.config.expanduser().resolve()),
                config_sha256=config.sha256,
            )
        return 0
    except (
        CheckpointError,
        EndpointError,
        LocalEditError,
        MemoryStoreError,
        TaskStateError,
        WorkingSetError,
        RunMemoryError,
        ResearchRadarError,
        RadarConflictError,
        CommandError,
        OSError,
    ) as exc:
        print(f"continuity: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
