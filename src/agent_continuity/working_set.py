"""Deterministic retrieval gate and bounded model working-set construction."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .compaction import TaskStateError, task_state_sha256, validate_task_state
from .memory_store import MemoryScope, MemoryStore, MemoryStoreError

WORKING_SET_SCHEMA = "coding-intelligence-working-set/v1"
RETRIEVAL_AUDIT_SCHEMA = "coding-intelligence-retrieval-audit/v1"
STATE_TOKEN_BUDGET = 2_048
MEMORY_TOKEN_BUDGET = 4_096
PER_MEMORY_TOKEN_CAP = 512
MAX_MEMORIES = 8
RETRIEVAL_CANDIDATE_LIMIT = 32
_MAX_TOKENIZE_RESPONSE_BYTES = 16 * 1024 * 1024


class WorkingSetError(RuntimeError):
    """The working set cannot be built truthfully within its fixed contract."""


class TokenCounter(Protocol):
    """Exact tokenizer contract injected into deterministic packing."""

    @property
    def identifier(self) -> str: ...

    def count(self, text: str) -> int: ...


class OfflineWhitespaceTokenCounter:
    """Explicit test-only tokenizer: maximal non-whitespace runs are tokens."""

    identifier = "offline:whitespace-v1"

    def count(self, text: str) -> int:
        return len(re.findall(r"\S+", text, flags=re.UNICODE))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Mapping[str, str],
        new_url: str,
    ) -> None:
        return None


class LlamaCppTokenCounter:
    """Count with one non-retried request to a loopback llama.cpp `/tokenize`."""

    def __init__(self, url: str, *, timeout: float = 10.0):
        self.url = self._validate_url(url)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise WorkingSetError("tokenize timeout must be a positive number")
        self.timeout = float(timeout)
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect(),
        )

    @property
    def identifier(self) -> str:
        return f"llama.cpp:{self.url}"

    def count(self, text: str) -> int:
        if not isinstance(text, str):
            raise WorkingSetError("tokenizer input must be text")
        body = json.dumps(
            {"content": text, "add_special": False, "parse_special": False},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.getcode()
                if status != 200:
                    raise WorkingSetError(f"tokenize endpoint returned HTTP {status}")
                raw = response.read(_MAX_TOKENIZE_RESPONSE_BYTES + 1)
        except WorkingSetError:
            raise
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            raise WorkingSetError(f"tokenize endpoint returned HTTP {code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise WorkingSetError(f"tokenize endpoint failed: {error}") from error
        if len(raw) > _MAX_TOKENIZE_RESPONSE_BYTES:
            raise WorkingSetError("tokenize endpoint response exceeds 16 MiB")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise WorkingSetError("tokenize endpoint returned invalid JSON") from error
        if not isinstance(value, dict) or not isinstance(value.get("tokens"), list):
            raise WorkingSetError("tokenize endpoint response must contain a tokens list")
        tokens = value["tokens"]
        if any(
            not isinstance(token, int) or isinstance(token, bool) or token < 0
            for token in tokens
        ):
            raise WorkingSetError("tokenize endpoint returned an invalid token id")
        return len(tokens)

    @staticmethod
    def _validate_url(value: str) -> str:
        if not isinstance(value, str) or not value:
            raise WorkingSetError("tokenize URL must be non-empty text")
        try:
            parsed = urllib.parse.urlsplit(value)
            port = parsed.port
        except ValueError as error:
            raise WorkingSetError(f"invalid tokenize URL: {error}") from error
        if parsed.scheme != "http":
            raise WorkingSetError("tokenize URL must use loopback HTTP")
        if parsed.username is not None or parsed.password is not None:
            raise WorkingSetError("tokenize URL cannot contain credentials")
        if parsed.query or parsed.fragment or parsed.path != "/tokenize":
            raise WorkingSetError("tokenize URL must have the exact /tokenize path")
        host = parsed.hostname
        if host is None:
            raise WorkingSetError("tokenize URL must contain a loopback host")
        is_loopback = host.casefold() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise WorkingSetError("tokenize URL must use a loopback host")
        netloc = f"[{host}]:{port}" if ":" in host and port is not None else parsed.netloc
        if ":" in host and port is None:
            netloc = f"[{host}]"
        return urllib.parse.urlunsplit(("http", netloc, "/tokenize", "", ""))


@dataclass(frozen=True)
class QueryInput:
    request: str
    file_symbols: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkingSet:
    model_text: str
    state_tokens: int
    memory_tokens: int
    total_tokens: int
    included_memory_ids: tuple[str, ...]
    retrieval_audit: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": WORKING_SET_SCHEMA,
            "model_text": self.model_text,
            "state_tokens": self.state_tokens,
            "memory_tokens": self.memory_tokens,
            "total_tokens": self.total_tokens,
            "included_memory_ids": list(self.included_memory_ids),
            "retrieval_audit": self.retrieval_audit,
        }


class _CountLedger:
    def __init__(self, counter: TokenCounter):
        identifier = getattr(counter, "identifier", None)
        if not isinstance(identifier, str) or not identifier:
            raise WorkingSetError("token counter identifier must be non-empty text")
        self.counter = counter
        self.identifier = identifier
        self.cache: dict[str, int] = {}

    def count(self, text: str, label: str) -> int:
        if text in self.cache:
            return self.cache[text]
        try:
            value = self.counter.count(text)
        except WorkingSetError:
            raise
        except Exception as error:
            raise WorkingSetError(f"token counter failed for {label}: {error}") from error
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise WorkingSetError(
                f"token counter returned an invalid count for {label}: {value!r}"
            )
        self.cache[text] = value
        return value


def build_literal_query(
    request: str,
    objective: str,
    *,
    file_symbols: Sequence[str] = (),
    errors: Sequence[str] = (),
    tools: Sequence[str] = (),
    blockers: Sequence[str] = (),
) -> tuple[str, list[dict[str, str]]]:
    """Return an ordered query that contains every input literal byte-for-byte."""

    groups = (
        ("request", (request,)),
        ("objective", (objective,)),
        ("file_symbol", file_symbols),
        ("error", errors),
        ("tool", tools),
        ("blocker", blockers),
    )
    components: list[dict[str, str]] = []
    for kind, values in groups:
        for value in values:
            if not isinstance(value, str) or not value:
                raise WorkingSetError(f"{kind} query inputs must be non-empty text")
            components.append({"kind": kind, "literal": value})
    return "\n".join(item["literal"] for item in components), components


def pack_working_set(
    store: MemoryStore,
    task_id: str,
    query_input: QueryInput,
    scopes: Sequence[MemoryScope | Mapping[str, Any]],
    token_counter: TokenCounter,
) -> WorkingSet:
    """Pack one deterministic state-first context under fixed exact-token budgets."""

    if not isinstance(task_id, str) or not task_id:
        raise WorkingSetError("task_id must be non-empty text")
    if not isinstance(query_input, QueryInput):
        raise WorkingSetError("query_input must be QueryInput")
    if not scopes:
        raise WorkingSetError("at least one explicit exact scope is required")
    scopes_by_key: dict[str, MemoryScope] = {}
    try:
        for value in scopes:
            scope = MemoryScope.from_value(value)
            scopes_by_key[scope.key] = scope
    except MemoryStoreError as error:
        raise WorkingSetError(str(error)) from error
    normalized_scopes = sorted(scopes_by_key.values(), key=lambda scope: scope.key)
    counts = _CountLedger(token_counter)

    # Hold the same store lock across state selection and candidate retrieval so
    # the working set cannot mix revisions from concurrent appends/rebuilds.
    try:
        with store._exclusive_store_lock():
            state, state_projection = _load_current_state(store, task_id)
            active_blockers = tuple(
                item["text"] for item in state["blockers"] if item["status"] == "active"
            )
            open_tools = tuple(item["tool"] for item in state["open_tool_calls"])
            literal_query, components = build_literal_query(
                query_input.request,
                state["objective"]["text"],
                file_symbols=query_input.file_symbols,
                errors=query_input.errors,
                tools=(*open_tools, *query_input.tools),
                blockers=(*active_blockers, *query_input.blockers),
            )
            candidates = store.search(
                literal_query,
                normalized_scopes,
                top_k=RETRIEVAL_CANDIDATE_LIMIT,
            )
    except (MemoryStoreError, TaskStateError) as error:
        raise WorkingSetError(str(error)) from error

    state_text = "## CURRENT TYPED TASK STATE\n" + _canonical_text(state) + "\n"
    state_tokens = counts.count(state_text, "task state")
    if state_tokens > STATE_TOKEN_BUDGET:
        raise WorkingSetError(
            f"current task state requires {state_tokens} tokens; budget is {STATE_TOKEN_BUDGET}"
        )

    memory_text = "## RETRIEVED LONG-TERM MEMORY\n"
    memory_tokens = counts.count(memory_text, "memory header")
    if memory_tokens > MEMORY_TOKEN_BUDGET:
        raise WorkingSetError(
            f"memory header requires {memory_tokens} tokens; budget is {MEMORY_TOKEN_BUDGET}"
        )

    included: list[str] = []
    candidate_audit: list[dict[str, Any]] = []
    for rank, candidate in enumerate(candidates, start=1):
        provenance = _provenance(candidate)
        audit = {
            "rank": rank,
            "memory_id": candidate["memory_id"],
            "retrieval": candidate["retrieval"],
            "provenance": provenance,
            "decision": "excluded",
            "reason": None,
            "representation": None,
            "candidate_tokens": None,
            "memory_tokens_if_included": None,
        }
        if not provenance["source_event_ids"] or not provenance["evidence"]:
            audit["reason"] = "missing_provenance"
            candidate_audit.append(audit)
            continue
        if len(included) >= MAX_MEMORIES:
            audit["reason"] = "max_memories"
            candidate_audit.append(audit)
            continue

        full_block = _memory_block(candidate, include_excerpts=True)
        block = full_block
        representation = "full"
        block_tokens = counts.count(block, f"memory {candidate['memory_id']} full")
        if block_tokens > PER_MEMORY_TOKEN_CAP:
            block = _memory_block(candidate, include_excerpts=False)
            representation = "compact"
            block_tokens = counts.count(block, f"memory {candidate['memory_id']} compact")
        audit["representation"] = representation
        audit["candidate_tokens"] = block_tokens
        if block_tokens > PER_MEMORY_TOKEN_CAP:
            audit["reason"] = "per_memory_token_cap"
            candidate_audit.append(audit)
            continue

        proposed = memory_text + block
        proposed_tokens = counts.count(proposed, f"memory total through {candidate['memory_id']}")
        audit["memory_tokens_if_included"] = proposed_tokens
        if proposed_tokens > MEMORY_TOKEN_BUDGET:
            audit["reason"] = "memory_token_budget"
            candidate_audit.append(audit)
            continue
        memory_text = proposed
        memory_tokens = proposed_tokens
        included.append(candidate["memory_id"])
        audit["decision"] = "included"
        audit["reason"] = "included"
        candidate_audit.append(audit)

    model_text = state_text + "\n" + memory_text
    total_tokens = counts.count(model_text, "complete working set")
    audit_value = {
        "schema": RETRIEVAL_AUDIT_SCHEMA,
        "token_counter": counts.identifier,
        "query": {
            "literal": literal_query,
            "components": components,
        },
        "scopes": [asdict(scope) for scope in normalized_scopes],
        "state": {
            "task_id": task_id,
            "revision": state_projection["revision"],
            "state_sha256": state_projection["state_sha256"],
            "through_event_id": state_projection["through_event_id"],
            "committed_at": state_projection["committed_at"],
            "tokens": state_tokens,
            "budget": STATE_TOKEN_BUDGET,
        },
        "memory": {
            "tokens": memory_tokens,
            "budget": MEMORY_TOKEN_BUDGET,
            "per_memory_cap": PER_MEMORY_TOKEN_CAP,
            "max_memories": MAX_MEMORIES,
            "candidate_limit": RETRIEVAL_CANDIDATE_LIMIT,
            "candidates": candidate_audit,
        },
    }
    return WorkingSet(
        model_text=model_text,
        state_tokens=state_tokens,
        memory_tokens=memory_tokens,
        total_tokens=total_tokens,
        included_memory_ids=tuple(included),
        retrieval_audit=audit_value,
    )


def _load_current_state(
    store: MemoryStore, task_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    with closing(store._connect()) as connection:
        row = connection.execute(
            "SELECT revision, state_json, state_sha256, through_event_id, committed_at "
            "FROM task_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        raise WorkingSetError(f"no committed task state exists for task_id {task_id}")
    try:
        state = validate_task_state(json.loads(row["state_json"]))
    except (json.JSONDecodeError, TaskStateError) as error:
        raise WorkingSetError(f"current task state is invalid: {error}") from error
    expected_hash = task_state_sha256(state)
    if row["state_sha256"] != expected_hash:
        raise WorkingSetError("current task-state projection hash mismatch")
    if state["task_id"] != task_id or state["through_event_id"] != row["through_event_id"]:
        raise WorkingSetError("current task-state projection identity mismatch")
    return state, dict(row)


def _provenance(candidate: Mapping[str, Any]) -> dict[str, Any]:
    evidence = [
        {
            key: item.get(key)
            for key in (
                "evidence_id",
                "source_kind",
                "source_locator",
                "source_event_id",
                "source_sha256",
                "observed_at",
                "authority",
            )
        }
        for item in candidate.get("evidence", [])
    ]
    source_ids = sorted(
        (
            {candidate.get("source_event_id")}
            | {item["source_event_id"] for item in evidence if item["source_event_id"]}
        )
        - {None}
    )
    return {"source_event_ids": source_ids, "evidence": evidence}


def _memory_block(candidate: Mapping[str, Any], *, include_excerpts: bool) -> str:
    if not include_excerpts:
        # A production episode's full display metadata can exceed the fixed
        # 512-token per-memory contract even when its actual claim is small.
        # Preserve the complete claim and cryptographic attribution, while
        # leaving redundant display/temporal fields in the immutable store.
        value = {
            "memory_id": candidate["memory_id"],
            "claim": {
                "subject": candidate["subject"],
                "predicate": candidate["predicate"],
                "object": candidate["object"],
            },
            "verification_status": candidate["verification_status"],
            "provenance": {
                "source_event_ids": sorted(
                    {
                        candidate["source_event_id"],
                        *(
                            item.get("source_event_id")
                            for item in candidate["evidence"]
                            if item.get("source_event_id")
                        ),
                    }
                ),
                "source_sha256": sorted(
                    {
                        item.get("source_sha256")
                        for item in candidate["evidence"]
                        if item.get("source_sha256")
                    }
                ),
                "authorities": sorted(
                    {
                        item.get("authority")
                        for item in candidate["evidence"]
                        if item.get("authority")
                    }
                ),
            },
        }
        return "### MEMORY\n" + _canonical_text(value) + "\n"

    evidence = [
        {
            **{
                key: item.get(key)
                for key in (
                    "evidence_id",
                    "source_kind",
                    "source_locator",
                    "source_event_id",
                    "source_sha256",
                    "observed_at",
                    "authority",
                )
            },
            "excerpt": item.get("excerpt"),
        }
        for item in candidate["evidence"]
    ]
    value = {
        "memory_id": candidate["memory_id"],
        "kind": candidate["kind"],
        "claim": {
            "subject": candidate["subject"],
            "predicate": candidate["predicate"],
            "object": candidate["object"],
        },
        "temporal": {
            "status": candidate["status"],
            "valid_from": candidate["valid_from"],
            "valid_to": candidate["valid_to"],
            "observed_at": candidate["observed_at"],
            "recorded_at": candidate["recorded_at"],
        },
        "verification_status": candidate["verification_status"],
        "provenance": {
            "source_event_id": candidate["source_event_id"],
            "evidence": evidence,
        },
    }
    return "### MEMORY\n" + _canonical_text(value) + "\n"


def _canonical_text(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise WorkingSetError(f"working-set value is not canonical JSON: {error}") from error
