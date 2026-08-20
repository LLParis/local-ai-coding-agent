# Memory architecture decision

Date: 2026-08-20

## Decision

Memory v1 is a sovereign local durability and retrieval layer, not a hosted service and not a vector database. Its authority chain is:

```text
append-only canonical JSONL events
              ↓ replay
SQLite typed projections + FTS5 word/trigram indexes
              ↓ retrieval gate
validated current-state ledger + cited relevant memories
              ↓ context packer
model request
```

Build the append-only log, typed task-state ledger, explicit temporal/provenance model, SQLite FTS5 retrieval, and loss-checked compaction first. Defer vectors and entity boosting until a real corpus plus the frozen evaluation suite demonstrates a failure that they fix. Do not make Mem0, Zep, another hosted service, or a graph store a v1 dependency.

This decision specializes the memory plane proposed in [`HARNESS_FUSION_DECISION.md`](HARNESS_FUSION_DECISION.md). It does not authorize implementation in this slice.

## Research reconciliation

The first user packet at `C:\Users\sirlo\.codex\attachments\49afb434-ee68-48f7-8476-54fb22971ac1\pasted-text.txt` correctly separates conversational history from long-term memory, requires persistence outside any one session, distinguishes user/agent/run scopes, and highlights recent-context extraction, deduplication, retrieval gates, query expansion, and explainable multi-signal ranking (`pasted-text.txt:65-141,155-243,254-443`; SHA-256 `112452dabdbd2bd02ec39c8c80a8c9f26b83c738a7892a7d631f1ce9bbd0e887`). Its three-store Mem0 explanation describes a moving implementation and is not current authority.

The second packet at `C:\Users\sirlo\.codex\attachments\676de629-2744-4c6e-9d8d-54be691a2fc2\pasted-text.txt` contributes the right taxonomy—procedural skills, semantic facts, episodic events—and the right lifecycle verbs: add, supersede, retire, attribute, and consolidate (`pasted-text.txt:149-258`; SHA-256 `5fa7a8b0068f5e9b6f7c08f786ef2425071c7c4eb2f5bd1f837c6b8ec6a85e26`). Its small arena usefully tests exact recall, cross-language recall, and temporal supersession (`:359-505`), and its graph experiment reports high latency plus lost or misattributed detail (`:516-581`). These are useful hypotheses, not controlled benchmarks.

The third packet at `C:\Users\sirlo\.codex\attachments\4fad4d37-f6d6-423f-8779-0628668789ac\pasted-text.txt` proposes Hindsight, Obsidian Headless, and DeepSeek as a continuous-memory stack (SHA-256 `cdc0986939c8cec5074ac5095ae91e06bc8f5e9e100494837c572828dbfc85f5`). Its valuable additions are Knowledge Pages as derived projections, explicit concern about memory bloat, and class/TTL-based garbage collection. Claims that an Obsidian vault should be canonical memory, autonomous nightly reflection is inherently safe, unlimited local compute removes context failure, or agents “sandbag” from compute anxiety are not accepted without evidence.

