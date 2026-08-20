# Research-to-architecture synthesis

Date: 2026-08-20

This synthesis preserves the useful content from the six user research packets
without treating video claims as verified facts.

## Source packets

- Local-model and harness packet: SHA-256
  `7418b40564aa2812fdeeb3e71fc6d8d42477dfe73ae6d79de8d2f07c80a36392`
- Open-weight and small-model orchestration packet: SHA-256
  `0abd37f7f04c53c78c1d144063fbd87e9e76ee29a971632772284aadb81d263f`
- Qwen3.8/Pi agentic-use packet: SHA-256
  `d6a39006e627cf31cf79347bb97a7e39dcfab034ad989460812406c1cb0185b3`
- Mem0 architecture packet: SHA-256
  `112452dabdbd2bd02ec39c8c80a8c9f26b83c738a7892a7d631f1ce9bbd0e887`
- Memory-layer comparison packet: SHA-256
  `5fa7a8b0068f5e9b6f7c08f786ef2425071c7c4eb2f5bd1f837c6b8ec6a85e26`
- Hindsight/Obsidian/retention packet: SHA-256
  `cdc0986939c8cec5074ac5095ae91e06bc8f5e9e100494837c572828dbfc85f5`

## Adopted now

1. **Role-specialized orchestration.** Hosted Codex architects; Qwen implements;
   Devstral verifies; deterministic tests decide factual acceptance. Models
   exchange evidence through bounded task capsules and trajectories.
2. **One hot model.** EXCALIBUR swaps owned backends instead of overcommitting
   32 GB VRAM. The round trip was measured with no UAC.
3. **Task-conditional reasoning limits.** Qwen structured-edit reasoning is now
   capped at 2,048 tokens. The earlier unlimited setting reproduced the packet's
   warned-about failure: completion budget consumed with no final answer.
4. **MTP speculative decoding.** Qwen remains on MTP draft 3; local metrics
   showed roughly 145 generated tokens/second on the qualification workload.
5. **Trajectory observability.** Every thin-lane run retains the prompt,
   request, raw response, hashes, scopes, timing, candidate, diff, test command,
   and result in the isolated stage.
6. **Local 80/20 operating model.** Local handles routine bounded work without
   quota or recurring inference cost. Hosted frontier remains the architect and
   escalation path for novel, ambiguous, or high-judgment work.
7. **Open-weight specialization.** Future adapters/weight changes must target a
   measured failure family and beat the frozen baseline before promotion.
8. **Small protocol, replaceable interactive harness.** The current thin lane
   remains production authority. Pi and pinned DeepSeek headless are A/B
   adapter candidates; Claude contributes selected scheduling, cancellation,
   subagent-ownership, and headless-I/O mechanisms. See
   [`HARNESS_FUSION_DECISION.md`](HARNESS_FUSION_DECISION.md).
9. **External, loss-checked continuity.** Active context is a working set, not
   durable memory. The chosen memory design uses hash-chained local events,
   SQLite FTS5 projections, typed state, temporal supersession, provenance, and
   independently checked compaction. Mem0, vectors, entities, and graphs remain
   optional future candidates. See
   [`MEMORY_ARCHITECTURE_DECISION.md`](MEMORY_ARCHITECTURE_DECISION.md).
10. **Tiered retention.** Compact event hashes, task invariants, provenance, and
    protected evidence survive; scratch and bulky model/tool payloads have
    explicit TTL/archive classes and deterministic GC. Storage is finite, and
    GC may never erase active state or evidence supporting a production claim.

## Evaluated, but not promoted

- Gemma 4 31B QAT Q4_0: fully GPU-resident at 32K; 1/3 implementation tasks.
  It passed a simple verifier calibration but rejected a correct real
  TypeScript patch, so it is not the v1 verifier.
- gpt-oss-20b: fast on a tiny historical task, then 0/3 on the harder tranche.
- Devstral Small 2: 1/3 as implementer, but 3/3 as verifier, including a real
  Qwen TypeScript patch. It is promoted only for verification.
