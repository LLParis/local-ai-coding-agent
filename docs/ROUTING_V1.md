# Coding Intelligence Routing v1

Date: 2026-08-20

## Outcome

Routing v1 is the deterministic control-plane core for the harness fusion. It
chooses an already-qualified route from a versioned evidence manifest and a
caller-supplied runtime snapshot. It does not start a backend, call a model,
touch the network, edit a workspace, retry a task, or change a qualification
verdict.

The four execution layers remain separate:

```text
task request
    -> model                 intelligence provider
    -> harness               tool/agent protocol
    -> verifier              independently qualified review role
    -> inference edge        where model inference runs
    -> execution edge        where repository tools/builds run
```

This distinction is operational. For the proven Swift route, Qwen inference
runs on EXCALIBUR, the bounded thin harness owns the edit contract, Xcode/Swift
runs on the Mac edge, and Devstral supplies the local verifier verdict. Pi is a
harness candidate, not a model. DeepSeek is a harness candidate. Laguna is a
model candidate.

Implementation:

- `src/agent_continuity/routing.py`
- `config/routing-v1.json`
- `tests/test_routing.py`

The checked-in manifest's canonical semantic SHA-256 at this revision is
`sha256:07b583e8f3b50ccbb05f69a528c31b0f4b90b127eee82c6740f86cc602ab75df`.
The router emits that hash on every decision.

## Current truthful authority

| Route | Model | Harness | Verifier | Execution edge | Status |
|---|---|---|---|---|---|
| Hosted primary | Hosted Codex GPT-5.6-Sol | Hosted Codex app | Hosted Codex arbiter | Hosted cloud | `accepted` while available |
| Bounded sovereign coding | Qwen3.8 27B Q6 | Thin structured edit | Devstral Small 2 | EXCALIBUR | `accepted` for Python, TypeScript, and PowerShell at up to 32K |
| Bounded sovereign Apple coding | Qwen3.8 27B Q6 | Thin structured edit | Devstral Small 2 | Mac Apple edge | `accepted` for proven Swift/Xcode work at up to 32K |
| Sovereign verification | Devstral Small 2 | Local structured verifier | Factual authority remains project evidence | EXCALIBUR | `accepted` |
| Native-context Qwen | Qwen3.8 27B Q6 | Thin structured edit | Devstral Small 2 | EXCALIBUR | `inconclusive` until the final matrix passes |
| Qwen through Pi | Qwen3.8 27B Q6 | Pi four-tool adapter | Devstral Small 2 | EXCALIBUR | `not_run` on a live model |
| Qwen through DeepSeek | Qwen3.8 27B Q6 | DeepSeek adapter candidate | Devstral Small 2 | EXCALIBUR | `runtime_blocked` pending the reviewed four-tool plugin |
| Laguna | Laguna XS 2.1 Q4_K_M | Thin structured edit | Devstral Small 2 | EXCALIBUR | `not_run`; artifact integrity is not runtime qualification |

Qwen3.8 Q8, Qwen3.6, Gemma 4, and gpt-oss remain visible role-specific
candidates. Gemma's verifier route remains explicitly `rejected`; its separate
research/vision route remains `not_run`. A role-specific rejection is not a
global model deletion.

This is a provisional routing portfolio. It is not a final model-tournament
winner or a measured 90–95% frontier-equivalence claim.

## Manifest contract

The JSON manifest has six top-level fields and rejects unknown fields:

1. `schema_version`: exactly `1`.
2. `manifest_id`: stable lowercase identity.
3. `evidence`: local artifact ID, relative path, and canonical `sha256:` digest.
4. `components`: independent model, harness, verifier, and edge catalogs.
5. `routes`: explicit component composition plus task surface and qualification.
6. `policy`: cloud primary, sovereign routes, Apple edge, production statuses,
   and retry count.

`load_routing_manifest()` verifies every evidence file and digest by default.
An edited handoff, run record, or decision document therefore makes the
manifest stale rather than silently changing its meaning. Updating evidence is
an intentional manifest revision.

Validation first takes one detached canonical JSON snapshot, and the returned
manifest exposes immutable component and route mappings. Runtime sets are also
copied into frozen sets. A caller therefore cannot promote a route or alter
decision evidence after verification while retaining the earlier manifest or
runtime hash.

Only `accepted` is production eligible. The complete verdict vocabulary is:

- `accepted`
- `rejected`
- `inconclusive`
- `evaluator_invalid`
- `runtime_blocked`
- `not_run`

The last four do not become rejection. `adjudicate_qualification_status()` also
preserves an existing status when the new observation is evaluator-invalid,
blocked, inconclusive, or absent. Acceptance or rejection requires an
applicable valid evaluator and at least one evidence ID.

An accepted implementation route must name an accepted verifier. Every
sovereign route must resolve entirely to local models, harnesses, verifier
components, and edges. An Xcode tool requirement must resolve to the configured
Mac Apple edge.

## Routing inputs

The task request declares only observable requirements:

