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

## Proven and live

| Layer | Current authority | Evidence |
|---|---|---|
| PC inference lifecycle | Bounded Qwen3.8 Q6, native 262K Qwen, and Ollama are mutually exclusive, loopback-only, exactly owned Limited Scheduled Tasks with rollback and no UAC | `bin/doctor.ps1`; lifecycle evidence under `runs/` |
| Incumbent local model | Qwen3.8-27B Q6 is the accepted local implementer substrate | `runs/qwen38-q6-native-long-context-qualification.json`: 60/60, 1,128/1,128 checks |
| Current production harness | Everyday `bin/coding.cmd` repository-objective command: DeepSeek Harness drives Qwen3.8-27B Q6 Native 262K through an inspect/edit/test loop in an isolated stage, runs the real project command, bounds repairs to two materially new failures, takes one advisory Devstral review, restores the backend, and applies a verified result by default | `bin/coding.cmd`; `src/agent_continuity/production_worker.py`; `runs/deepseek-four-tool-production.json` |
| Local verifier | Devstral Small 2 is accepted for its independent verifier role | `runs/devstral-verifier.json`; `docs/TOURNAMENT_V1.md` |
| Durable continuity | Hash-chained event log, SQLite projection/search, typed task state, loss-checked compaction, retention, rehydration, and hourly Limited maintenance | commit `e13e998`; Memory proof artifacts under `runs/` |
| Research intake | Scoped, current, hash-pinned Research Radar task with bounded primary-source intake and rollback | commit `eedbd43`; `docs/RESEARCH_RADAR_V1.md` |
| PC/Mac control | Passwordless exact SSH edge, supervised Mac Codex host, full Xcode/Swift, and reversible task-catalog maintenance | Mac evidence under `runs/` and `mac/` |
| Routing substrate | Deterministic immutable routing manifest and verdict-integrity logic | commit `eedbd43`; `config/routing-v1.json` |

## Proven adjacent paths

- Native Qwen's 262K profile and the DeepSeek production composition are
  accepted in the checked-in router with exact evidence hashes.
- Swift/Mac execution is live through the task-file lane's `mac-swift`
  verifier. Automatic language detection from the plain `coding.cmd` command
  is a post-live convenience, not a missing execution path.
- Qwen Code and Pi remain installed mechanism donors/alternate adapters; they
  do not compete with the DeepSeek default during ordinary work.

## Working but limited

- The thin task-file runner remains available, but it is a one-response scoped
  edit and no longer the ordinary PC entrypoint. It remains the explicit path
  for exact capsules and Mac Swift verification.
- Hosted-to-local continuity selection is still operator-invoked; the
  sovereign local command itself is live and requires no hosted call.

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

Zero launch-critical implementation items remain. Commit this production state
and stop the launch slice. All comparative testing and portfolio expansion
below are post-live work.

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
