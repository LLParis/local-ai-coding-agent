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
2. plans and performs scoped edits in an isolated stage;
3. runs real tests, builds, and static checks;
4. uses materially new failure evidence for at most two targeted repairs and
   stops on an identical failure;
5. sends Swift/Xcode verification to the trusted Mac edge when required;
6. independently reviews meaningful diffs and treats deterministic execution
   as final authority;
7. preserves user source until supported promotion, records durable Memory and
   truthful telemetry, cleans up exact children/stages, and reports the result.

One substantial real repository objective must complete through this exact path
before the product is called live-complete.

## Proven and live

| Layer | Current authority | Evidence |
|---|---|---|
| PC inference lifecycle | Bounded Qwen3.8 Q6, native 262K Qwen, and Ollama are mutually exclusive, loopback-only, exactly owned Limited Scheduled Tasks with rollback and no UAC | `bin/doctor.ps1`; lifecycle evidence under `runs/` |
| Incumbent local model | Qwen3.8-27B Q6 is the accepted local implementer substrate | `runs/qwen38-q6-native-long-context-qualification.json`: 60/60, 1,128/1,128 checks |
| Current production harness | Thin structured-edit path with isolated stage, one response, real test, Devstral review, backend restoration, and source preservation | `bin/coding-task.ps1`; `src/agent_continuity/local_edit.py`; Tournament v1 evidence |
| Local verifier | Devstral Small 2 is accepted for its independent verifier role | `runs/devstral-verifier.json`; `docs/TOURNAMENT_V1.md` |
| Durable continuity | Hash-chained event log, SQLite projection/search, typed task state, loss-checked compaction, retention, rehydration, and hourly Limited maintenance | commit `e13e998`; Memory proof artifacts under `runs/` |
| Research intake | Scoped, current, hash-pinned Research Radar task with bounded primary-source intake and rollback | commit `eedbd43`; `docs/RESEARCH_RADAR_V1.md` |
| PC/Mac control | Passwordless exact SSH edge, supervised Mac Codex host, full Xcode/Swift, and reversible task-catalog maintenance | Mac evidence under `runs/` and `mac/` |
| Routing substrate | Deterministic immutable routing manifest and verdict-integrity logic | commit `eedbd43`; `config/routing-v1.json` |

## Proven but not fully integrated

- Native Qwen's 262K profile is accepted, but the checked-in router still needs
  its new qualification evidence and final production dispatch update.
- The Swift/Mac execution verifier has a successful live zero-model proof and
  is awaiting final integration/commit in the standard coding command.
- The operator curriculum exists and validates, but must be reconciled after
  the final command and routing behavior are frozen.

## Working but limited

- The thin runner is the strongest production-proven harness today, but it is
  a one-response scoped edit rather than the complete autonomous
  inspect/edit/test/repair worker.
- The standard coding command lacks the completed bounded new-evidence repair
  loop and natural repository-objective entrypoint.
- The current router describes candidate harnesses but is not yet the everyday
  production dispatcher.

## Harness and model candidates without production authority

- Pi 0.84.2 is privately installed with a committed four-tool adapter and fake
  endpoint/process controls; its live Qwen gate is not yet accepted.
- DeepSeek Harness 0.1.0-rc.8 is privately installed. A four-tool event plugin
  is partially implemented but currently has failing focused tests and remains
  uncommitted/unqualified.
- Qwen Code is a high-value official, model-aligned harness challenger, but it
  is not installed or adapted in this repository.
- Laguna XS 2.1 Q4_K_M is checksum-verified locally, but runtime/template/tool
  and real-task qualification are not proven.
- Qwen Q8, Qwen3.6, gpt-oss, Gemma, Qwen3-Coder-Next, GLM-Flash, and SERA are
  references or challengers, not launch blockers.
- Tournament v2 is a parked post-live scientific asset. Its offline files must
  be internally consistent, but no extended campaign blocks launch.

## Exact remaining work to live completion

1. Finish and prove the pluggable autonomous inspect/edit/test loop with at
   most two materially new-evidence repair passes.
2. Finish and commit automatic Swift/Mac verification in the everyday coding
   command.
3. Integrate the strongest research-supported mechanisms from the thin runner,
   Pi, DeepSeek, Qwen Code, Codex, and Claude-style tool ordering directly
   behind the pluggable production harness; do not run a pre-launch harness
   tournament.
4. Complete one substantial real repository objective locally through the
   selected/fused path, including deterministic verification, Memory,
   restoration, cleanup, and source/promotion truth.
5. Update routing with current evidence, reconcile the operator guide, run the
   authoritative full suite/doctor/process/task audit, commit a clean tree, and
   stop the launch slice.

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
