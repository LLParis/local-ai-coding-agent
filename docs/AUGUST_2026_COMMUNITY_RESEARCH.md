# August 2026 local-model research intake

Date: 2026-08-20

Community reports are discovery evidence, not promotion evidence. They identify
configurations, failure families, models, and harnesses to test. The command
center promotes only repeated matched-task outcomes on EXCALIBUR.

## Source packets

| Packet | SHA-256 | Lines | Content |
|---|---|---:|---|
| Qwen3.8 search index | `19cac7ef5bf820f19bfa579630b677868747ed14717396563504026b60a6fd8a` | 765 | Search-result titles and snippets |
| Best Local LLMs | `53ec51d5497d49a9efc29d8cd834a3fac70886ec9cc1e6e45c2d2efff6d1beb9` | 893 | Full August 2026 megathread |
| Qwen uncensored discussion | `3dd3f7b8eb30a378caf86dfd2534fd474022695e1248ce67819be1155f5e02da` | 1,089 | Full discussion |

## Exact thread titles worth retaining

Highest-signal titles from the supplied packets and the follow-up web search:

1. **Best Local LLMs - August 2026**
2. **Local agentic coding Benchmark : Qwen 3.8 27B (in many weights quants / cache quants / engine / reasoning effort) vs others.**
3. **I benchmarked every Qwen 3.8 27B quant that fits in 16GB VRAM**
4. **Guide for running dense models on <=16 GB VRAM (Qwen 3.8 27B on 16 GB -> Q4_K_M, 130k ctx, ~20 t/s).**
5. **After pushing 1M+ tokens through Qwen 3.8 27B, here is my optimal llama.cpp config for 16GB VRAM (73k Context, Agentic Coding)**
6. **completely solved my qwen3.8 27b q8 thinking loops**
7. **Qwen3.8-27b has the highest level of "agency" I've ever seen in a local model**
8. **Qwen3.8-27B Uncensored Aggressive is out with K_P quants and HauhauCS FastMTP (up to 3.02x TG)!**
9. **Qwen 3.8 27B uncensored is basically like owning a car**
10. **What is the best uncensored/abliterated Qwen 3.8 27b model?**
11. **Qwen3.8-27B vs 3.6**
12. **I think people are seriously underestimating Qwen 3.8 27B.**
13. **Game over. 22GB local models run in Pi now outperform Claude Code Opus 5 High on real-world coding tasks published after training cutoffs**
14. **I ran Qwen3.8-27B against Opus, Sonnet, GPT and others. Results inside.**
15. **Qwen 3.8 27B - Day 3 Summary**
16. **Qwen3.8 27B vs Qwen3.6 27B vs Gemma 4 31B - the 24GB GPU comparison**
17. **Qwen3.8-27B; I don't get it**

Direct pages recovered by web search include the
[quant comparison](https://www.reddit.com/r/LocalLLM/comments/1vr4iqj/i_benchmarked_every_qwen_38_27b_quant_that_fits/),
[Pi/Sharp-template claim](https://www.reddit.com/r/ClaudeCode/comments/1vrqxqc/game_over_22gb_local_models_run_in_pi_now/),
[frontier comparison](https://www.reddit.com/r/LocalLLM/comments/1vst6ua/i_ran_qwen3827b_against_opus_sonnet_gpt_and/),
[73K configuration](https://www.reddit.com/r/LocalLLaMA/comments/1vqrt86/after_pushing_1m_tokens_through_qwen_38_27b_here/),
and [three-day synthesis](https://www.reddit.com/r/hermesagent/comments/1vr554a/qwen_38_27b_day_3_summary/).

## Net-new candidates

### Immediate challenger: Laguna XS 2.1

The useful hypothesis is not better zero-shot patching. It is better project
coherence after many turns: less duplicate implementation, stronger convention
adherence, more consistent testing/documentation, and less architectural drift.
The [official model card](https://huggingface.co/poolside/Laguna-XS-2.1)
describes a 33B-A3B agentic-coding model with 262K context. The
[official GGUF card](https://huggingface.co/poolside/Laguna-XS-2.1-GGUF)
provides a 20.3 GB Q4_K_M artifact. Its README still says upstream support is
pending, but [llama.cpp PR #25165](https://github.com/ggml-org/llama.cpp/pull/25165)
was merged on 2026-07-22. The pinned local build still needs an actual load,
template/tool-parser, and task proof before inference results are trusted.

### Conditional challenger: KAT-Coder V2.5 Dev

The [official card](https://huggingface.co/Kwaipilot/KAT-Coder-V2.5-Dev)
describes a 35B-A3B coding model with 262K context and reports lower malformed
tool labels and single-turn repetition. A provenance-pinned high-quality quant
and correct runtime/parser are prerequisites.

### Specialist candidates

- [Gemma 4 26B-A4B](https://huggingface.co/google/gemma-4-26B-A4B) is a
  fast auxiliary/state/vision challenger; it does not replace the installed
  Gemma 4 31B without matched evidence.
- [UI-Mate 27B](https://github.com/Tencent/UI-Mate) is a later disposable
  Windows/macOS GUI specialist, not a coding-agent replacement.
- Laguna S 2.1, Ling 3 Flash, DeepSeek V4 Flash, and LongCat Flash Lite are
  host-offload or multi-GPU research lanes. The 96 GB DDR5 capacity makes some
  experiments possible, but they must beat fully resident models on total task
  outcome and wall time.

## Experiments added

1. A frozen 20-step evolving-repository coherence task; expand to 50 steps only
   if 20 separates candidates.
2. Qwen Q6 with F16, Q8, and Q4 K/V cache on identical 32K and native-context
   tasks.
3. No speculation versus native MTP versus target-specific DFlash, recording
   acceptance, output equality, task score, VRAM/RAM, and hangs.
4. Each model's official sampling, chat template, tool parser, and preserved-
   thinking recipe versus the current deterministic baseline.
5. Base versus uncensored/Heretic derivative only after the base model refuses
   an authorized coding or defensive-security task.
6. Pi plus the Sharp Qwen template as a challenger, never as a result accepted
   from the Reddit claim alone.

## Claims not promoted

- Perplexity similarity between Q4_K_M and Q8 does not establish equal coding,
  tool, or long-context quality.
- No supplied packet contains a reproducible 130K recipe.
- No supplied packet proves Q8 KV fixes thinking loops.
- "Opus-level," ACT/college, and highest-agency headlines are discovery leads.
- An uncensored derivative can lose coherence; reduced refusal is not increased
  coding intelligence.
- DeepSeek V4 Flash's official 304B/167 GB artifact is not a bounded single-5090
  candidate even though 96 GB system RAM enables contained offload research.
