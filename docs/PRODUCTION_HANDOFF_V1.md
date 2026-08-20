# Coding Intelligence Command Center — v1 production handoff

Status date: 2026-08-20

## Production-proven foundation

### EXCALIBUR inference lifecycle

- Ollama, bounded Qwen3.8 Q6, and native-context Qwen3.8 Q6 are current-user
  Scheduled Tasks with exact wrapper/child ownership, loopback-only listeners,
  Job Object teardown, rollback, mutual exclusion, and no UAC.
- Bounded profile: Q6 weights, 32,768 context, Q8 KV, medium reasoning capped
  at 2,048, MTP draft 3.
- Native profile: Q6 weights, 262,144 context, Q4 KV, one text slot, medium
  reasoning capped at 16,384, MTP off.
- The captured native lifecycle switched bounded → native in 20.007 seconds and
  native → bounded in 19.727 seconds. A later live scalar/array rollback defect
  was preserved, fixed, regression-tested, and re-proven in
  `runs/qwen-native-switch-repair.json`.

### Bounded coding path

- `bin\coding-task.cmd <task.json>` makes one implementation response, applies
  one to four scoped replacements only in a retained stage, runs the real
  verifier-only project command, and leaves the source workspace unchanged.
- A passing implementation receives one independent Devstral Small 2 verdict
  through Ollama's native structured-output endpoint.
- Every non-PlanOnly path restores bounded Qwen. Model calls, verifier calls,
  backend swaps, process exits, restore, and zero-retry telemetry remain
  separate.
- Python, TypeScript, PowerShell, and Swift fixes have passed real hidden tests.
  The Swift task used PC Qwen inference, macOS Xcode/Swift 6.3.3 verification,
  and Devstral review; exact evidence is in `runs/swift-mac-edge.json`.

### Durable memory and continuity

- Coding runs now append a canonical, fsynced, hash-chained event stream before
  model dispatch and commit typed task-state revisions with evidence.
- Large payloads are content-addressed. Active memory stores objective/state,
  hashes, and locators rather than copying prompts, responses, diffs, or test
  output.
- SQLite projections use WAL/FULL/FK/trusted-schema-off plus exact, unicode61,
  and trigram retrieval. Scope and temporal filters run before ranking.
- A deterministic packer places validated task state first, then at most eight
  provenance-bearing memories under exact token budgets.
- Compaction preserves raw history and requires invariant/citation/artifact
  checks plus an independently bound semantic-verifier report before immutable
  state commit.
- Cross-process Windows/macOS/Linux locks cover append, fsync, projection,
  verify, replay, and rebuild. Forced process death releases ownership.
- An unmatched model request remains `outcome_unknown`; it is never replayed or
  fabricated as completed.

### PC/Mac execution edge

- Proton's second-VPN filtering was repaired with official Tailscale-range
  exclusions; Tailscale, Proton, public internet, and local inference coexist.
- Passwordless native macOS Remote Login uses a dedicated ED25519 key.
- The PC Codex app has a saved auto-connected SSH host. The Mac LaunchAgent owns
  one official Codex app-server on a Unix socket; zero TCP app-server listeners
  exist. Forced child termination recovered in four seconds.
- Xcode is globally selected at `/Applications/Xcode.app/Contents/Developer`.
- Mac and PC share one terminal-child catalog-maintenance engine. Only
  `task_complete` subagents older than 24 hours can be archived; root, recent,
  non-terminal, malformed, or out-of-scope tasks are preserved. Every mutation
  uses supported `codex archive` and records `codex unarchive`; retries are zero.
- Mac active tasks fell 496 → 86 and task listing reached 0.084 seconds. PC
  active tasks fell 436 → 149, active subagents 317 → 30, active rollout bytes
  13.35 GB → 4.27 GB, and listing reached 0.113 seconds. Nothing was deleted.

## Evidence-based roles

