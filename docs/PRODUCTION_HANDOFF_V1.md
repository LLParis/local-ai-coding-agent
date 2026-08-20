# Coding Intelligence Command Center — v1 production handoff

Status date: 2026-08-20

## Production-proven on EXCALIBUR

- Loopback Ollama Scheduled Task with exact wrapper/child ownership and model
  preservation.
- Loopback Qwen3.8 Q6 Scheduled Task with exact wrapper/child ownership, 32K
  context, Q8 KV cache, medium reasoning capped at 2,048, and MTP draft 3.
- Automatic Qwen/Ollama swap with no UAC: 8.12 seconds to Ollama and 14.69
  seconds back to Qwen in the captured round trip.
- Windows thin edit lane: one model call, zero automatic retries, explicit
  mutable/context/verifier paths, isolated stage, real project command, and
  source workspace unchanged.
- Immutable SHA-256 task capsule created in one process and recovered by a
  second fresh process with the same workspace identity.
- One everyday entry point: `bin\coding-task.cmd <task.json>`.
- Trajectory record retained inside every isolated stage.
- Fourteen focused platform tests pass; one Mac-only tunnel test is skipped on
  Windows by design. Frozen qualification cases pass after promoted fixes.

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

3. The command switches the selected owned backend, makes one model request,
   stages the patch, runs the verifier command, and returns one JSON report.
4. Promote a staged diff only after tests pass and the configured verifier
   accepts it. There is no automatic retry or direct mutation of source work.

## Current configured state

- Qwen task: `Coding Intelligence Excalibur Qwen3.8`
- Ollama task: `AnimeFrontier Excalibur Ollama` (legacy name retained for the
  already-proven runtime; rename is cosmetic debt)
- Qwen endpoint: `127.0.0.1:8818`
- Ollama endpoint: `127.0.0.1:11434`
- Model artifacts installed: Qwen3.8 Q6/Q8, gpt-oss-20b, Gemma 4 31B QAT,
  Devstral Small 2, and prior Qwen3.6.
- Raw endpoints remain loopback-only.

## Not yet production-proven

- The final Mac/Swift execution-edge task. The Mac remains online in Tailscale,
  but its Codex host/app server and inbound SSH are currently unavailable from
  this PC. No claim of Apple-edge completion is made.
- A 90% or 95% frontier-equivalence rate. The current sample is deliberately
  small and contains failures.
- Automatic patch promotion into real user work.
- Fine-tuned weights or adapters; no training dataset is yet large or clean
  enough to justify them.

## Exact next step when the Mac reconnects

Run one frozen Swift package defect through PC Qwen, execute `swift test` on the
Mac stage, return the diff/test/trajectory, and have Devstral verify the patch.
Do not reopen the completed Windows lifecycle or model-selection work.
