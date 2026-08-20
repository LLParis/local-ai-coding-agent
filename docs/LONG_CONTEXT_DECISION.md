# Long-context and knowledge-grounding decision

Date: 2026-08-20

## Decision

Keep two Qwen3.8 Q6 lanes with different jobs:

1. **Bounded production lane:** Q6, 32K, the existing one-response structured-edit path. It remains the fast routine executor.
2. **Required interactive baseline:** Q6 weights, native `262,144` context, Q4 K/V cache, one slot, text-only, MTP off. This is the first configuration that must pass long-horizon agent, compaction, and endurance tests.

Do not lower weight precision merely to claim full context: Q6 already allocated the native window on this RTX 5090. Dynamic Q5/Q4 GGUFs and Blackwell NVFP4 are challenger lanes only after the Q6 baseline runs the same frozen tasks. MTP-on is also a challenger, not the full-context baseline.

The native window is `262,144`. The model's documented approximately one-million-token mode is a static YaRN extension, not native context. It is a later ceiling experiment because static scaling can harm shorter inputs (`D:\11_CS\00_REPOS\AI Research Mastery\docs\QWEN38_DOSSIER.md:14,45,54-57`).

## Evidence, not Reddit consensus

The long-context packet at `C:\Users\sirlo\.codex\attachments\a046dc38-0251-4910-b334-f6ed44ac1363\pasted-text.txt` is an anecdotal Reddit thread (SHA-256 `85826c0f56e68c58914fce83c56fcfee636520e5800d8ef057d8c6ddac7f9364`). It repeatedly claims 50K is inadequate, recommends 128K–262K, reports 70–80% compaction thresholds, and discusses MTP/llama.cpp failures (`pasted-text.txt:30-44,83-156,524-676,701-822,858-996,1053-1168`). These are candidate hypotheses, not quality proof.

The local repository has stronger evidence. Its results explicitly label the tranche partial and single-run (`D:\11_CS\00_REPOS\AI Research Mastery\experiments\0001_baseline\RESULTS.md:1-5`):

| Configuration | Short smoke | Decode | VRAM after | Interpretation |
|---|---:|---:|---:|---|
| Q6 / 262,144 / Q4 KV / MTP off | pass | 56.63 tok/s | 30,163 MiB | About 2,444 MiB below the GPU's 32,607 MiB nominal total; viable first baseline |
| Q6 / 262,144 / Q4 KV / MTP on | pass | 141.92 tok/s | 31,918 MiB | Only 270–331 MiB observed free; ceiling treatment |

The raw records are `q6_text_256k_q4kv_medium_mtp_off_text_smoke.json` and `q6_text_256k_q4kv_medium_mtp_on_text_smoke.json`. The first reports 56.62699 tok/s, 30,163 MiB used and 2,025 MiB reported free after the request. The second reports 141.91578 tok/s, 31,918 MiB used and 270 MiB free. Whole-GPU readings include the Windows desktop, so nominal subtraction and driver-reported free differ.

This proves allocation plus one 37-token prompt and 98-token completion. It does not prove retrieval, synthesis, positional fidelity, tool use, or stability near context capacity; the repository states that directly (`RESULTS.md:37-46,68-74`; `curriculum/foundations_00/README.md:9-13,26-30,44-48`).

## Host capacity

A live read-only snapshot on 2026-08-20 confirmed:

- RTX 5090: 32,607 MiB VRAM;
- system memory: 96.0 GiB DDR5 configured at 6000 MT/s;
- free system memory at capture: 68.41 GiB.

RAM is valuable capacity for model files, local knowledge indexes, event/blob archives, CPU tools, and an emergency offload/fallback path. It is not a substitute for GPU memory bandwidth. Active weights or KV state that spill into host RAM traverse a much slower memory/PCIe path, so more context may fit while prompt processing and decode become materially slower. Record GPU residency, host-RAM use, page faults, prompt rate, and decode rate; never describe “fits in 96 GB” as equivalent to “fully GPU-resident.”

## Configuration roles

