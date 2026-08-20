from __future__ import annotations

import contextlib
import http.server
import json
import socketserver
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from agent_continuity.compaction import TASK_STATE_SCHEMA  # noqa: E402
from agent_continuity.memory_store import MemoryScope, MemoryStore  # noqa: E402
from agent_continuity.working_set import (  # noqa: E402
    MAX_MEMORIES,
    MEMORY_TOKEN_BUDGET,
    PER_MEMORY_TOKEN_CAP,
    STATE_TOKEN_BUDGET,
    LlamaCppTokenCounter,
    OfflineWhitespaceTokenCounter,
    QueryInput,
    WorkingSetError,
    pack_working_set,
)

NOW = "2026-08-20T08:00:00Z"


class _SectionCounter:
    identifier = "fake:section-counter"

    def __init__(self, *, state: int = 10, block: int = 10, header: int = 0):
        self.state = state
        self.block = block
        self.header = header

    def count(self, text: str) -> int:
        if text.startswith("## CURRENT") and "\n\n## RETRIEVED" not in text:
            return self.state
        if text.startswith("### MEMORY"):
            return self.block
        if text.startswith("## RETRIEVED"):
            return self.header + text.count("### MEMORY") * self.block
        if text.startswith("## CURRENT"):
            return self.state + self.header + text.count("### MEMORY") * self.block
        return 0


