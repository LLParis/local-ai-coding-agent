# Research-to-architecture synthesis

Date: 2026-08-20

This synthesis preserves the useful content from the two user research packets
without treating video claims as verified facts.

## Source packets

- Local-model and harness packet: SHA-256
  `7418b40564aa2812fdeeb3e71fc6d8d42477dfe73ae6d79de8d2f07c80a36392`
- Open-weight and small-model orchestration packet: SHA-256
  `0abd37f7f04c53c78c1d144063fbd87e9e76ee29a971632772284aadb81d263f`

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

## Tested, but not promoted

- Gemma 4 31B QAT Q4_0: fully GPU-resident at 32K; 1/3 implementation tasks.
  It passed a simple verifier calibration but rejected a correct real
  TypeScript patch, so it is not the v1 verifier.
- gpt-oss-20b: fast on a tiny historical task, then 0/3 on the harder tranche.
- Devstral Small 2: 1/3 as implementer, but 3/3 as verifier, including a real
  Qwen TypeScript patch. It is promoted only for verification.
- DeepSeek Harness concepts: trajectory inspection and replaceable seams are
  adopted. No unrestricted third-party harness/plugin runtime is installed in
  the production path; the video-described project was not found as an official
  `deepseek-ai` core repository.

## Test later

- SGLang/NVFP4 only if a controlled Qwen lane beats the current llama.cpp
  latency, reliability, and memory result on the same tasks.
- Qwen Coder Next or other CPU-offloaded MoE models only if end outcomes improve
  enough to justify slower memory streaming.
- Small language specialists for instruction discovery, test selection,
  language-specific linting, visual review, and watchdog diagnosis.
- Adapter fine-tuning or distillation after the trajectory store contains a
  meaningful set of repeated, independently classified failure examples.
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

## Primary references checked

- Qwen3.8: https://huggingface.co/Qwen/Qwen3.8-27B
- Gemma 4: https://huggingface.co/google/gemma-4-31B
- Gemma QAT GGUF: https://huggingface.co/google/gemma-4-31B-it-qat-q4_0-gguf
- Devstral Small 2: https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512
- Devstral Ollama package: https://ollama.com/library/devstral-small-2
- Better Harnesses, Smaller Models: https://arxiv.org/abs/2607.08938
- DeepSeek official repositories: https://github.com/deepseek-ai

## Measurable quality target

The stretch target is at least 90% independently accepted outcomes on a frozen,
representative workflow suite. It is not yet achieved. The suite must include
multiple trials, unfamiliar repositories, Python, TypeScript, PowerShell,
Swift, resume, malformed outputs, and real build/test diagnosis. Failed trials
remain in the denominator.