| Lane | Initial status | Purpose |
|---|---|---|
| Q6 / 32K / current bounded settings | production baseline | One bounded diagnosis/edit/test response |
| Q6 / 262,144 / Q4 KV / MTP off | required next qualification | Premier interactive and long-horizon baseline |
| Same Q6 native lane / MTP on | challenger | Speed treatment after reliability and headroom gates |
| Dynamic Q5/Q4 GGUF / native context | challenger | Test whether extra VRAM margin improves end outcomes enough to offset weight degradation |
| Blackwell NVFP4 | challenger | Blackwell-specific throughput/capacity treatment after exact artifact/runtime support exists |
| Q6 / YaRN approximately 1.01M | later ceiling | Ultra-long allocation and quality experiment after native 262K succeeds |

Vision is a separate lane. The premier 262K baseline is text-only so the projector does not consume the VRAM margin needed for native context. A future vision lane gets its own measured context ceiling and cannot inherit text-only results.

## Frozen native-context suite

Freeze five agent tasks at three measured prompt-fill levels. Token counts come from the exact runtime tokenizer and include the real harness system prompt, tools, skills, task ledger, retrieved memory, conversation, and tool-result summaries.

| Fill | Prompt-token target | Purpose |
|---|---:|---|
| 50% | 131,072 ±0.5% | Normal long session; no compaction in the raw-capacity arm |
| 75% | 196,608 ±0.5% | Initial compaction boundary and sustained interactive pressure |
| 90% | 235,930 ±0.5% | Contained ceiling probe; production policy blocks ordinary continuation here |

Each prompt reserves up to 24,576 tokens for reasoning and visible output. The five tasks are:

1. **Depth-balanced retrieval:** exact needles and distractors at early, middle, and late positions; return cited source IDs and reject a planted contradiction.
2. **Temporal decision synthesis:** preserve an early architectural decision, a later supersession, active constraints, and an `as_of` answer while completing a current plan.
3. **Repository diagnosis/edit:** find a real planted defect whose deciding context appears far from the code excerpt, produce one scoped edit, and pass hidden tests.
4. **Tool-contract continuity:** make ordered read/search/edit/test calls, survive large summarized tool results, and emit valid schemas without loops or duplicate effects.
5. **Resume/compaction:** stop at a semantic checkpoint, resume in a fresh process, preserve objective/open items/artifact hashes, and continue with the correct next action.

Each configuration receives one warmup and three measured trials per `(fill, task)` in randomized order. A failed or timed-out trial remains in the denominator. Capture exact request tokens, positional recall, citations, hidden-test outcome, tool-schema validity, repeated-call count, reasoning/output tokens, TTFT, prompt/decode rate, wall time, peak VRAM/RAM, backend warnings, compaction events, and resume-state equality.

Native Q6/MTP-off qualifies only with:

- 100% critical constraint, supersession, citation, and resume-state retention;
- 100% hidden-test and tool-schema success;
- zero repeated identical tool loops, duplicate side effects, malformed outputs, hangs, or backend restarts;
- no more than five percentage points degradation from 50% to 90% on any scored non-binary metric;
- three clean repetitions at every fill.

## Compaction watermarks

The raw-capacity arm disables automatic compaction for one contained request at each fill. The production policy arm uses the loss-checked process in [`MEMORY_ARCHITECTURE_DECISION.md`](MEMORY_ARCHITECTURE_DECISION.md):

- **50%:** checkpoint and record fill telemetry; do not compact merely because the session is large.
- **70% or projected next request above 75%:** generate a compaction candidate in the background.
- **75%:** commit only after invariant, provenance, open-item, unknown-tool, and artifact-hash validation; target at most 55% fill after compaction.
- **90%:** hard stop for ordinary next steps. Require a validated compaction, a fresh child/subsession, or a fresh-session handoff. Never gamble the remaining window on another unconstrained model turn.

Watermarks are initial policy, not doctrine. The 50/75/90 suite may move the 70/75 triggers earlier, but never later than a demonstrated quality cliff. Compaction does not delete raw events.

## Protect the orchestrator context

Long tool output belongs in the event/blob store. The main session receives a typed summary containing only question, cited findings, exact source locators/hashes, unresolved conflicts, and the next required action. Full compiler logs, HTTP bodies, document text, and child trajectories remain retrievable by reference.

