# Bounded local-model tournament — tranche 1

Date: 2026-08-20

This tranche used three real defects in the Coding Intelligence implementation.
Each model received exactly one response per case, with no automatic retry. The
source workspace remained unchanged during qualification; verifier files were
staged but hidden from the model.

| Case | Qwen3.8 Q6 | Devstral Small 2 | Gemma 4 31B | gpt-oss-20b |
|---|---|---|---|---|
| Exact launched-model readiness | Failed; capped rerun returned output but broke a caller contract | Passed in 4.276 s, with redundant endpoint edit | Failed: empty structured response | Failed: empty structured response |
| Cross-platform checkpoint paths | Passed in 17.714 s | Failed: missing `PureWindowsPath` import | Failed: truncated JSON | Failed: empty structured response |
| ControlMaster ownership before tunnel reuse | Passed in 16.108 s | Failed: checked file owner, not live master | Passed in 64.656 s | Failed: invalid tool-call payload / HTTP 500 |

The Qwen checkpoint and tunnel patches were reviewed by hosted Codex and
promoted to the canonical source. Hosted Codex supplied the final minimal case-1
fix after no local model produced a clean accepted patch.

Cross-language Qwen evidence:

- TypeScript ownership fix: passed in 2.734 s.
- PowerShell ownership fix: initial under-specified task failed; corrected task
  exposed the `StartTimeUtc` contract and passed in 6.910 s.

Verifier evidence:

- Gemma passed two simplified calibration verdicts but rejected the correct real
  TypeScript patch; it was not promoted.
- Devstral accepted a known-good patch, rejected a known-incomplete patch, and
  accepted the correct real TypeScript patch: 3/3. It is the v1 local verifier.

## Routing decision for v1

- Default local implementer: Qwen3.8 27B Q6 for scoped coding work.
- Default local verifier: Devstral Small 2 24B.
- Gemma 4 31B: research/vision candidate only.
- gpt-oss-20b: fast tiny-task/diagnostic candidate only; it is not promoted as
  an autonomous builder for harder work.
- Factual authority: hidden repository tests. Hosted Codex remains architect and
  escalation arbiter while available.

This is a provisional v1 routing decision, not a universal model ranking.
Raw run records and retained staged paths are under `runs/`.