Current Mem0 OSS v3 differs materially. The primary source was inspected at main commit [`ed38ddf8731fb7fab7c41bb0f8ab3185df23e7c5`](https://github.com/mem0ai/mem0/tree/ed38ddf8731fb7fab7c41bb0f8ab3185df23e7c5):

- OSS graph memory was removed; the old external graph drivers and `enable_graph` configuration are gone, while graph memory is a Platform feature ([migration source, lines 288-307](https://github.com/mem0ai/mem0/blob/ed38ddf8731fb7fab7c41bb0f8ab3185df23e7c5/docs/migration/oss-v2-to-v3.mdx#L288-L307)).
- OSS retrieval over-fetches semantic candidates, computes optional BM25 and entity boosts, then fuses the three signals. BM25/entity signals only rerank semantic candidates; they do not expand recall ([main.py, lines 1483-1575](https://github.com/mem0ai/mem0/blob/ed38ddf8731fb7fab7c41bb0f8ab3185df23e7c5/mem0/memory/main.py#L1483-L1575)).
- `explain=True` returns semantic, BM25, entity, raw, divisor, final, and threshold components ([scoring.py, lines 55-128](https://github.com/mem0ai/mem0/blob/ed38ddf8731fb7fab7c41bb0f8ab3185df23e7c5/mem0/utils/scoring.py#L55-L128)). Adopt this explainability principle.
- Extraction is single-pass ADD-only with exact MD5 text deduplication; changed facts accumulate and retrieval is expected to surface the current one ([migration source, lines 309-335](https://github.com/mem0ai/mem0/blob/ed38ddf8731fb7fab7c41bb0f8ab3185df23e7c5/docs/migration/oss-v2-to-v3.mdx#L309-L335); [main.py, lines 916-946](https://github.com/mem0ai/mem0/blob/ed38ddf8731fb7fab7c41bb0f8ab3185df23e7c5/mem0/memory/main.py#L916-L946)). Coding constraints and completion state need explicit supersession, not a ranking guess.
- The optional NLP path can degrade to fewer signals. Mem0 documents semantic-only fallback when spaCy is absent and loss of BM25 when Qdrant lacks `fastembed` ([migration source, lines 336-361](https://github.com/mem0ai/mem0/blob/ed38ddf8731fb7fab7c41bb0f8ab3185df23e7c5/docs/migration/oss-v2-to-v3.mdx#L336-L361)). Current source loads English `en_core_web_sm` and returns no entities when unavailable ([spacy_models.py, lines 19-84](https://github.com/mem0ai/mem0/blob/ed38ddf8731fb7fab7c41bb0f8ab3185df23e7c5/mem0/utils/spacy_models.py#L19-L84); [entity_extraction.py, lines 712-730](https://github.com/mem0ai/mem0/blob/ed38ddf8731fb7fab7c41bb0f8ab3185df23e7c5/mem0/utils/entity_extraction.py#L712-L730)). An [open issue](https://github.com/mem0ai/mem0/issues/4884) reports that non-English retrieval can silently lose both BM25 and entity signals.
- Current main protects identity fields from freeform metadata, but open issue reports still identify entity ambiguity and cross-scope `linked_memory_ids` risks ([entity disambiguation #5438](https://github.com/mem0ai/mem0/issues/5438), [scope isolation #5439](https://github.com/mem0ai/mem0/issues/5439)). Treat issue reports as risk evidence, not proof of current shipped behavior. V1 avoids the whole entity layer and makes scope columns immutable.

Mem0 remains a future adapter/reference candidate. It is not installed for v1.

## Hindsight and Obsidian boundary

Hindsight is a credible future adapter and benchmark candidate. Current primary source was checked at main commit [`e20bb290f795dd83e158a18ad90a2a45ced8a5d9`](https://github.com/vectorize-io/hindsight/tree/e20bb290f795dd83e158a18ad90a2a45ced8a5d9). It is MIT licensed, supports self-hosting and local Ollama, and exposes three clear operations—retain, recall, and reflect. Its memory banks are isolated; observations are deduplicated, evidence-grounded projections with exact supporting quotes and proof counts ([best-practices source, lines 18-45](https://github.com/vectorize-io/hindsight/blob/e20bb290f795dd83e158a18ad90a2a45ced8a5d9/hindsight-docs/src/pages/best-practices.mdx#L18-L45)). A local MCP deployment uses embedded PostgreSQL, persists locally, and can use Ollama without an API key ([local MCP source](https://github.com/vectorize-io/hindsight/blob/e20bb290f795dd83e158a18ad90a2a45ced8a5d9/hindsight-docs/docs-integrations/local-mcp.md)).

Those strengths do not make Hindsight authority. Its retain path uses an LLM to extract facts and explicitly does not preserve raw input verbatim, so the command center's canonical events and provenance must remain underneath it. Its own 0.9.0 report says automatic recall on every prompt scored worse than no memory in an earlier coding benchmark; the improved integration reflects once on the session goal and keeps reflect as an explicit tool rather than adding retrieval latency/noise to every turn ([official 0.9.0 report](https://hindsight.vectorize.io/blog/2026/08/06/hindsight-0-9-0)). No auto-retain, auto-recall, or nightly reflect writes to v1 by default. An exact local model/provider path must also qualify; a [current Ollama/gpt-oss issue](https://github.com/vectorize-io/hindsight/issues/3246) shows why nominal local support is not execution proof.

Hindsight Knowledge Pages fit only as derived projections. Hindsight itself describes them as database-view-like renderings over processed memory: raw documents remain the source of truth, and deleting a page loses nothing because it can be regenerated. A future adapter may benchmark its observations, reflect answers, and Knowledge Pages against the frozen suite, but any returned claim re-enters v1 as a cited candidate and passes the same validator.

Obsidian Headless is not a memory database. The official documentation describes an **open-beta** command-line transport for Obsidian Sync and requires an active Sync subscription ([Headless Sync](https://obsidian.md/help/sync/headless)); official pricing starts at a recurring $4/month annually or $5/month monthly ([pricing](https://obsidian.md/pricing)). That conflicts with the no-recurring-cost core and adds remote transport/storage limits. No Obsidian component is installed for v1.

A local Markdown export remains optional: generate human-readable current-state, decisions, and Knowledge Page-style documents from SQLite under an export directory. Each page carries source event IDs, state hash, and a generated marker; it is never read back as authority automatically. Opening that directory in free local Obsidian is an operator choice. If the user already pays for Sync, Headless may transport the export, but Sync state is neither canonical nor required. A deliberate import command may turn a human edit into a new user-authored event.

## V1 runtime layout

The configured runtime root is `${CODING_INTELLIGENCE_STATE}\memory\v1`; it is outside every project repository. On EXCALIBUR it is canonical. A disconnected Mac edge may append to a host-local spool and later import records by event hash.

```text
memory/v1/
  events/<host-id>/<session-id>.jsonl  canonical append-only segments
  blobs/sha256/<prefix>/<digest>       large content-addressed payloads
  archive/<year-month>/<manifest>.zst  verified cold payload bundles
  checkpoints/<task-id>/<revision>.json
  memory.sqlite3                       rebuildable projection and indexes
  schemas/                             pinned JSON Schemas
```

The live PC substrate already proved SQLite `3.50.4`, `ENABLE_FTS5`, the `unicode61` tokenizer, and the `trigram` tokenizer in memory. Runtime startup must repeat that capability probe and fail the memory health check if either required tokenizer is absent. There is no silent semantic-only or reduced-index fallback.

SQLite opens with `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`, and `trusted_schema=OFF`. The raw JSONL remains authoritative: append canonical bytes, flush and fsync, then apply the event to SQLite in one transaction. If a crash lands between those operations, startup replays events after the projection's last committed hash. A projection row can be rebuilt; a raw event cannot be inferred from a projection row.

## Tiered retention and garbage collection

Append-only does not mean every byte remains hot forever. Compact event records, hashes, state transitions, and provenance survive; bulky payloads follow an explicit class and TTL.

| Class | Contents | Hot retention | After hot TTL |
|---|---|---:|---|
| `core` | objectives, constraints, decisions, state/compaction commits, memory rows, event headers/hash chain, provenance, blocker/open-tool state | indefinite | never GC; compact by schema only |
| `evidence` | authoritative tests/verifier outputs, accepted diffs, artifact manifests, source excerpts | 180 days after task terminal | move to verified compressed local archive; remain pinned while referenced by active state, an active/disputed memory, or a production claim |
| `bulk` | full model responses, verbose compiler/tool/HTTP output, large retrieved bodies | 14 days after task terminal | archive for 90 additional days when cited; otherwise evict payload and retain digest, byte count, source locator, bounded head/tail excerpt, and eviction event |
| `scratch` | copied stages, caches, downloads, temporary media and intermediate transforms | task terminal + 24 hours | delete if no committed evidence or artifact reference points to it |

Every blob row carries `retention_class`, `created_at`, `task_terminal_at`, `expires_at`, `size_bytes`, `sha256`, archive locator/hash, reference count, and pin reasons. The configured hot-blob byte budget is finite. At 80% usage GC processes eligible `scratch` then `bulk`; at 95% it stops accepting new unpinned bulk payloads and emits a health failure rather than deleting `core` or protected evidence.

GC is deterministic and non-agentic. A payload is eligible only after the task is terminal, no active task-state/memory/evidence reference protects it, and any required archive has been written and hash-verified. Hot deletion appends `blob/archived` or `blob/evicted`; the originating event line remains with its content hash and availability state. Compaction cannot change retention class, and a model cannot unpin evidence. Privacy purge remains the only operator-authorized rewrite path.

## Canonical event schema

Every JSONL line conforms to this logical schema:

```json
{
  "schema": "coding-intelligence-memory-event/v1",
  "event_id": "uuid",
  "host_id": "excalibur",
  "session_id": "uuid",
  "task_id": "uuid-or-null",
  "parent_event_id": "uuid-or-null",
  "seq": 42,
  "occurred_at": "RFC3339 UTC",
  "type": "memory/committed",
  "actor": {"kind": "user|agent|tool|system", "id": "...", "model": "..."},
  "scope": {
    "owner_id": "local-owner",
    "workspace_id": "canonical-root-sha256-or-null",
    "task_id": "uuid-or-null",
    "agent_role": "implementer|verifier|architect|operator|null",
    "session_id": "uuid"
  },
  "payload": {},
  "payload_sha256": "sha256:...",
  "previous_record_sha256": "sha256:...|null",
  "record_sha256": "sha256:..."
}
```

The hash covers the previous hash plus canonical record fields excluding `record_sha256`. A large payload becomes a blob reference with byte length and SHA-256. One writer owns one `(host_id, session_id)` segment; import rejects duplicate hashes, repeated sequence numbers with different hashes, and broken chains.

Required event families are `task/*`, `model/request|response`, `tool/intent|result|outcome-unknown`, `file/observed|edited`, `verification/result`, `artifact/observed`, `memory/candidate|committed|superseded|retired|disputed`, `state/proposed|committed`, `compaction/proposed|rejected|committed`, and `session/ended`. Checkpoint before model dispatch, before a top-level side-effecting tool body, and before the next model step.

## SQLite projection schema

The DDL below is the v1 logical contract; migrations may add indexes without changing field meaning.

```sql
CREATE TABLE event_index (
  event_id TEXT PRIMARY KEY,
  host_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  segment_path TEXT NOT NULL,
  byte_offset INTEGER NOT NULL,
  payload_sha256 TEXT NOT NULL,
  record_sha256 TEXT NOT NULL UNIQUE,
  UNIQUE(host_id, session_id, seq)
);

CREATE TABLE memory_item (
  memory_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN
    ('constraint','preference','decision','fact','lesson','episode','procedure_ref')),
  subject TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object_json TEXT NOT NULL CHECK(json_valid(object_json)),
  searchable_text TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  workspace_id TEXT,
  task_id TEXT,
  agent_role TEXT,
  session_id TEXT,
  status TEXT NOT NULL CHECK(status IN
    ('active','superseded','retired','disputed')),
  valid_from TEXT,
  valid_to TEXT,
  observed_at TEXT,
  recorded_at TEXT NOT NULL,
  review_after TEXT,
  verification_status TEXT NOT NULL CHECK(verification_status IN
    ('user_authority','observed','tested','source_verified','inferred')),
  supersedes_id TEXT REFERENCES memory_item(memory_id),
  source_event_id TEXT NOT NULL REFERENCES event_index(event_id),
  content_sha256 TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  UNIQUE(scope_key, content_sha256)
);

CREATE TABLE evidence (
  evidence_id TEXT PRIMARY KEY,
  memory_id TEXT REFERENCES memory_item(memory_id),
  task_id TEXT,
  source_kind TEXT NOT NULL CHECK(source_kind IN
    ('user','event','file','symbol','command','test','web','artifact','model_inference')),
  source_locator TEXT NOT NULL,
  source_event_id TEXT REFERENCES event_index(event_id),
  source_sha256 TEXT,
  observed_at TEXT NOT NULL,
  authority TEXT NOT NULL,
  excerpt TEXT,
  CHECK(memory_id IS NOT NULL OR task_id IS NOT NULL)
);

CREATE TABLE task_state (
  task_id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL,
  state_json TEXT NOT NULL CHECK(json_valid(state_json)),
  state_sha256 TEXT NOT NULL,
  through_event_id TEXT NOT NULL REFERENCES event_index(event_id),
  committed_at TEXT NOT NULL
);

CREATE TABLE consolidation_commit (
  commit_id TEXT PRIMARY KEY,
  source_first_event_id TEXT NOT NULL,
  source_last_event_id TEXT NOT NULL,
  proposal_sha256 TEXT NOT NULL,
  validator_json TEXT NOT NULL CHECK(json_valid(validator_json)),
  verifier_json TEXT NOT NULL CHECK(json_valid(verifier_json)),
  committed_event_id TEXT NOT NULL REFERENCES event_index(event_id)
);

CREATE TABLE blob_catalog (
  blob_sha256 TEXT PRIMARY KEY,
  size_bytes INTEGER NOT NULL,
  media_type TEXT,
  hot_locator TEXT,
  retention_class TEXT NOT NULL CHECK(retention_class IN
    ('core','evidence','bulk','scratch')),
  availability TEXT NOT NULL CHECK(availability IN
    ('hot','archived','evicted')),
  created_at TEXT NOT NULL,
  task_terminal_at TEXT,
  expires_at TEXT,
  archive_locator TEXT,
  archive_sha256 TEXT,
  reference_count INTEGER NOT NULL,
  pin_reasons_json TEXT NOT NULL CHECK(json_valid(pin_reasons_json))
);

CREATE TABLE retrieval_audit (
  retrieval_id TEXT PRIMARY KEY,
  request_event_id TEXT NOT NULL REFERENCES event_index(event_id),
  query_json TEXT NOT NULL CHECK(json_valid(query_json)),
  candidate_json TEXT NOT NULL CHECK(json_valid(candidate_json)),
  packed_json TEXT NOT NULL CHECK(json_valid(packed_json)),
  created_at TEXT NOT NULL
);
```

`scope_key` is a canonical, escaped encoding of owner, workspace, task, agent role, and session; it is derived from immutable columns rather than accepted from freeform metadata. Two external-content FTS tables index `memory_id` plus `searchable_text`: `unicode61 remove_diacritics 2` for word retrieval and `trigram` for identifiers, substrings, and scripts without whitespace tokenization. The reducer updates both only from committed projection events. Exact indexes on `scope_key`, `workspace_id`, `task_id`, `subject`, `predicate`, status, and validity time remain separate from FTS.

## Typed current-state ledger

`task_state.state_json` must validate before commit and contain stable item IDs so a rephrased summary cannot silently lose an obligation:

```json
{
  "schema": "coding-intelligence-task-state/v1",
  "task_id": "uuid",
  "objective": {"text": "...", "source_event_id": "..."},
  "success_criteria": [{"id": "sc-1", "text": "...", "status": "open|met", "evidence_ids": []}],
  "constraints": [{"id": "c-1", "text": "...", "status": "active|retired", "source_event_id": "..."}],
  "decisions": [{"id": "d-1", "text": "...", "rationale": "...", "source_event_ids": []}],
  "completed": [{"id": "w-1", "text": "...", "evidence_ids": [], "completed_at": "..."}],
  "current_action": {"text": "...", "owner": "...", "started_at": "..."},
  "next_actions": [{"id": "n-1", "order": 1, "text": "...", "depends_on": []}],
  "blockers": [{"id": "b-1", "text": "...", "status": "active|cleared", "evidence_ids": []}],
  "open_tool_calls": [{"call_id": "...", "tool": "...", "outcome": "not_started|unknown"}],
  "artifacts": [{"path": "...", "sha256": "...", "evidence_id": "..."}],
  "through_event_id": "uuid",
  "previous_state_sha256": "sha256:..."
}
```

The objective, active constraints, open success criteria, blockers, open tool calls, and next-action IDs are lossless invariants. Models may improve prose, but cannot remove or close an item without a cited event and, where required, evidence.

## Memory classes and authority

- **Working memory:** the current task ledger and recent event tail. Always packed; never retrieved by similarity.
- **Semantic memory:** durable constraints, preferences, decisions, facts, and verified lessons. Retrieved by explicit scope and relevance.
- **Episodic memory:** dated observations and past task outcomes. It answers “what happened/as of when” and never silently becomes a current fact.
- **Procedural memory:** a pointer to a versioned skill or runbook plus proof of the versions that succeeded. A transcript is not promoted to a skill merely because the model summarized it.

User preferences are authoritative for the user's intent. Project files are authoritative for current code. Tests are authoritative only for the behavior they actually exercise. Official external sources are authoritative only within their documented scope and observation time. Model inference cannot by itself mark work complete, supersede a user constraint, or establish a system fact.

## Temporal supersession, retirement, and disputes

Normal maintenance never deletes history.

- **ADD:** append a new active memory with its evidence and validity interval.
- **SUPERSEDE:** append the replacement, point `supersedes_id` at the prior item, and project the prior item to `superseded` with `valid_to`. Current queries return the replacement; `as_of` queries can return the prior fact.
- **RETIRE:** append a retirement event and project the item to `retired` without a replacement. Use when a rule or preference no longer applies.
- **DISPUTE:** preserve both claims, mark them disputed, and return the conflict plus provenance until an authoritative event resolves it.
- **NOOP:** record the evaluated candidate and reason it did not change state when audit value warrants it.

`recorded_at` answers when the system learned a claim; `observed_at` answers when evidence was observed; `valid_from`/`valid_to` answer when the claim applies. “Latest” sorts by validity and authority, not write order alone. Scope columns are immutable after commit; metadata cannot move a memory to another owner/workspace/task/agent.

Only an explicit operator-authorized privacy purge may physically remove sensitive raw data. That administrative operation creates a new sanitized log generation and a non-sensitive purge tombstone; no agent or consolidation job can invoke it.

## Provenance contract

Every committed memory has at least one evidence row. File evidence includes canonical path, content SHA-256, and line/symbol when available. A command or test includes argv, cwd, exit code, output hash, and timestamp. Web evidence includes the direct URL, publisher, retrieval time, and content hash. User evidence cites the exact user event. A derived memory lists all parent evidence; the extraction model and prompt/version are recorded separately from authority.

Secrets, credentials, auth files, raw environment dumps, and unrelated personal data are rejected during semantic-memory admission. The raw event layer may store a redacted record or a restricted blob reference as dictated by the task evidence contract, but retrieval never injects a secret merely because text search matched it.

## Consolidation and admission

Raw events are recorded continuously. Semantic consolidation runs at a task terminal boundary, before compaction, or by explicit operator request—not after every conversational turn.

Candidate operations are `ADD`, `SUPERSEDE`, `RETIRE`, `DISPUTE`, `NOOP`, and `PROCEDURE_CANDIDATE`. A small local model may propose them, but the deterministic validator owns admission:

1. The candidate cites an existing event range and evidence IDs.
2. Scope is explicit and immutable.
3. Content is reusable beyond the current message, non-secret, and not merely a transient model thought.
4. Completion and capability claims cite tests or equivalent direct evidence.
5. Exact canonical SHA-256 duplicates become `NOOP`.
6. Near duplicates are surfaced by FTS and resolved as `NOOP`, `SUPERSEDE`, or `DISPUTE`; wording similarity alone never overwrites a fact.
7. A procedure becomes a skill candidate only after at least two independently verified successes and still requires separate skill review/versioning.

Commit order is raw `memory/candidate` event, deterministic validation, optional independent semantic verifier, raw terminal decision event, then one SQLite projection transaction. A rejection leaves memory unchanged and does not trigger an immediate retry loop.

## Retrieval gate and context packing

The current task ledger plus explicitly tagged global user constraints are always included. All other long-term memory passes the retrieval gate.

1. Build a deterministic query from the user request, task objective, file/symbol names, exact error strings, tool names, and active blockers. Optional local-model query expansion may add audited variants but cannot remove the literal query.
2. Require explicit scope. Default union is owner-global plus exact workspace plus exact task; agent-role memory is included only for that role. Wildcard cross-owner retrieval is forbidden.
3. Apply status and `as_of` validity filters before ranking.
4. Union candidates from exact structured indexes, word FTS, and trigram FTS. Keyword search expands recall; it is not restricted to a semantic candidate pool.
5. Fuse channel ranks deterministically and return an explanation containing channel, rank, exact matches, scope, temporal status, evidence authority, and source IDs. Recency is a tie-breaker, not a truth signal.
6. Pack the task ledger first, then at most eight long-term memories. Initial budget is 2,048 tokens for state plus 4,096 tokens for retrieved memory; each memory is capped at 512 tokens with provenance. Record every included and excluded candidate in `retrieval_audit`.

Expose `memory_search(query, scopes, as_of?, kinds?, top_k?)` and `memory_get(memory_id)` to interactive agents. Automatic retrieval and explicit tool retrieval use the same engine. If evidence is weak, contradictory, stale, or absent, return that fact rather than inventing an answer.

## Loss-checked compaction

Compaction follows the two-phase design in the fusion decision and never deletes raw events.

1. Freeze the source range by first/last event ID and record hashes.
2. Generate a candidate task ledger and summary with cited event/evidence IDs.
3. Deterministically compare stable ID sets: objective, active constraints, open criteria, blockers, open/unknown tool calls, next actions, artifact hashes, and unresolved disputes must survive exactly.
4. Re-hash cited artifacts and reject stale evidence.
5. Require evidence for every newly completed item and every cleared blocker.
6. Have an independent verifier check semantic omissions and contradictions after deterministic checks pass.
7. Append `compaction/committed` with source range, candidate hash, previous state hash, validator report, verifier identity, and retained-tail boundary. A rejection appends `compaction/rejected` and keeps the last valid state.

On resume, verify event chains, replay projections through the last valid compaction, classify unmatched tools as `not_started` or `outcome_unknown`, verify scoped artifact hashes, and pack the next request from the validated state. Never replay a potentially completed side effect merely because its result is missing.

External memory can make continuity effectively unbounded in elapsed time and number of sessions. It cannot preserve every latent nuance of a model's prior hidden state. Larger context still improves local reasoning and reduces retrieval/summary dependence.

Local throughput and low marginal inference cost improve how much work can be attempted; they do not remove finite attention, context packing errors, retrieval misses, tool-contract failures, or bad compaction. Models do not experience compute anxiety. “Sandbagging” is useful only as an observable outcome label—omitted steps, skipped verification, premature stopping—not as a psychological explanation.

## Frozen memory evaluation suite

Freeze `evals/memory/v1/` before implementation tuning: forty cases, five in each tranche.

1. **Exact/identifier recall:** paths, symbols, commands, error codes, task IDs.
2. **Paraphrase recall:** same fact with different vocabulary.
3. **Multilingual recall:** English facts queried in Chinese and the reverse, including proper names and numbers.
4. **Temporal truth:** May→June launch, 7 PM→9 PM arrival, current and historical `as_of` queries, retirement without replacement.
5. **Scope isolation:** owner, workspace, task, agent, and session collisions; Apple/Python same-name ambiguity across scopes.
6. **Conflict/provenance/negative:** contradictory sources, authoritative correction, no-memory answer, source citation, secret rejection.
7. **Compaction retention:** long histories with many constraints, open items, blockers, decisions, and artifact hashes across repeated compactions.
8. **Crash/replay/retention:** interruption before model, after a side effect before result, after verification, GC refusal for protected evidence, and eligible bulk/scratch archive or eviction.

Each case contains seed events, query and scope, expected memory IDs, forbidden IDs, expected state ledger, evidence requirements, context budget, and expected answer facts. Model-assisted phases run three times; failures remain in the denominator.

Hard promotion requirements:

- 100% forbidden-scope exclusion and zero cross-scope leakage;
- 100% retention of objective, active constraints, open criteria, blockers, unknown tool outcomes, and artifact hashes through compaction/resume;
- 100% current and `as_of` temporal answers;
- zero duplicate side-effect recommendations after crash recovery;
- zero GC removal of task invariants or evidence protected by active state, active/disputed memory, or a production claim;
- 100% provenance coverage for injected memories and completed claims;
- at least 95% Recall@10 on exact/identifier cases and at least 90% overall Recall@10;
- zero fabricated memory on negative cases;
- truthful capability/latency telemetry for every retrieval channel.

Latency is recorded at corpus sizes 100, 1,000, 10,000, and 100,000 but receives a production threshold only after the first real local baseline. Correctness gates are not relaxed for speed.

## When richer retrieval earns entry

Hindsight is tested as a read-only derived-memory adapter after v1 passes its hard gates. Feed a disposable bank only committed, cited v1 facts/events with stable document IDs and exact scope tags. Compare `recall`, goal-time `reflect`, observations, and Knowledge Pages against the local FTS baseline on the same forty cases plus held-out coding-history tasks. Its output never writes through directly: it must name source evidence and re-enter v1 as a candidate. Promotion requires every v1 hard gate, measurable improvement on the failure family that motivated it, truthful local latency/resource telemetry, and repeated success with the exact Ollama model/configuration. Automatic per-turn retain/recall and scheduled reflection remain off unless a separate frozen experiment proves them beneficial.

Vector retrieval is evaluated only after the v1 corpus contains at least 500 active memories or three real failures share a semantic/paraphrase retrieval cause. It must use a pinned local embedding model and improve overall held-out Recall@10 by at least five percentage points without any hard-gate regression, unexplained fallback, scope leakage, or more than 2x the measured FTS p95 latency.

Entity boosting is evaluated only after at least twenty frozen entity-centric/ambiguous cases exist. It must improve that tranche by at least five points while preserving 100% scope isolation and disambiguation. Every score component must be returned, as Mem0's `explain` path does. Entity storage remains a retrieval index, never memory authority.

Graph memory is not a default or a planned v1 milestone. It enters evaluation only if a frozen multi-hop tranche demonstrates a repeated failure that FTS plus vector retrieval cannot solve and a fully local graph candidate beats them enough to justify its latency, maintenance, and temporal-consistency cost.

## Explicitly rejected for v1

- Mem0, Zep, Supabase, Pinecone, or another hosted/runtime dependency.
- Hindsight as canonical v1 storage, automatic auto-retain/auto-recall, or an autonomous nightly-reflect write path before frozen evaluation.
- Obsidian Headless/Sync as memory authority or required transport; local Markdown is an optional generated operator view only.
- A default vector, entity, or graph store before corpus evidence.
- Mem0's ADD-only “current fact wins by ranking” as task-state authority.
- Exact-string dedup as the only consolidation mechanism.
- Silent retrieval degradation when optional NLP or index capabilities are absent.
- English-only entity/lemma processing as a correctness dependency.
- A giant `MEMORY.md` injected into every request.
- Automatic per-turn “reflection” that writes model guesses into durable truth.
- Destructive replacement of superseded history.
- Model-authored compaction committed without invariant, provenance, and artifact-hash validation.
- “Unlimited” local compute as a solution to finite context/attention, or compute anxiety as an explanation for model behavior.

## Completion boundary

This architecture is ready for implementation when the JSON Schemas and SQL migration are frozen, the forty-case fixtures exist, and the event writer/replayer plus FTS capability probe have named owners. Memory v1 is production-proven only after the hard evaluation requirements pass on fresh-process replay with the current Qwen implementer and independent verifier. Until then, this document is the decision and the research packets are evidence inputs—not proof that any memory product solves the command center's continuity problem.