Research, broad repository reconnaissance, and knowledge lookup should run in bounded child/subsessions with independent context. A child returns cited findings rather than its entire tool transcript. This preserves the orchestrator's native window for state, decisions, synthesis, and verification instead of filling it with read/search mechanics.

## Parametric knowledge is a separate capability

The knowledge packet at `C:\Users\sirlo\.codex\attachments\9fa71a97-63ed-4104-be75-819a7399228f\pasted-text.txt` claims Qwen3.8-27B lost obscure parametric knowledge relative to Qwen3.6 (SHA-256 `992c13f0fa8ecc2a54d41e34bce6c856eed1be5ec9a47d9ad852fab23e173c14`). It is another Reddit discussion, not a controlled local result.

Evaluate four capabilities separately:

1. **Closed-book recall:** did the model know the answer from weights?
2. **Calibrated abstention:** did it admit uncertainty instead of hallucinating?
3. **Tool selection:** did it choose the right official-doc, web, or local-corpus search?
4. **Grounded answer:** did it synthesize the cited sources correctly?

For coding and current technical facts, prefer local code/artifacts, official documentation, and primary web sources over parametric recall. Qwen3.6 and Gemma remain candidate knowledge specialists only if the same frozen suite proves a role-specific gain; EXCALIBUR's one-hot model policy means they are swapped, not assumed simultaneously resident.

Air-gapped knowledge is a later local-corpus lane: versioned ZIM/document sources, SQLite FTS, provenance, and optional embeddings only after lexical failures justify them. The model receives short cited excerpts through a knowledge subsession, never an encyclopedia-sized dump in the main context.

## Runtime reliability boundary

MTP and recent long-running llama.cpp paths have unresolved reliability risk:

- [llama.cpp #26827](https://github.com/ggml-org/llama.cpp/pull/26827) reports 100K+ MTP prefills hard-locking a host through unsynchronized multi-ubatch decode; MTP-off completed the same requests.
- [llama.cpp #26558](https://github.com/ggml-org/llama.cpp/issues/26558) reports hard CUDA failure under MTP plus KV saturation.
- [llama.cpp #27330](https://github.com/ggml-org/llama.cpp/issues/27330) reports stochastic CUDA-graph GPU hangs on RTX 5090/Qwen3.8 under sustained agent load, with and without MTP. That is a separate graph-path risk, so disabling MTP alone is not a complete reliability argument.

These reports are not proof that pinned build `b10435` fails on EXCALIBUR. They are reasons to keep the current pinned runtime, use MTP-off for the first full-context baseline, record CUDA-graph state, and require a soak before changing production.

MTP-on promotion requires the same full frozen suite plus a rolling 75%-fill endurance run with at least 100 completed turns or eight hours, whichever is longer. It must show zero hangs/restarts, preserve every scored answer, improve median end-to-end latency, and leave at least 1.5 GiB observed free VRAM under the real desktop load. The existing 262K/MTP-on smoke does not meet the headroom condition.

## One-million-token ceiling

Only after native Q6 passes may a YaRN configuration target approximately 1.01M tokens. It is a separate server profile with an exact runtime/model configuration and cannot serve short routine work. Test allocation first, then 50/75/90% positional retrieval and synthesis, then tool/resume tasks. Compare against native 262K plus validated compaction and retrieval; the larger raw window wins only if end outcomes improve enough to justify memory, latency, and scaling-quality costs.

## Promotion order

1. Preserve Q6 32K as the bounded production lane.
2. Freeze and run Q6 native 262,144/Q4-KV/MTP-off at 50/75/90% fill.
3. Prove loss-checked compaction, subsession returns, and fresh-process resume.
4. Test MTP-on after reliability/headroom preconditions.
5. Test dynamic Q5/Q4 and Blackwell NVFP4 against the same Q6 baseline.
6. Evaluate Qwen3.6/Gemma knowledge-specialist roles and a local air-gapped corpus separately.
7. Run the approximately 1.01M YaRN ceiling experiment last.

No configuration is promoted from allocation, throughput, Reddit reports, or one short smoke. Promotion requires repeated real diagnosis, edit, tool, retrieval, compaction, and verification outcomes with truthful telemetry.
