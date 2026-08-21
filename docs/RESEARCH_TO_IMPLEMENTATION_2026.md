# 2026 research-to-implementation map

Status: applied to the live foundation on 2026-08-20. This is a mobile-readable
decision map, not a benchmark or a claim that every paper is correct.

## What changed the production system

| 2026 work | Strongest applicable finding | Decision here |
|---|---|---|
| [Agentic Harness Engineering](https://arxiv.org/abs/2604.25850) | Tools, middleware, long-term memory, and observable trajectory decisions transfer better than prompt churn. | Applied: typed tool events, pre-effect identity, durable trajectories, outcome reconciliation, and replaceable harness components. |
| [Code as Agent Harness](https://arxiv.org/abs/2605.18747) | Code and executable feedback are the shared substrate for reasoning, action, verification, and coordination. | Applied: one staged repository, dedicated file tools, confined PowerShell, repository-native verification, Mac execution, and deterministic results as final authority. |
| [CompactionRL](https://arxiv.org/abs/2607.05378) | Long-horizon coding improves when the model learns to compact trajectories rather than merely receiving a larger window. | Parked for post-live training. The current system first uses DeepSeek event compaction and preserves raw history; a future local training lane should target compaction skill before a language-specific Swift LoRA. |
| [MemoHarness](https://arxiv.org/abs/2607.14159) | Per-case experience plus distilled global patterns can adapt a harness without test-time search. | Applied at the useful floor: each completed run becomes a provenance-bearing workspace episode, and the next run retrieves relevant episodes under a fixed token budget. Learned harness mutation remains post-live. |
| [Agentic Context Management](https://arxiv.org/abs/2607.23809) | Offload context into external memory and retrieve it on demand instead of forcing all history into every prompt. | Applied: workspace-scoped just-in-time Memory retrieval, typed current task state, stable task streams, and bounded injection marked as evidence rather than instructions. |
| [TRACE: Reliable Context Compression](https://arxiv.org/abs/2608.06503) | Compression can weaken recent constraints and destabilize later actions; compression itself needs behavioral evidence. | Applied structurally: immutable raw events remain the source of truth, compacted state never deletes them, invalid evaluators are corrected rather than counted as model failures, and future compaction tuning stays outcome-local. |
| [Harness the Memory](https://arxiv.org/abs/2608.15008) | No memory substrate wins in every regime, and excessive retrieval can distract a sequential decision-maker from action-critical context. | Applied: state is packed first; retrieval is exact-workspace scoped, relevance-ranked, provenance-bearing, and capped at eight records/4,096 tokens. Vector, graph, or learned substrate routing waits for a real failure that the current sparse/exact channels cannot solve. |
| [Qwen3-Coder-Next](https://arxiv.org/abs/2603.00729) | New open coding models increasingly co-design model capability with agent scaffolds. | Retained as a post-foundation model candidate. It does not interrupt the current Qwen3.8-27B Q6 + DeepSeek production path. |

## Current architecture produced by the synthesis

```text
repository + objective
        |
evidence-bound local router
        |
workspace Memory retrieval + fresh-stage DeepSeek continuation
        |
Qwen3.8-27B Q6 Native 262K
        |
list / read / search / edit / confined pwsh / test
        |
PC project command or automatic Mac Swift/Xcode execution
        |
Devstral advisory review + deterministic apply + durable episode
```

The DeepSeek session lineage is a family, not a stale-path resume. Each run
creates a fresh session whose immutable `cwd` is the new stage, seeds it from
the latest balanced family history, and records the parent. This preserves
continuity after old stages are deleted while keeping current files
authoritative.

## Explicitly not adopted yet

- No Swift LoRA: the base Qwen model already passed the real Mac Swift path.
  Training becomes justified only after repeated real Swift failures survive
  compiler feedback, retrieved context, and tool improvements.
- No automatic harness self-rewrite: real trajectory outcomes must first show
  which component is limiting performance.
- No forced multi-model vote on every task: Qwen/DeepSeek implements, the real
  toolchain decides, Devstral reviews, and specialist routes activate only when
  the task requires them.
- No benchmark blocks ordinary use. Tournament V2, larger quants, Laguna,
  Gemma vision, gpt-oss fast routing, and post-training are post-live work.

## Research operating rule

Research enters through primary papers, official implementations, and current
deployment evidence. A paper supplies a mechanism hypothesis; the next real
task supplies the outcome. We adopt useful mechanisms directly, record mixed
or invalid outcomes truthfully, and never rebuild the product around a paper's
headline alone.
