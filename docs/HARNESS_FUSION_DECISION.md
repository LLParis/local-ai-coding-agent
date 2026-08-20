# Harness fusion decision

Date: 2026-08-20

## Outcome

Do not merge Codex, Claude Code, DeepSeek Harness, and Pi into one giant codebase. The best system is a small command-center protocol with replaceable execution adapters and a durable memory plane. Keep the current thin lane as the production execution authority, keep hosted Codex as architect and arbiter, add loss-checked continuity as shared infrastructure, and A/B Pi and DeepSeek as local interactive adapters. Port a few proven Claude mechanisms into the shared protocol; do not port Claude's product.

```text
                         hosted Codex
                      architect / arbiter
                               |
task capsule -> router -> bounded executor adapter -> normalized event log
                    |          |              |
                    |          |              +-> hidden tests + Devstral verifier
                    |          +-> Pi or DeepSeek interactive experiment
                    +-> current one-call thin lane (production)
                               |
                 loss-checked continuity memory
          raw events -> typed state -> retrieval -> working set
```

This keeps the parts that improve outcomes while avoiding a multi-month rewrite and the permission/retry machinery that caused the original failure.

## Evidence boundary

- **Current command center / Codex-derived operating contract:** live repository code and completed real tasks. The locally installed Codex app exposes packaged binaries and `app.asar` at `C:\Program Files\WindowsApps\OpenAI.Codex_26.814.5517.0_x64__2p2nqsd0c76g0\app\resources`, but no quickly discoverable auditable Codex source snapshot. This decision therefore uses the command center's proven Codex-derived contracts rather than pretending the packaged binary was source-reviewed.
- **Claude Code:** read-only source snapshot at `D:\src`. It contains no `.git` metadata, so claims below cite exact snapshot paths and lines. Key file SHA-256 values are `cli/print.ts` `77bb8bed6ebe3e5fbc3c6521dcddab479af8ce9e3df82b2a103c27bf14cc573d`, `services/tools/StreamingToolExecutor.ts` `ee17dce704af73787c9ed76dbbc638167521e5c1f68c9436149378bb3358601c`, `utils/conversationRecovery.ts` `2668884b9abe434de32b1b1c56996a782b89914902cd9b44dfa4e81699cd642a`, and `tools/AgentTool/runAgent.ts` `7a99609b319cb1d1d1f593fdc4ac281a7512e99f29c4c31ea3e52cb681f9c622`.
- **DeepSeek Harness:** official source pinned at commit `141eb6fef83422698aef7a981029e843e8161534`; the bounded decision and exact source references are in [`DEEPSEEK_HARNESS_AUDIT.md`](DEEPSEEK_HARNESS_AUDIT.md).
- **Pi:** current primary documentation for [the minimal harness](https://pi.dev/docs/latest), [sessions](https://pi.dev/docs/latest/sessions), [compaction](https://pi.dev/docs/latest/compaction), [extensions](https://pi.dev/docs/latest/extensions), [security](https://pi.dev/docs/latest/security), [llama.cpp](https://pi.dev/docs/latest/llama-cpp), and [custom providers](https://pi.dev/docs/latest/custom-provider). Pi was not installed or run.
- **User research packet:** `C:\Users\sirlo\.codex\attachments\8f6c71dc-195a-4b8f-a024-5874f35ec2b2\pasted-text.txt`, SHA-256 `d6a39006e627cf31cf79347bb97a7e39dcfab034ad989460812406c1cb0185b3`. It is an anecdotal Reddit thread, not benchmark evidence.

## Best contribution from each system

| System | Keep | Do not inherit | Assigned role |
|---|---|---|---|
| Current thin lane | Immutable task capsule, one model response, explicit mutable paths, verifier-only context, isolated stage, real test, truthful trajectory, exact owned backend lifecycle | One-shot execution as the permanent interactive experience | Production bounded executor and promotion authority |
| Hosted Codex | Highest-judgment diagnosis, architecture, arbitration, source-aware implementation discipline | Cloud dependence as the only continuity option | Architect, escalation path, final arbiter while usage exists |
| Claude Code | Ordered tool lifecycle, schema/value validation, concurrency classification, balanced synthetic tool results, scoped subagent tools, max-turn limits, abort/cleanup ownership, structured headless output | Huge product surface, buffered transcript as the strongest durability layer, permission/UI complexity, automatic recovery guesses | Mechanism donor only |
| DeepSeek Harness | Replaceable provider/tool/session seams, append-only event-sourced session, semantic checkpoints, reconstructable request headers, explicit unknown tool outcomes, one-shot headless composition | Default five retries, 64-round Ralph loop, partial Windows ACL runner, raw telemetry, unrestricted plugin composition | Rich experimental substrate and future UI/session adapter |
| Pi | Small core, native llama.cpp route, RPC/JSON event modes, JSONL tree sessions, resume/fork, append-only compaction entries, simple extension/provider interface | No built-in sandbox, extensions running as the user, project trust mistaken for tool confinement, model-authored compaction accepted without our loss checks | First lightweight interactive/Qwen adapter to A/B against DeepSeek |

## Adopt now

### 1. Preserve the production lane

The production path remains exactly bounded: one model response, one isolated stage, explicit mutable/context/verifier paths, one authoritative test, and one normalized report. The contract is already explicit in `AGENTS.md:23-46`, implemented in `src/agent_continuity/local_edit.py:128-159,178-239,269-329`, and backed by immutable SHA-256 checkpoints in `src/agent_continuity/checkpoint.py:357-447`. No interactive harness replaces it before outcome parity.

Elevated model lifecycle remains outside every agent harness. `windows/Switch-ExcaliburBackend.ps1:10-114` switches only the two named Scheduled Tasks, proves listener health, restores the previous backend on failure, and reports `uacPrompt: false`. A permission mode inside Claude, DeepSeek, or Pi is not Windows elevation.

### 2. Use one ordered tool lifecycle

The shared future interactive executor should implement this minimal sequence:

```text
schema validation -> tool-specific validation -> scope/rule decision
-> durability checkpoint -> execute under owned abort signal
-> normalized result -> post-observation -> append terminal event
```

Claude validates model arguments with both a schema and tool-specific validator before hooks or execution (`D:\src\services\tools\toolExecution.ts:614-733`), runs pre-tool hooks and the permission decision before the body (`:775-969`), executes the tool with an owned context (`:1180-1223`), and records normalized success or failure before cleanup (`:1397-1579,1631-1744`). DeepSeek independently uses a comparable ordered tool pipeline and checkpoints the call before external effects; its references are in the bounded audit. Adopt the shared ordering, not either implementation wholesale.

Only calls explicitly classified concurrency-safe may overlap. Exclusive calls form ordering barriers; results remain in model order. Every announced tool call receives exactly one terminal result, including cancellation or fallback. Claude demonstrates these mechanics in `D:\src\services\tools\StreamingToolExecutor.ts:34-150,153-205,265-404,407-519`. This matters for coherent replay, not for maximizing tool-call volume.

### 3. Eliminate permission friction by configuration

Automation gets no prompt path. It runs under one named `autonomous-stage` policy: permit only the capsule's tools and paths, deterministically deny everything else, and return the denial as a normal tool result. Interactive operator sessions may use a separate prompt-capable policy. Administrative lifecycle actions always go through the pre-owned Scheduled Tasks.

Claude's useful concept is the typed separation of `allow`, `deny`, and `ask`, plus an attached reason and optional durable rule update (`D:\src\types\permissions.ts:16-44,172-320`). Its `dontAsk` mode converts an unresolved ask into denial (`D:\src\utils\permissions\permissions.ts:473-518`), while explicit deny/ask rules remain ahead of bypass and allow rules (`:1061-1155,1169-1310`). The command center should use the simpler no-prompt subset. Do not port the classifier, UI carousel, or bypass-immune prompt cases into unattended execution.

### 4. Give subagents exact roles and ownership

Every child receives a role, provider/model, tool allowlist, maximum turns, parent task ID, transcript ID, and linked cancellation owner. Session grants from one child do not leak to another. On completion or abort, the parent drains the child, closes its transcript, and kills only that child's owned subprocesses.

Claude's source shows the valuable pieces: child `allowedTools` replace session allow rules so parent approvals do not leak (`D:\src\tools\AgentTool\runAgent.ts:249-329,465-479`); incomplete parent tool calls are filtered before a fork (`:368-373`); model, tool, transcript, and max-turn settings are explicit (`:340-353,697-756`); and MCP clients, caches, transcript mappings, todos, and background shells are cleaned in `finally` (`:816-855`). Use these ownership rules with the command center's existing role tournament. Do not default to teams, polling mailboxes, or hundreds of agents.

### 5. Standardize headless I/O

Every adapter returns NDJSON events plus one terminal result object. Stdout carries protocol only; diagnostics go to stderr. The result names status, model/provider, session/task ID, turns, model calls, elapsed time, stop reason, diff/test/verifier outcome, and evidence paths. A non-success terminal result produces a nonzero exit code after the event/checkpoint writer flushes.

Claude's headless path exposes explicit resume/fork, output mode, allowed tools, max turns, and budget fields (`D:\src\cli\print.ts:455-492`), guards NDJSON stdout from stray output (`:587-626`), streams without retaining the whole session when unnecessary (`:847-915`), and maps terminal states to output and exit status (`:917-973`). These are compact protocol ideas worth copying. Its full headless product startup is not.

## Persistent memory and loss-checked compaction

Smaller local context windows require persistent memory, but a summary alone is not memory. The command center should use five layers.

### 1. Append-only raw event log

The raw event stream is the source of truth. Each record has a monotonic sequence, session/task/parent IDs, timestamp, type, provider/model, payload hash, previous-record hash, and payload or content-addressed reference. Events include user objective, context offered, model request/response, tool intent/result, file observation/edit, test/verifier result, state update, compaction proposal/decision, and terminal status. Old records are never deleted or rewritten; a branch points to a parent sequence.

DeepSeek's session model supplies the event-sourcing and semantic-checkpoint foundation. Pi independently demonstrates JSONL tree sessions whose entries carry `id` and `parentId`, with resume, fork, clone, compaction, and branch summaries. The current lane already has the bounded evidence fields in `continuity-trajectory.json` and a hash-validated task/workspace checkpoint; these become producers into the shared log rather than being replaced.

### 2. Typed state ledger

Maintain one current projection with required fields:

- objective and success criteria;
- constraints and user preferences;
- decisions with rationale and source event IDs;
- completed items with proof;
- current action and owner;
- ordered next actions;
- blockers and the evidence that establishes each blocker;
- files, symbols, commands, tests, models, and hosts involved;
- artifact paths plus hashes and last verified sequence.

The projection is derived state, never authority. Every field points back to raw events. A model may propose changes, but only the state reducer commits them.

### 3. Retrieval index and working-set packer

Index raw events and artifacts by task, repository, file, symbol, command, model, error signature, decision, and evidence hash. The index stores pointers, not rewritten truth. Before each request, a deterministic working-set packer budgets context in this order:

1. current objective, constraints, open items, and blockers;
2. exact current task capsule and last verified checkpoint;
3. relevant file/symbol excerpts and artifact hashes;
4. unresolved or unknown tool outcomes;
5. recent turn tail;
6. retrieved earlier events and validated compaction summaries.

The packer records exactly which sources entered the model request, making omissions diagnosable.

### 4. Two-phase compaction commit

Compaction never replaces or deletes history. It appends a derived checkpoint through this transaction:

1. Freeze the proposed source range by first/last event sequence and hash.
2. Ask a model for a typed candidate covering objective, constraints, decisions, completed/current/next work, blockers, evidence, artifact hashes, and open tool outcomes.
3. Deterministically reject a candidate if required fields are absent, cited events do not exist, artifact hashes mismatch, any active constraint/open item disappears, a completed claim lacks proof, or an unmatched side-effecting tool call is presented as completed.
4. Have an independent verifier compare the candidate with the source range for semantic omissions. One rejection returns to the prior valid state; it does not start an autonomous retry loop.
5. Append `compaction/committed` with the candidate hash, covered range, retained-tail boundary, validator result, and verifier identity. Keep the raw range forever.

Pi's default compaction already appends a `CompactionEntry`, preserves a recent tail, carries prior summaries forward, avoids splitting tool call/result pairs, and tracks read/modified files cumulatively. Its structured summary fields closely match the proposed ledger. However, Pi's documented flow accepts a model-generated summary after generation; the command center adds independent invariant, citation, open-item, and hash validation before commit.

### 5. Semantic checkpoints and replay evaluation

Flush before every model request, before a top-level side-effecting tool body, and before the next model step. On restart, verify the hash chain, rebuild the typed ledger, classify unmatched calls as `not started` or `outcome unknown`, recheck artifact hashes, and pack the next request from the last validated state. Never blindly replay a possibly completed side effect.

Run periodic frozen replay tests at realistic context pressure: stop mid-request, mid-tool, after edit-before-result, after test, and across multiple compactions; resume in a new process; then score retained constraints, open items, correct next action, artifact identity, and duplicate-effect avoidance. A continuity release must pass these tasks at the production context size before it replaces anything.

This design can provide effectively unbounded continuity because durable external state outlives any one context window. It cannot literally preserve every latent nuance of a prior model state. Retrieval can miss a dependency and summaries can lose meaning, so larger context remains valuable and the raw event log remains necessary.

## Adapter experiments after the memory core

### Pi lane

Pi is the first lightweight interactive candidate because its primary documentation confirms a small terminal core, native llama.cpp routing, JSONL tree sessions, RPC/JSON event output, and direct OpenAI-compatible provider registration. Run it only through an adapter that pins project trust, active tools, endpoint, model, turn/call budget, and stage directory. Give it one concise global instruction to verify unstable facts against primary external sources and expose one reviewed search provider when a task needs research. Pi has no built-in sandbox and its TypeScript extensions run with the user's full process permissions, so load no project-local, self-authored, or unreviewed extension in qualification or production; the agent never installs its own next extension.

### DeepSeek lane

DeepSeek remains the richer substrate candidate. Its adapter should use a pinned commit and custom lean headless profile, local provider route, telemetry disabled, model retry disabled, Ralph disabled, and the existing Scheduled Task lifecycle outside the harness. It competes against Pi on the same tasks; architecture elegance does not decide promotion.

### Long-context Qwen lane

Reddit packets claim that longer context can matter more than weight quantization for agentic tasks; those claims remain anecdotal. Local evidence is stronger: Q6 weights already allocated the native 262,144-token window with Q4 KV and MTP off, using 30,163 MiB after a short smoke. Therefore the required interactive baseline is Q6/native/Q4-KV/MTP-off, not a lower weight quant.

Do not change the Q6 32K bounded production lane. Qualify the full native Q6 lane at 50/75/90% prompt fill with loss-checked compaction; then test MTP, dynamic Q5/Q4, Blackwell NVFP4, and approximately 1.01M YaRN as challengers on the same tasks. Exact evidence, thresholds, RAM implications, and recent llama.cpp reliability risks are in [`LONG_CONTEXT_DECISION.md`](LONG_CONTEXT_DECISION.md).

### Knowledge grounding

Claims that Qwen3.8 has weaker parametric recall than Qwen3.6 are unverified until the frozen local suite separates closed-book recall, calibrated abstention, tool selection, and grounded synthesis. Prefer code/artifacts and primary official sources. Broad research runs in bounded child/subsessions and returns only cited findings to the orchestrator; Qwen3.6, Gemma, and local ZIM/FTS corpora are candidate specialist lanes, not assumed upgrades.

## Promotion order

1. Keep the current thin lane in production.
2. Add the append-only event and typed-state memory core beneath it.
3. Prove loss-checked compaction and fresh-process replay under the current 32K Q6 lane.
4. A/B Pi and DeepSeek adapters on the same bounded coding and continuity suite.
5. Qualify Q6 at native 262,144/Q4-KV/MTP-off; only then compare MTP and lower/dynamic weight quants without changing the production default.
6. Promote one interactive adapter only after repeated diagnosis, scoped edit, authoritative verification, resume, and compaction wins.
7. Add Claude-style parallel tools or subagent orchestration only for workloads that measurably benefit.

## Reject

- A source-level mega-merge of the full harnesses.
- Direct local-model writes to user work before staged proof and independent verification.
- UAC or approval prompts in unattended operation; denial must be machine-readable and elevation pre-owned.
- Default automatic model retries, unbounded Ralph/agent loops, or an 80-call activity target.
- Self-installing or hot-loaded extensions with user-account authority.
- Pi project trust treated as tool confinement; its own security documentation says it is not a sandbox.
- DeepSeek's default Windows ACL runner or default retry/Ralph settings in the production path.
- Claude's recovery behavior of filtering unresolved tool calls and injecting a generic continuation as the authoritative side-effect policy (`D:\src\utils\conversationRecovery.ts:139-247`). DeepSeek's explicit unknown-outcome classification is stronger.
- Claude's buffered JSONL append path as the strongest durability implementation: the inspected path batches `appendFile` writes and exposes a queue flush (`D:\src\utils\sessionStorage.ts:606-685,841-860`) but does not show the fsync/checksummed-frame semantics present in DeepSeek and the current checkpoint writer.
- Model-authored compaction committed without independent loss checks.
- Any claim that the result is 90–95% of frontier quality until the frozen representative suite demonstrates it.

## Stopping condition

The fusion design is complete when one shared task/event/state protocol drives the production thin lane plus at least one local interactive adapter; fresh-process replay and compaction preserve all required invariants on the frozen suite; Qwen implements and Devstral verifies without user prompts; and the operator can inspect current objective, next action, evidence, model route, and remaining budgets with one command. Until those facts are measured, this document is the architecture decision, not a production-readiness claim.