- stable task ID;
- role (`architecture`, `inspect`, `implement`, or `verify` in the current
  manifest);
- capability, such as `scoped_edit`, `diagnosis`, or `verification`;
- implementation language;
- current packed context tokens;
- exact tool needs;
- `production` or an explicitly named `qualification` candidate.

The runtime snapshot is supplied by the caller and contains:

- whether hosted cloud access is currently available;
- operable model IDs;
- operable harness IDs;
- available inference/execution edges;
- satisfied live qualification gates.

"Operable" means the lifecycle owner can make the component available. It does
not claim every one-hot EXCALIBUR model is simultaneously GPU-resident. Backend
switching remains outside the pure router.

Example:

```python
from agent_continuity.routing import (
    RoutingRequest,
    RuntimeState,
    load_routing_manifest,
    select_route,
)

manifest = load_routing_manifest("config/routing-v1.json")
request = RoutingRequest(
    task_id="repair-parser-001",
    role="implement",
    capability="scoped_edit",
    language="python",
    context_tokens=16000,
    tool_needs=("repo_inspection", "structured_edit", "project_test"),
)
runtime = RuntimeState(
    cloud_available=False,
    operable_model_ids=frozenset({"qwen3.8-27b-q6", "devstral-small-2-24b"}),
    operable_harness_ids=frozenset(
        {"thin-structured-edit", "local-structured-verifier"}
    ),
    available_edge_ids=frozenset({"excalibur", "mac-apple"}),
)
decision = select_route(manifest, request, runtime)
```

That request deterministically selects `local-qwen38-q6-bounded` and records
`cloud_unavailable` as the fallback reason. It does not make a model call.

## Selection algorithm

Production selection is deterministic and independent of JSON array order:

1. Validate request, runtime IDs, component references, evidence, and policy.
2. Consider every route and retain its exact ineligibility reason codes.
3. Select the hosted primary when it is compatible and cloud access exists.
4. Otherwise find the best compatible `accepted` single-expert route by
   explicit priority and stable route ID.
5. A team may replace that single route only when it has the same task surface,
   names that exact best single as its baseline, cites comparative evidence,
   and records a positive measured gain. Among proven teams, the largest gain
   wins deterministically.
6. If no accepted compatible route exists, return `unroutable`; never invent a
   fallback or silently select an unproven candidate.

Qualification mode is deliberately different. It selects only the named
candidate and may exercise `not_run`, `inconclusive`, `evaluator_invalid`,
`runtime_blocked`, or `rejected` candidates when their current components and
runtime gates are ready. That is permission to run a controlled qualification,
not production promotion.

## Decision telemetry

Every decision includes:

- selected route, model(s), harness, verifier, inference edge, execution edge;
- fallback source, top-level reason, and exact detailed reason codes;
- every considered route, its current verdict, and all ineligibility reasons;
- selected evidence IDs and their pinned SHA-256 values;
- canonical manifest, request, and runtime SHA-256 values;
- `automatic_retries: 0`.

Typical exact reason codes include `cloud_unavailable`,
`status_inconclusive`, `context_exceeds_route_limit`,
`tool_unsupported:xcode`, `runtime_tag_missing:pi-live-model-gate`, and
`verifier_model_unavailable:devstral-small-2-24b`. The reason ledger makes an
unroutable result diagnosable without rerunning or guessing.

## Single-expert baseline and model preservation

The router does not assume a multi-model team is better. The best eligible
single expert is calculated first and retained as the baseline. An accepted
team must match the baseline's roles, capabilities, languages, tools, and
context surface, then cite a positive matched comparison. A team cannot earn
authority from architecture enthusiasm alone.

The router also never mutates or deletes a model record. A failed evaluator,
runtime outage, incomplete matrix, or absent trial remains its own status. A
valid role-specific rejection can remove that route from production selection,
but the model and its other role hypotheses remain in the evidence catalog.

## Promotion procedure

Promotion remains outside routing selection:

1. Pin model artifact, runtime, harness, prompt/template, tools, task fixture,
   and evaluator.
2. Run one response per trial with zero automatic retry.
3. Audit unexpected success and failure for evaluator validity.
4. Record one of the six terminal statuses with exact evidence.
5. Only after repeated applicable real-task wins, update the relevant route to
   `accepted`, update its evidence hashes, and rerun manifest mutation tests.
6. Do not delete the prior candidate or invalid trials.

Pi, DeepSeek, Laguna, native-context Qwen, Q8, Qwen3.6, Gemma, gpt-oss, future
quants, and future teams all use this same procedure. Routing v1 expresses the
current evidence; it does not prejudge the remaining tournament.

## Current integration boundary

The routing core is ready for a status/dispatch caller, but this slice does not
change `cli.py`, the production task runner, backend lifecycle, memory store,
or adapter executors. Until that caller is connected, existing commands keep
their current behavior. Integrating selection must preserve the router's pure
decision first, then hand the selected IDs and hashes to the separately owned
backend switch and executor.