- DeepSeek Harness is an official `deepseek-ai` project. Its pinned
  `0.1.0-rc.8` source was audited at commit
  `141eb6fef83422698aef7a981029e843e8161534`. Event-sourced continuity,
  semantic checkpoints, headless execution, and replaceable provider/tool seams
  are adopted as architecture contracts. The current thin lane remains the
  production runner; a pinned DeepSeek headless runtime is an optional future
  adapter, not an installed dependency. See
  [`DEEPSEEK_HARNESS_AUDIT.md`](DEEPSEEK_HARNESS_AUDIT.md).
- Hindsight is a credible future memory adapter and benchmark candidate. Its
  retain/recall/reflect API, isolated banks, evidence-backed observations,
  local Ollama path, and derived Knowledge Pages warrant a frozen A/B test. It
  is not installed, canonical storage, or authorized for automatic per-turn
  retention/recall or nightly reflection.
- Obsidian Headless is an open-beta client for paid Obsidian Sync, not a
  sovereign database. It is not installed or required. A regenerable local
  Markdown export may be offered later as an optional operator view.

## Test later

- SGLang/NVFP4 only if a controlled Qwen lane beats the current llama.cpp
  latency, reliability, and memory result on the same tasks.
- Qwen Coder Next or other CPU-offloaded MoE models only if end outcomes improve
  enough to justify slower memory streaming.
- Small language specialists for instruction discovery, test selection,
  language-specific linting, visual review, and watchdog diagnosis.
- Adapter fine-tuning or distillation after the trajectory store contains a
  meaningful set of repeated, independently classified failure examples.
- Hindsight as a read-only derived-memory adapter after local JSONL/SQLite v1
  passes its hard replay, scope, provenance, compaction, and retention gates.
- A plugin API after two genuinely different consumers need it; no self-writing
  plugin or UI mechanism enters the execution path merely because it is novel.

## Rejected or unverified claims

- No general 90–95% frontier-equivalence claim is made from videos, vendor
  benchmarks, or this small tranche.
- Claims of a specific basic-harness to 88–90% SWE-bench jump, thousands of
  agents delivering 80% of enterprise sprints, or a particular repository being
  the fastest-growing ever remain unverified until primary artifacts reproduce
  them.
- "Obliterated" or nominally uncensored weights are not upgrades when they loop,
  degrade tool contracts, or reduce verified task completion.
- Hundreds of agents are not a default. Add one role only when it owns a distinct
  evidence-producing function and improves a measured outcome.
- Unlimited local storage or compute does not remove finite context, attention,
  retrieval, tool-contract, or compaction failure. Models do not feel compute
  anxiety; measure skipped steps and premature stopping as outcomes instead.
- Knowledge Pages and Markdown notes are derived views, not source-of-truth
  substitutes for raw events, hashes, provenance, and verified task state.

## Primary references checked

- Qwen3.8: https://huggingface.co/Qwen/Qwen3.8-27B
- Gemma 4: https://huggingface.co/google/gemma-4-31B
- Gemma QAT GGUF: https://huggingface.co/google/gemma-4-31B-it-qat-q4_0-gguf
- Devstral Small 2: https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512
- Devstral Ollama package: https://ollama.com/library/devstral-small-2
- Better Harnesses, Smaller Models: https://arxiv.org/abs/2607.08938
- DeepSeek Harness (pinned audit):
  https://github.com/deepseek-ai/deepseek-harness/tree/141eb6fef83422698aef7a981029e843e8161534
- Pi documentation: https://pi.dev/docs/latest
- Mem0 OSS v3 (pinned audit):
  https://github.com/mem0ai/mem0/tree/ed38ddf8731fb7fab7c41bb0f8ab3185df23e7c5
- Hindsight (pinned source):
  https://github.com/vectorize-io/hindsight/tree/e20bb290f795dd83e158a18ad90a2a45ced8a5d9
- Hindsight 0.9.0 / Knowledge Pages:
  https://hindsight.vectorize.io/blog/2026/08/06/hindsight-0-9-0
- Obsidian Headless Sync: https://obsidian.md/help/sync/headless
- Obsidian pricing: https://obsidian.md/pricing

## Measurable quality target

The stretch target is at least 90% independently accepted outcomes on a frozen,
representative workflow suite. It is not yet achieved. The suite must include
multiple trials, unfamiliar repositories, Python, TypeScript, PowerShell,
Swift, resume, malformed outputs, and real build/test diagnosis. Failed trials
remain in the denominator.