class _InvalidCounter:
    identifier = "fake:invalid"

    def __init__(self, value: object):
        self.value = value

    def count(self, _text: str) -> object:
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _TokenizeHandler(http.server.BaseHTTPRequestHandler):
    mode = "valid"
    calls = 0

    def do_POST(self) -> None:  # noqa: N802
        type(self).calls += 1
        length = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(length))
        if self.path != "/tokenize":
            self.send_error(404)
            return
        if self.mode == "http-error":
            self.send_error(500)
            return
        if self.mode == "bad-json":
            self._body(b"not-json")
            return
        if self.mode == "bad-token":
            self._body(b'{"tokens":[true]}')
            return
        tokens = list(range(len(value["content"].split())))
        self._body(json.dumps({"tokens": tokens}).encode())

    def _body(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


@contextlib.contextmanager
def tokenize_server(mode: str):
    handler = type("TokenizeHandler", (_TokenizeHandler,), {"mode": mode, "calls": 0})
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/tokenize", handler
        finally:
            server.shutdown()
            thread.join()


class WorkingSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = MemoryStore(self.root)
        self.session_id = str(uuid.uuid4())
        self.task_id = str(uuid.uuid4())
        self.state_event_id = str(uuid.uuid4())
        self.allowed = MemoryScope(
            "local-owner", "workspace-a", self.task_id, None, self.session_id
        )
        self.forbidden = MemoryScope(
            "local-owner", "workspace-b", self.task_id, None, self.session_id
        )
        self.append_state()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def append_state(self) -> None:
        state = {
            "schema": TASK_STATE_SCHEMA,
            "task_id": self.task_id,
            "objective": {
                "text": "Repair OwnerPid without losing literal identifiers.",
                "source_event_id": self.state_event_id,
            },
            "success_criteria": [],
            "constraints": [],
            "decisions": [],
            "completed": [],
            "current_action": {
                "text": "Build the deterministic working set.",
                "owner": "retrieval-gate",
                "started_at": NOW,
            },
            "next_actions": [],
            "blockers": [
                {
                    "id": "blocker-1",
                    "text": "Preserve ERROR_X42 verbatim.",
                    "status": "active",
                    "evidence_ids": [],
                }
            ],
            "open_tool_calls": [],
            "artifacts": [],
            "disputes": [],
            "through_event_id": self.state_event_id,
            "previous_state_sha256": None,
        }
        self.store.append(
            host_id="excalibur",
            session_id=self.session_id,
            task_id=self.task_id,
            event_id=self.state_event_id,
            occurred_at=NOW,
            event_type="state/committed",
            actor={"kind": "user", "id": "operator"},
            scope=self.allowed,
            payload={"revision": 1, "state": state},
        )

    def append_memory(
        self,
        memory_id: str,
        text: str,
        *,
        scope: MemoryScope | None = None,
        excerpt: str | None = None,
    ) -> None:
        event_id = str(uuid.uuid4())
        self.store.append(
            host_id="excalibur",
            session_id=self.session_id,
            task_id=self.task_id,
            event_id=event_id,
            occurred_at=NOW,
            event_type="memory/committed",
            actor={"kind": "user", "id": "operator"},
            scope=scope or self.allowed,
            payload={
                "memory_id": memory_id,
                "kind": "fact",
                "subject": memory_id,
                "predicate": "requires",
                "object": {"text": text},
                "searchable_text": text,
                "verification_status": "tested",
                "evidence": [
                    {
                        "source_kind": "test",
                        "source_locator": f"fixture:{memory_id}",
                        "authority": "authoritative-test",
                        "excerpt": excerpt if excerpt is not None else text,
                    }
                ],
            },
        )

    @staticmethod
    def query() -> QueryInput:
        return QueryInput(
            request=r"Fix D:\src\owner.py now.",
            file_symbols=(r"D:\src\owner.py::OwnerPid",),
            errors=("ERROR_X42: listenerPID != ownerPID",),
            tools=("pytest -q tests/test_owner.py",),
            blockers=("Do not edit hidden_tests.py",),
        )

    def test_state_first_literal_query_exact_scope_and_provenance_are_deterministic(self) -> None:
        self.append_memory(
            "allowed-memory",
            "ERROR_X42 requires ownerPID and pytest in workspace-a.",
        )
        self.append_memory(
            "forbidden-memory",
            "ERROR_X42 requires ownerPID and pytest in workspace-b.",
            scope=self.forbidden,
        )
        counter = OfflineWhitespaceTokenCounter()

        first = pack_working_set(
            self.store, self.task_id, self.query(), [self.allowed], counter
        ).as_dict()
        second = pack_working_set(
            self.store, self.task_id, self.query(), [self.allowed], counter
        ).as_dict()

        self.assertEqual(first, second)
        self.assertTrue(first["model_text"].startswith("## CURRENT TYPED TASK STATE\n"))
        self.assertLess(
            first["model_text"].index("objective"),
            first["model_text"].index("### MEMORY"),
        )
        self.assertEqual(first["included_memory_ids"], ["allowed-memory"])
        self.assertNotIn("forbidden-memory", json.dumps(first))
        audit = first["retrieval_audit"]
        expected_literals = [
            self.query().request,
            "Repair OwnerPid without losing literal identifiers.",
            *self.query().file_symbols,
            *self.query().errors,
            *self.query().tools,
            "Preserve ERROR_X42 verbatim.",
            *self.query().blockers,
        ]
        self.assertEqual(
            [item["literal"] for item in audit["query"]["components"]], expected_literals
        )
        for literal in expected_literals:
            self.assertIn(literal, audit["query"]["literal"])
        candidate = audit["memory"]["candidates"][0]
        self.assertEqual((candidate["decision"], candidate["reason"]), ("included", "included"))
        self.assertEqual(
            candidate["provenance"]["evidence"][0]["source_locator"],
            "fixture:allowed-memory",
        )
        self.assertTrue(candidate["provenance"]["source_event_ids"])

    def test_max_eight_and_memory_budget_exclusions_are_fully_audited(self) -> None:
        for index in range(10):
            self.append_memory(f"memory-{index}", f"ERROR_X42 candidate {index}")

        exact_boundary = pack_working_set(
            self.store,
            self.task_id,
            self.query(),
            [self.allowed],
            _SectionCounter(block=PER_MEMORY_TOKEN_CAP),
        )
        self.assertEqual(len(exact_boundary.included_memory_ids), MAX_MEMORIES)
        decisions = exact_boundary.retrieval_audit["memory"]["candidates"]
        self.assertEqual(len(decisions), 10)
        self.assertEqual([item["reason"] for item in decisions].count("max_memories"), 2)
        self.assertEqual(exact_boundary.memory_tokens, MEMORY_TOKEN_BUDGET)

        budget_limited = pack_working_set(
            self.store,
            self.task_id,
            self.query(),
            [self.allowed],
            _SectionCounter(block=500, header=100),
        )
        self.assertEqual(len(budget_limited.included_memory_ids), 7)
        budget_decisions = budget_limited.retrieval_audit["memory"]["candidates"]
        self.assertEqual(
            [item["reason"] for item in budget_decisions].count("memory_token_budget"),
            3,
        )

    def test_state_and_per_memory_token_boundaries_are_exact(self) -> None:
        at_boundary = pack_working_set(
            self.store,
            self.task_id,
            self.query(),
            [self.allowed],
            _SectionCounter(state=STATE_TOKEN_BUDGET),
        )
        self.assertEqual(at_boundary.state_tokens, STATE_TOKEN_BUDGET)
        with self.assertRaisesRegex(WorkingSetError, "task state requires 2049"):
            pack_working_set(
                self.store,
                self.task_id,
                self.query(),
                [self.allowed],
                _SectionCounter(state=STATE_TOKEN_BUDGET + 1),
            )

        self.append_memory("oversized", "ERROR_X42 oversized candidate")
        excluded = pack_working_set(
            self.store,
            self.task_id,
            self.query(),
            [self.allowed],
            _SectionCounter(block=PER_MEMORY_TOKEN_CAP + 1),
        )
        decision = excluded.retrieval_audit["memory"]["candidates"][0]
        self.assertEqual((decision["decision"], decision["reason"]), (
            "excluded",
            "per_memory_token_cap",
        ))

    def test_missing_scope_state_and_invalid_counter_fail_closed(self) -> None:
        with self.assertRaisesRegex(WorkingSetError, "explicit exact scope"):
            pack_working_set(
                self.store, self.task_id, self.query(), [], OfflineWhitespaceTokenCounter()
            )
        with self.assertRaisesRegex(WorkingSetError, "no committed task state"):
            pack_working_set(
                self.store,
                str(uuid.uuid4()),
                self.query(),
                [self.allowed],
                OfflineWhitespaceTokenCounter(),
            )
        with self.assertRaisesRegex(WorkingSetError, "invalid count"):
            pack_working_set(
                self.store, self.task_id, self.query(), [self.allowed], _InvalidCounter(-1)
            )
        with self.assertRaisesRegex(WorkingSetError, "token counter failed"):
            pack_working_set(
                self.store,
                self.task_id,
                self.query(),
                [self.allowed],
                _InvalidCounter(RuntimeError("broken counter")),
            )

    def test_llama_cpp_counter_is_loopback_only_exact_and_non_retried(self) -> None:
        for url in (
            "https://127.0.0.1/tokenize",
            "http://192.0.2.1/tokenize",
            "http://127.0.0.1/v1/tokenize",
            "http://user:secret@127.0.0.1/tokenize",
        ):
            with self.subTest(url=url), self.assertRaises(WorkingSetError):
                LlamaCppTokenCounter(url)

        with tokenize_server("valid") as (url, handler):
            counter = LlamaCppTokenCounter(url, timeout=2)
            self.assertEqual(counter.count("one two three"), 3)
            self.assertEqual(handler.calls, 1)
        with tokenize_server("bad-token") as (url, _):
            with self.assertRaisesRegex(WorkingSetError, "invalid token id"):
                LlamaCppTokenCounter(url).count("one")
        with tokenize_server("bad-json") as (url, _):
            with self.assertRaisesRegex(WorkingSetError, "invalid JSON"):
                LlamaCppTokenCounter(url).count("one")
        with tokenize_server("http-error") as (url, handler):
            with self.assertRaisesRegex(WorkingSetError, "HTTP 500"):
                LlamaCppTokenCounter(url).count("one")
            self.assertEqual(handler.calls, 1)


if __name__ == "__main__":
    unittest.main()
