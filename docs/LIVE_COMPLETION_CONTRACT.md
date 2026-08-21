# Coding Intelligence live-completion contract

This is the canonical answer to three questions:

1. What is already proven and live?
2. What is working but incomplete or merely a candidate?
3. What exact finite work remains before high-level local coding is live?

Current code and runtime evidence override this document when they disagree.
Update this contract as an item changes state; do not add post-live research to
the launch critical path.

## Product outcome

The everyday local command accepts a repository and objective, then under a
normal user account and without UAC:

1. inspects and searches the repository;
2. plans and performs scoped edits in an isolated stage under the DeepSeek
   Harness default loop driving the Qwen3.8-27B Q6 Native 262K backend;
3. runs real tests, builds, and static checks;
4. uses materially new failure evidence for at most two targeted repairs and
   stops on an identical failure;
5. sends Swift/Xcode verification to the trusted Mac edge when required;
6. sends meaningful diffs to Devstral exactly once as an advisory reviewer and
   treats deterministic execution as final authority;
7. applies a verified result to the source by default (`--stage-only` keeps it
   in the retained stage), records durable Memory including the effective
   requested scope and truthful telemetry, cleans up exact children/stages, and
   reports the result.

The first substantial real objective completed on 2026-08-20. Its original
terminal was corrected to `evaluator_invalid` because generated Python bytecode
was misclassified as a source edit; the model's 17 edits, two passing compiler
runs, source preservation, child exit 0, promotion, Memory reconciliation, and
backend restoration are recorded in `runs/deepseek-four-tool-production.json`.
The current integrated path then completed run
`e037d1fe-da95-4121-a9cc-e20bbcc51972`: evidence-bound routing selected Native
Qwen, continued a prior session into a fresh stage, used workspace-write
PowerShell, edited one scoped document, passed model and
independent verification, completed Devstral, applied source, finalized Memory,
and restored Native Qwen. A post-apply `.git` ACL cleanup error was reconciled;
staged Git metadata now lives outside the sandboxed workspace.
Its old compact representation did not fit under the per-memory token cap, so
that run injected zero episodes. Run `c72309f7-edc3-4fc9-9423-f2af7506e334`
then proved the repaired path: four relevant episodes/1,986 tokens were injected,
the prior DeepSeek lineage continued, two documentation edits verified and
applied transactionally, cleanup completed after the read-only Git-object root
fix, Memory was superseded with the reconciled outcome, and Native Qwen was
restored. Exact paired evidence is in `runs/live-foundation-completion.json`.

## Proven and live

| Layer | Current authority | Evidence |
|---|---|---|
| PC inference lifecycle | Bounded Qwen3.8 Q6, native 262K Qwen, and Ollama are mutually exclusive, loopback-only, exactly owned Limited Scheduled Tasks with rollback and no UAC | `bin/doctor.ps1`; lifecycle evidence under `runs/` |
| Incumbent local model | Qwen3.8-27B Q6 is the accepted local implementer substrate | `runs/qwen38-q6-native-long-context-qualification.json`: 60/60, 1,128/1,128 checks |
| Current production harness | Everyday `bin/coding.cmd` repository-objective command: DeepSeek Harness drives Qwen3.8-27B Q6 Native 262K through an inspect/edit/test loop in an isolated stage, runs the real project command, bounds repairs to two materially new failures, takes one advisory Devstral review, restores the backend, and applies a verified result by default | `bin/coding.cmd`; `src/agent_continuity/production_worker.py`; `runs/live-foundation-completion.json` |
| Local verifier | Devstral Small 2 is accepted for its independent verifier role | `runs/devstral-verifier.json`; `docs/TOURNAMENT_V1.md` |
| Durable continuity | Hash-chained event log, SQLite projection/search, typed task state, loss-checked compaction of complete claims plus cryptographic provenance under the per-memory token cap, retention, rehydration, and hourly Limited maintenance | commit `e13e998`; Memory proof artifacts under `runs/` |
| Research intake | Scoped, current, hash-pinned Research Radar task with bounded primary-source intake and rollback | commit `eedbd43`; `docs/RESEARCH_RADAR_V1.md` |
| PC/Mac control | Passwordless exact SSH edge, supervised Mac Codex host, full Xcode/Swift, and reversible task-catalog maintenance | Mac evidence under `runs/` and `mac/` |
| Routing substrate | Deterministic immutable routing manifest and verdict-integrity logic | commit `eedbd43`; `config/routing-v1.json` |
| Evidence-bound automatic routing | The routing manifest is connected to `bin/coding.cmd` as the automatic dispatcher; the router selects the accepted DeepSeek/Qwen native route by default and the Swift/Mac route for Swift objectives without operator invocation | `config/routing-v1.json`; `src/agent_continuity/production_routing.py` |
| Exact Native model alias | `Qwen38Native` is the backend for the frozen Q6/native-262K/Q4-KV/MTP-off profile and exposes exact alias `arm-qwen38-q6-native-262k`; the everyday loop switches to it without UAC | `src/agent_continuity/production_routing.py`; `windows/Switch-ExcaliburBackend.ps1` |
| Workspace Memory retrieval | The everyday loop retrieves exact-scope workspace Memory before model dispatch, packing typed state and prior episode evidence into the session context | `src/agent_continuity/production_memory.py`; Memory v1 canonical events |
| Fresh-stage session lineage | Each run creates a new session with current-stage `cwd`, seeds it from the latest balanced task-family history, and records its parent; stale deleted stage paths are never resumed | `src/agent_continuity/production_memory.py`; `adapters/deepseek/scoped_plugin.mjs` |
| Workspace-write PowerShell + diagnostics | Full-repository DeepSeek jobs get dedicated file tools plus workspace-write PowerShell for staged Git, builds, and diagnostics; focused `--scope` jobs use only dedicated scoped tools without PowerShell. Qwen Code's pinned `basedpyright`/`tsc` commands are on the diagnostic path | `adapters/deepseek/bounded_profile.patch.yml`; `adapters/deepseek/adapter.py` |
| Windows-safe verifier overrides | The everyday command accepts exact verifier argv directly or through a UTF-8 JSON file, avoiding batch-shell quote loss | `src/agent_continuity/production_worker.py` |
| Atomic promotion | Promotion writes an external backup, runs one apply, compares the manifest, rolls back on failure, and cleans up only after success; `--stage-only` retains the stage without touching the workspace | `src/agent_continuity/production_worker.py` |
| Automatic Swift-to-Mac execution | The everyday `bin/coding.cmd` command automatically detects Swift objectives and routes them to the Mac edge. Supported modes: Swift package test/build, one unambiguous shared-scheme Xcode build, and plain Swift typecheck. Live Swift-package evidence: run `404f6f14-c535-4f5d-ae4d-541f98b7a536`. Xcode and plain-Swift modes are source-supported but not yet live-proven until matching real projects exist | `config/routing-v1.json`; `runs/swift-mac-edge.json`; `runs/live-foundation-completion.json` |
| Mac catalog maintenance | The Mac Codex catalog-maintenance LaunchAgent is repaired and running; SQLite uses `mode=rw` plus `query_only=ON`, and the post-fix run exited 0 | `ops/codex_catalog_maintenance.py`; `runs/mac-codex-catalog-maintenance.json` |
| GitHub mobile documentation | README and operator curriculum are optimized for mobile reading and link to the GitHub repository for full reference | `README.md`; `docs/OPERATOR_CURRICULUM_V1.md` |
| 2026 research map | A concise mobile-readable map of the strongest applicable January–August 2026 agent research and exactly how this system applies or parks each finding | `docs/RESEARCH_TO_IMPLEMENTATION_2026.md` |

