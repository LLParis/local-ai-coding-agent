# Coding Intelligence Command Center — v1 production handoff

Status date: 2026-08-20

## Production-proven on EXCALIBUR

- Loopback Ollama Scheduled Task with exact wrapper/child ownership and model
  preservation.
- Loopback Qwen3.8 Q6 Scheduled Task with exact wrapper/child ownership, 32K
  context, Q8 KV cache, medium reasoning capped at 2,048, and MTP draft 3.
- Automatic Qwen/Ollama swap with no UAC: 8.12 seconds to Ollama and 14.69
  seconds back to Qwen in the captured round trip.
- Windows thin edit lane: one implementation call, zero automatic retries,
  explicit mutable/context/verifier paths, isolated stage, real project
  command, and source workspace unchanged.
- Live everyday two-model command: Qwen diagnosed and fixed the frozen
  PowerShell process-ownership defect in 4.249 seconds, the hidden test passed,
  Devstral accepted the staged diff in 8.44 seconds through Ollama's native
  structured endpoint, and Qwen was restored. The source fixture SHA-256 stayed
  identical, the final process exit was zero, and no UAC or hosted model call
  occurred. Exact evidence is in `runs/final-end-to-end.json`.
- Immutable SHA-256 task capsule created in one process and recovered by a
  second fresh process with the same workspace identity.
- One everyday entry point: `bin\coding-task.cmd <task.json>`.
- Trajectory record retained inside every isolated stage.
- Twenty focused platform tests pass; one Mac-only tunnel test is skipped on
  Windows by design. Frozen qualification cases pass after promoted fixes.

## Independent verifier path

- The everyday command now requires two independent results after a successful
  staged edit: the authoritative project test and one strict Devstral Small 2
  accept/reject verdict over only the objective, scoped diff, and test result.
- Every non-PlanOnly path restores Qwen as the idle backend. Accepted, rejected,
  malformed, and restore outcomes remain separate telemetry; no retry or
  promotion is added.
- Twenty focused tests pass with one Mac-only skip, including fake-endpoint
  proofs of one verifier call, Unicode-safe transport, strict runtime
  rejection, restore, and PlanOnly zero-call behavior.
- The initial OpenAI-compatible verifier route returned HTTP 400 twice before
  Devstral inference. It was removed, not retried again. The production command
  now uses Ollama's native `/api/chat` structured-output route. Both failed
  attempts and the subsequent accepted path remain under `runs/`.

## Evidence-based model roles

| Role | Model/system | Evidence |
|---|---|---|
| Architect and final arbiter | Hosted Codex | Current frontier operator |
| Local implementer | Qwen3.8 27B Q6 | 2/3 core tasks; verified Python, TypeScript, and corrected PowerShell edits |
| Local verifier | Devstral Small 2 24B | 3/3 verifier cases, including one real Qwen patch |
| Factual acceptance | Project tests/contracts | Deterministic, hidden from implementer |
| Research/vision candidate | Gemma 4 31B QAT | 1/3 implementation; not promoted as verifier |
| Tiny-task fallback | gpt-oss-20b | Historical tiny task passed; harder tranche 0/3 |

Raw qualification records are in `runs/`. Failed trials were not retried and
were not converted into successes.

## Everyday operation

1. Copy `task.example.json` and fill in one bounded objective, workspace,
   mutable files, visible context, verifier-only context, and real test command.
2. Run:

   ```powershell
   bin\coding-task.cmd D:\path\to\task.json
   ```

3. The command makes one implementation request, stages the patch, and runs the
   hidden project test. On test success it switches to Ollama and makes exactly
   one Devstral verifier request. Every non-PlanOnly outcome restores Qwen as the
   idle default.
4. A successful report requires both the test and strict verifier acceptance.
   Rejection or malformed verifier output fails truthfully. There is no retry,
   automatic promotion, or direct mutation of source work.

## Current configured state

- Qwen task: `Coding Intelligence Excalibur Qwen3.8`
- Ollama task: `AnimeFrontier Excalibur Ollama` (legacy name retained for the
  already-proven runtime; rename is cosmetic debt)
- Qwen endpoint: `127.0.0.1:8818`
- Ollama endpoint: `127.0.0.1:11434`
- Model artifacts installed: Qwen3.8 Q6/Q8, gpt-oss-20b, Gemma 4 31B QAT,
  Devstral Small 2, and prior Qwen3.6.
- Raw endpoints remain loopback-only.

## Architecture decisions, not yet runtime claims

- DeepSeek Harness is the pinned future rich-session adapter substrate; Pi is
  the lightweight interactive adapter candidate. Neither is installed in the
  production path. See `docs/HARNESS_FUSION_DECISION.md`.
- Memory v1 is specified as hash-chained local events plus rebuildable SQLite
  FTS5 projections, typed state, temporal supersession, provenance, and
  loss-checked compaction. That memory plane is designed but not implemented or
  production-proven. See `docs/MEMORY_ARCHITECTURE_DECISION.md`.

## Not yet production-proven

- The final Mac/Swift execution-edge task. The Mac remains online in Tailscale,
  but its Codex host/app server and inbound SSH are currently unavailable from
  this PC. A bounded Taildrop transfer attempt returned HTTP 502. The complete
  frozen Swift package is committed under `evals/cross_language/swift/`; no
  claim of Apple-edge completion is made.
- A 90% or 95% frontier-equivalence rate. The current sample is deliberately
  small and contains failures.
- Automatic patch promotion into real user work.
- Fine-tuned weights or adapters; no training dataset is yet large or clean
  enough to justify them.

## Exact next step when the Mac reconnects

Run one frozen Swift package defect through PC Qwen, execute `swift test` on the
Mac stage, return the diff/test/trajectory, and have Devstral verify the patch.
Do not reopen the completed Windows lifecycle or model-selection work.
