# Research Radar and governed autoresearch decision

Date: 2026-08-20

## Outcome

Build a local-first, version-aware Research Radar that converts papers and
verified code into auditable experiment candidates. No feed, paper, benchmark,
or model may implement itself. Discovery, evidence extraction, decision, local
experiment, and promotion remain separate stages.

```text
community feeds + arXiv + code repositories
                    |
                    v
      canonical identity and version lineage
                    |
                    v
     relevance / evidence / feasibility scores
                    |
                    v
        operator decision and fixed experiment
                    |
                    v
 independent result -> adopt / defer / reject
                    |
                    v
        durable memory and learning curriculum
```

## VoltAgent collection boundary

The live source audited was
[`VoltAgent/awesome-ai-agent-papers@29037d57`](https://github.com/VoltAgent/awesome-ai-agent-papers/commit/29037d57d7d7aa025e12753ca862d2effd395bcb).
It is useful as one discovery feed, not as evidence or an ingestion substrate.

Observed state:

- 381 unique arXiv rows across five coarse sections;
- every displayed section count is stale;
- 103 of 363 version-pinned links lag the current arXiv version;
- 18 links float without a version;
- 56 displayed titles differ from current arXiv titles;
- two badges display a neighboring paper ID;
- two 2025 papers violate the stated January-2026-forward scope;
- zero rows link paper code;
- no structured data, ingestion code, tests, CI, dedupe, version monitor, claim
  extraction, or automation;
- MIT covers the collection, not the papers or their code.

The Radar records the collection commit and content hash as a
`source_observation`, then resolves every candidate against primary arXiv and
code sources.

## Canonical records

### Paper

- versionless arXiv ID as the primary identity;
- DOI/OpenReview aliases;
- withdrawal, replacement, and cross-list status.

### Paper version

- immutable `vN`, title, authors, submitted/revised dates, categories;
- abstract, license URI, Atom/PDF/source hashes;
- explicit lineage to prior versions.

### Code artifact

- normalized repository identity and every observed URL/redirect/fork;
- verified project/author linkage;
- commit/tag, license, archive hash, dependency/runtime claims;
- code is never cloned or executed during metadata intake.

### Claim

- exact paper section/page/table/figure;
- intervention, baseline, dataset, metric, repetitions/variance, compute;
- limitations, contradictory evidence, code/data availability.

### Triage

- research axes and named local failure addressed;
- applicability, evidence quality, feasibility, and recency as separate scores;
- `implement_now`, `experiment`, `defer`, `watch`, or `reject`;
- reason, evidence, owner, and revisit trigger.

### Experiment and decision

- immutable baseline/challenger artifacts and hashes;
- exact harness, prompt/template, tools, context, seeds, budgets, metrics, and
  hard regressions;
- evaluator validation evidence;
- result linked to commit, trajectories, logs, and independent verifier;
- operator/verifier identity and `adopted`, `failed`, `deferred`, `rejected`,
  or `superseded` decision.

## Deduplication and versions

- Base arXiv ID is canonical; every `vN` remains immutable.
- DOI equality may join aliases.
- Normalized title/author/year similarity creates a review candidate, never an
  automatic merge.
- Cross-lists and collection copies become source observations, not papers.
- A new version never overwrites a claim. It supersedes or invalidates the old
  claim explicitly.
- GitHub redirects/forks normalize to one project while retaining observed
  URLs and commit identities.

## Relevance and evidence scoring

Applicability:

- matches an observed failure or active research question;
- intervention fits a mutable platform layer;
- benchmark/task matches real Coding Intelligence work;
- adds something not already covered by current decisions.

Evidence:

- paper/venue status and version;
- controls, repetitions, variance, ablations, and holdout quality;
- code/data availability and exact artifact identity;
- independent reproduction or contradictory results.

Feasibility:

- RTX 5090 32.6 GB VRAM and 96 GB DDR5 capacity;
- Windows/runtime/tool-parser support;
- license and provenance;
- expected wall time, GPU hours, tokens, storage, and dependency risk.

Recency is displayed independently. A new weak preprint never outranks a strong
foundation merely for being new.

## Lifecycle

```text
new -> metadata_verified -> screened -> evidence_extracted
    -> decision_required
    -> experiment_candidate | watch | deferred | rejected
    -> reproduced | failed
    -> adopted | superseded
```

Rejection is durable and searchable. It requires a reason and revisit trigger.
An invalid evaluator yields `evaluator_invalid`, not rejection.

## Resource and trust policy

- Query the official arXiv API once daily using cached overlapping date
  windows, exact-ID dedupe, and at least three seconds between paginated calls.
- Recheck known paper versions weekly.
- Metadata and abstracts use no GPU.
- Fetch full text only for a bounded high-ranked queue.
- Treat PDF/TeX/repositories/prompts/install instructions as untrusted input.
- No code execution during intake.
- Every experiment declares `max_wall_minutes`, `max_gpu_hours`,
  `max_vram_mb`, `max_trials`, `max_tokens`, `max_storage_bytes`, and
  `max_cost_usd`.
- One GPU owner at a time. Read-only research and criticism may overlap.
- Nothing moves from paper to implementation without an explicit experiment
  capsule and operator-approved disposition.

## Mini-expert research rule

The best single expert is mandatory in every comparison. A team/router is not
an improvement merely because its average beats an average baseline.

- Compare best individual expert, explicit routed specialists, free-form team,
  and one agent with skills.
- Measure task correctness, context supplied, tool/schema accuracy, calls,
  tokens, latency, conflicts, duplicate effects, and final verification.
- Route sparsely. Do not ask every expert to deliberate on every task.
- Preserve specialist identity and individual results so orchestration cannot
  hide a stronger expert.

This rule is motivated by
[Multi-Agent Teams Hold Experts Back](https://arxiv.org/abs/2602.01011) and
[When Single-Agent with Skills Replace Multi-Agent Systems and When They Fail](https://arxiv.org/abs/2601.04748).

## Governed autoresearch

The audited upstream is
[`karpathy/autoresearch@228791fb`](https://github.com/karpathy/autoresearch/commit/228791fb499afffb54b46200aca536f79142f117).
It is a compact single-agent/single-GPU training protocol, not a coding harness
or implemented swarm.

Adopt:

- one small mutable surface;
- immutable evaluator and baseline-first run;
- fixed wall-clock budget;
- metric-driven keep/discard;
- commit-linked experiments and a simple ledger.

Strengthen:

- isolated worktree per candidate instead of destructive reset;
- append-only event/evidence records and preserved failed code;
- three-run confirmation for apparent wins;
- hidden final holdout and independent verifier;
- hard regressions across correctness, safety, latency, memory, and energy;
- exact GPU ownership/locking;
- finite candidate/run/token/time budgets;
- evaluator-integrity audit before negative verdicts.

The first governed loop should optimize one bounded surface—context-packer
weights or router thresholds—for at most twelve candidates. It cannot modify
its evaluator, evidence policy, budgets, or promotion authority.

## Initial paper queue

1. [Agentic Tool-calling and RL Training — 2606.00135](https://arxiv.org/abs/2606.00135): seed/prompt/template/history sensitivity.
2. [Replayable Financial Agents — 2601.15322](https://arxiv.org/abs/2601.15322): trajectory, decision, and evidence faithfulness as separate metrics.
3. [AutoRefine — 2601.22758](https://arxiv.org/abs/2601.22758): typed Rule/Skill/bounded-Subagent compilation.
4. [Multi-Agent Teams Hold Experts Back — 2602.01011](https://arxiv.org/abs/2602.01011): mandatory best-expert baseline.
5. [PerspectiveGap — 2606.08878](https://arxiv.org/abs/2606.08878): role-context completeness and leakage.
6. [SWE-Pruner — 2601.16746](https://arxiv.org/abs/2601.16746): deterministic/task-aware coding-context pruning before learned pruning.
7. [SCATE — 2607.08983](https://arxiv.org/abs/2607.08983): bounded coverage-aware supervisor.
8. [Speculative Decoding — 2211.17192](https://arxiv.org/abs/2211.17192), [Medusa — 2401.10774](https://arxiv.org/abs/2401.10774), and [EAGLE — 2401.15077](https://arxiv.org/abs/2401.15077): KV/speculation outcome matrix.
9. [ClawBench — 2604.08523](https://arxiv.org/abs/2604.08523): GUI final-action interception.
10. Karpathy autoresearch plus [AlphaEvolve — 2506.13131](https://arxiv.org/abs/2506.13131): governed bounded experiment loop.

[BudgetMem — 2602.06025](https://arxiv.org/abs/2602.06025) is queued next but
blocked until Memory v1 passes its frozen hard gates.

## Learning and portfolio output

Every adopted paper produces:

- a level-zero explanation;
- prerequisite map;
- exact reproduction plan;
- falsifiable extension idea;
- implementation, ablation, and negative-result evidence;
- concise technical report suitable for a public research portfolio.

This connects build-forward execution to fundamentals, reproducible research,
frontier-lab career preparation, and eventual entrepreneurship.