## Proven adjacent paths

- Native Qwen's 262K profile and the DeepSeek production composition are
  accepted in the checked-in router with exact evidence hashes.
- Swift/Mac execution is live through both the everyday `coding.cmd` command
  (automatic Swift objective detection routes to the Mac edge) and the
  task-file lane's explicit `mac-swift` verifier for exact capsules. The
  verified Qwen3.8/DeepSeek/Mac Swift repair run
  `404f6f14-c535-4f5d-ae4d-541f98b7a536` is live Swift-package evidence.
  Xcode shared-scheme build and plain-Swift typecheck are source-supported
  but not yet live-proven until matching real projects exercise them.
- Qwen Code and Pi remain installed mechanism donors/alternate adapters; they
  do not compete with the DeepSeek default during ordinary work. Qwen Code
  never claims DeepSeek session lineage.

## Working but limited

- The thin task-file runner remains available, but it is a one-response scoped
  edit and no longer the ordinary PC entrypoint. It remains the explicit path
  for exact capsules and Mac Swift verification.
- Automatic routing selects the sovereign local command by default; hosted
  Codex remains available for architecture and arbitration but is not the
  everyday path. The sovereign local command requires no hosted call.
- The Windows shell sandbox provides partial read confinement: the `pwsh`
  tool can read files outside the staged workspace in some paths, though
  write access is restricted to the workspace. Full read confinement is a
  post-live improvement.
- Qwen Code, Pi, and other models remain post-live alternates without
  production authority; they do not compete with the DeepSeek default.
  Qwen Code never claims DeepSeek session lineage.
- Xcode shared-scheme build and plain-Swift typecheck are source-supported
  but not yet live-proven until matching real projects exercise them.
- Two earlier research-map attempts ended without source edits; the later
  scoped live-contract run completed and is the current integrated evidence.

## Harness and model candidates without production authority

- Pi 0.84.2 is privately installed with a committed four-tool adapter and fake
  endpoint/process controls; its live Qwen gate is not yet accepted.
- Qwen Code is the model-aligned `--harness qwen-code` alternate behind the
  everyday command; DeepSeek Harness remains the production default.
- Laguna XS 2.1 Q4_K_M is checksum-verified locally, but runtime/template/tool
  and real-task qualification are not proven.
- Qwen Q8, Qwen3.6, gpt-oss, Gemma, Qwen3-Coder-Next, GLM-Flash, and SERA are
  references or challengers, not launch blockers.
- Tournament v2 is a parked post-live scientific asset. Its offline files must
  be internally consistent, but no extended campaign blocks launch.

## Exact remaining work to live completion

Zero launch-critical implementation items remain. Commit this production
state, then stop the launch slice. All comparative testing and portfolio
expansion below are post-live work.

## Post-live backlog

- Extended Excalibur Agent Bench and hosted-relative 90-95% measurement.
- Larger model and quant campaigns, including Qwen3-Coder-Next, GLM-Flash,
  SERA, Qwen Q8, and additional Laguna work.
- Fine-tuning, weight modification, learned routing, and architecture research.
- Larger Qwen Code/DeepSeek/Pi feature experiments and routed-team research.
- Harness/model comparison and all scientific testing beyond questions raised
  by actual live use.
- Academic publication, upstream contributions, and the broader AI Research
  Mastery program.