| Role | Current system | Status |
|---|---|---|
| Hosted architect/escalation | Codex | Available while hosted usage exists |
| Bounded local implementer | Qwen3.8 27B Q6 | Production-proven on scoped tasks |
| Local verifier | Devstral Small 2 24B | Production-proven verdict lane |
| Factual authority | Hidden project tests/contracts | Required |
| Long-context baseline | Qwen3.8 Q6 / native 262K / Q4 KV / MTP off | Lifecycle-proven; final matrix running |
| High-fidelity reference | Qwen3.8 27B Q8 | Installed; matched qualification pending |
| Prior-generation control | Qwen3.6 27B Q6 | Installed; matched qualification pending |
| Reasoning/vision candidate | Gemma 4 31B QAT | Installed; not promoted |
| Fast small-task candidate | gpt-oss 20B | Installed; harder tranche 0/3 |
| Project-coherence challenger | Laguna XS 2.1 Q4_K_M | Official 20.27 GB artifact verified; runtime/tasks pending |

No model is removed or demoted from anecdotes, vendor benchmarks, stale tests,
or evaluator failures. Raw failures remain in the denominator only when the
candidate actually received a valid applicable trial.

## Harness status

- The current thin runner remains production authority.
- Pi 0.84.2 is pinned per-user and wrapped with only read, search, scoped edit,
  and test; zero retries, no session/plugin discovery, bounded turns/calls,
  protocol-clean NDJSON, source preservation, and child-tree cleanup are proven
  against fake endpoints. Live Qwen qualification is pending.
- DeepSeek Harness 0.1.0-rc.8 is installed but blocked from live qualification.
  Its current headless mode exposes final text without a stable live event/tool
  interception seam and mounts broad capabilities. A reviewed four-tool plugin
  must earn entry.

## Everyday operation

1. Run the read-only status command:

   ```powershell
   bin\doctor.cmd
   ```

2. Create a bounded task from `task.example.json`.
3. Validate without model/backend/memory mutation:

   ```powershell
   powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
     -File bin\coding-task.ps1 -Task .\my-task.json -PlanOnly
   ```

4. Run:

   ```powershell
   bin\coding-task.cmd .\my-task.json
   ```

A `verified` result means the staged patch passed its named project evidence,
Devstral accepted it, bounded Qwen was restored, and terminal memory committed.
It does not automatically modify or promote the source workspace.

## Current configured state

- Bounded task: `Coding Intelligence Excalibur Qwen3.8`
- Native task: `Coding Intelligence Excalibur Qwen3.8 Native`
- Ollama task: `AnimeFrontier Excalibur Ollama` (legacy name retained)
- Catalog maintenance: `Coding Intelligence - Codex Catalog Maintenance`
- Qwen endpoint: `127.0.0.1:8818`
- Ollama endpoint: `127.0.0.1:11434`
- Mac host alias: `coding-intelligence-mac`
- Operational memory root: `%LOCALAPPDATA%\CodingIntelligence\MemoryV1`
- All raw inference endpoints remain loopback-only.

## Current validation

- Repository suite: 98 tests passed with one expected macOS-only skip after the
  final evaluator-ownership correction.
- Ruff, PowerShell parsers, JSON parsing, Mac shell parsing, and diff checks pass.
- The final frozen 60-run native-context matrix is active under evaluator commit
  `7b4d3e2`; interim matrices remain evaluator-debug evidence, not model losses.

## Not yet production-proven

- Completion of the final 60-run native-context matrix.
- Live Pi/Qwen results and a Pi-versus-thin-lane decision.
- Laguna runtime/template/tool compatibility and matched coding/coherence tasks.
- Final Q8/Qwen3.6/Gemma/gpt-oss and KV/MTP/DFlash/NVFP4 comparisons.
- A measured 90% or 95% frontier-equivalence rate.
- Automatic promotion into real user work.
- Fine-tuned local adapters or new weights.
- Continuous arXiv Research Radar ingestion; its architecture is decided in
  `docs/RESEARCH_RADAR_DECISION.md`.

## Exact continuation

1. Let the final native-context matrix finish; preserve every terminal result.
2. Restore bounded Qwen.
3. Run Pi and the current thin lane on the same frozen path defect.
4. Prove Laguna runtime compatibility, then run matched coding and project-
   coherence tasks.
5. Freeze routing roles from measured outcomes.
6. Update the operator guide and learning curriculum; rerun the full suite and
   doctor before declaring v1 complete.
