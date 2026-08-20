# Native 262K qualification suite

This directory is the frozen offline generator and deterministic scorer for the
Qwen3.8-27B Q6 native-context lane. It does not start llama.cpp, switch a
backend, or call a model.

## Frozen matrix

`manifest.json` defines five task families at exact prompt targets of 131,072,
196,608, and 235,930 tokens:

1. depth-balanced retrieval with an explicit contradiction;
2. temporal decision synthesis and supersession;
3. repository diagnosis, one scoped edit, and a hidden test;
4. ordered tool-contract execution without duplicate effects;
5. fresh-process resume/compaction state equality.

The required runtime recorded in every case is Qwen3.8-27B Q6, llama.cpp
`b10435`, native 262,144 context, Q4 KV, and MTP off.

## Live-runner boundary

Import `suite.py` and call `build_case` with the exact runtime tokenizer—not an
estimate. Include the real system prompt, tool schemas, skills, and task ledger
in `harness_prefix`, because the target covers the complete model request.

```python
case = build_case(
    "tool_contract",
    196_608,
    llama_token_count,
    tokenizer_id="llama.cpp-b10435-qwen38",
    harness_prefix=real_harness_prefix,
    padding_factory=tokenizer_aware_filler,
)
write_case(output_directory, case)
```

The padding factory receives `(requested_tokens, deterministic_seed)` and must
be prefix-stable for increasing budgets. The generator measures every candidate
with the supplied tokenizer and refuses an inexact result. The built-in
`whitespace_token_count` exists only for offline contract tests.

Give the live runner only `input.json`. Keep `oracle.json` outside model-visible
paths. The runner stages any `fixture` files according to their `visibility`,
sends `prompt.text`, allows one response and zero automatic retries, and emits
the normalized `coding-intelligence-long-context-runner-output/v1` object.
`validate_runner_output` is the strict executable schema. Its top-level fields
are:

```text
schema, suite_id, case_id, phase, trial_index,
model, runtime, prompt, response, telemetry
```

The response always carries the same typed fields, using empty objects/lists or
`null` where a family does not use one:

```text
status, citations, facts, rejected_claim_ids, temporal_answers,
active_constraint_ids, next_action_ids, diagnosis_code, edit, verification,
tool_trace, resume_state, duplicate_effect_ids
```

Telemetry records exact request/reasoning/output tokens, TTFT, wall time,
prompt/decode rate, peak VRAM/RAM, warnings, restarts, and timeout state.

Call `score_runner_output(input, oracle, result)` for one trial. Call
`aggregate_qualification(scores)` after one warmup plus measured trials 1, 2,
and 3 for all fifteen cases. Missing, malformed, timed-out, duplicate, or failed
trials remain in the denominator. Qualification requires every critical check,
all 60 runs, and no more than five points of 50%-to-90% degradation.

## One-shot live CLI

`live_runner.py` performs the same contract against an already-running
loopback llama.cpp server. It never starts, stops, or switches a backend:

```powershell
py -3 evals\long_context\live_runner.py `
  --base-url http://127.0.0.1:8818 `
  --model arm-qwen38-q6-native-262k `
  --family depth_retrieval `
  --target 131072 `
  --phase warmup `
  --trial-index 0 `
  --output runs\long-context\retrieval-50-warmup.json
```

Before the one inference request, the runner checks `/health`, the exact model
alias in `/v1/models`, and `/props` for Q6 model path, native context, one slot,
MTP-off/speculative-off, text-only mode, and pinned build. It counts the whole
chat request with `/v1/chat/completions/input_tokens`; compatible older servers
fall back to `/apply-template` plus `/tokenize`. It discovers stable one-token
padding through `/tokenize`, materializes the exact fill, and recounts the final
request. Any drift stops before inference.

Inference is one non-streaming `/v1/chat/completions` call with zero retries.
The runner checks response model/build and usage token counts, applies the code
family's single edit only in a temporary copied fixture, owns the hidden test,
normalizes the result, scores it, and writes one immutable JSON record. Timeout,
malformed output, runtime mismatch, and failed scores remain truthful terminal
records. Stdout contains one compact JSON summary only.
