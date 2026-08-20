# Coding Intelligence operating contract

This repository is the general Coding Intelligence / Agent Continuity control
plane. It is not an Anime Frontier or Media OS product feature. Those projects
may supply frozen evaluation fixtures only when the task explicitly says so.

## System roles

- EXCALIBUR is the inference, routing, lifecycle, telemetry, and evaluation
  command center.
- The Mac is the trusted Apple-development and media-tool execution edge.
- Hosted Codex is the highest-judgment architect and verifier when available.
- A local model earns implementation authority through repeated real tasks; a
  health response or vendor benchmark never qualifies it.

## Outcome-first execution

"Best" and "world-class" mean the best end-to-end result under the real time,
usage, compute, and product constraints. Those words never authorize extra
gates, audits, abstractions, safety theater, or test volume unless the added work
materially improves the requested outcome.

Before work begins, state one concrete slice deliverable and its stopping
condition. Resume the exact unfinished slice from the latest handoff instead of
redesigning completed foundations.

- Use the shortest path that produces the real requested outcome.
- Do not add infrastructure unless an observed blocker requires it for the
  current slice.
- Do not repeat the same failed command or model strategy more than twice.
  After the second failure, diagnose once, simplify or bypass the mechanism,
  and return to the mission.
- One qualification trial gets one model response. No autonomous retry loop.
- Run the smallest authoritative verification that can disprove the result.
  Test count is not an achievement by itself.
- Stop a slice when its stated outcome is proven. Record follow-up ideas instead
  of expanding the slice.
- Report configured, tested, and production-proven states separately.

## Research-verdict integrity

The default research verdict is neither yes nor no. A candidate remains
unresolved until an applicable, current, falsifiable experiment distinguishes
it. Caution is not evidence against a candidate, and a failed evaluator is not
evidence that the candidate failed.

- Before rejecting or demoting anything, prove that the model-visible request
  actually asks every question scored by the oracle.
- Test required behavior, not one preferred implementation, wording, diagnosis
  label, source hash, or serialization unless exact identity is the contract.
- Treat a different implementation that passes the authoritative behavioral
  test as a candidate success, then evaluate its real risks separately.
- Pin the candidate, runtime, harness, prompt/template, tools, context, and
  evaluator. A stale or incompatible component invalidates the trial rather
  than the candidate.
- Classify terminal outcomes explicitly as `accepted`, `rejected`,
  `inconclusive`, `evaluator_invalid`, or `runtime_blocked`. Never collapse the
  last three into rejection.
- Audit an unexpected failure once for prompt omission, hidden-answer leakage,
  stale fixtures, scorer overconstraint, runtime mismatch, and verifier bugs
  before issuing a negative verdict. Audit an unexpected success for leakage
  and weak tests with equal rigor.
- Give evaluators positive controls, negative controls, semantically equivalent
  implementations, malformed outputs, and mutation tests. Validate the judge
  before trusting its judgment.
- Preserve invalid trials and corrections in the evidence ledger, but exclude
  evaluator-invalid trials from candidate win/loss denominators.
- Do not accumulate gates as a substitute for discernment. Every test must map
  to an observed failure, required capability, or explicit research question.

## Edit and promotion boundary

Unqualified local models work on an isolated copy of real code. Isolation is a
staging boundary, not a demo or substitute product. The model receives only
explicit context and mutable paths; verifier-only fixtures remain hidden. A
hosted or independently qualified verifier reviews the diff and authoritative
test result before any patch can be promoted to user work.

Never use broad process kills, destructive Git commands, or bulk filesystem
mutations. Reconcile services by exact owner identity. Preserve unrelated and
pre-existing work.

## Execution context first

Before the first mutation, verify the intended host, repository/workspace,
writable scope, Windows integrity level, and process/service owner. If the slice
requires administrative or service authority, establish one dedicated owned
elevated execution path before proceeding. Do not discover missing authority
through cascading failed commands, approval prompts, or cross-session retries.
Use the repository's normal non-elevated context for ordinary source edits and
tests; elevation is an execution requirement, not a substitute for correct
scope.

## Primary-path reliability

A recovery rail does not satisfy the primary outcome. The primary path is not
complete until it starts automatically, reconnects without user intervention,
has exact lifecycle ownership, exposes current health and failure state,
updates with rollback, and survives the relevant process/app/login restart.

- Never relabel a recovery rail as a successful fallback when ordinary use of
  the primary still fails.
- If normal operation depends on SSH, a repair script, a manual prompt, or a
  second application, report the primary as broken and continue repairing it.
- Keep one independently owned repair rail so the primary can be recovered, but
  do not route routine work through it.
- After repairing a primary, prove stop/start, reconnect, and fresh-process or
  login recovery. A health response in the same process is insufficient.
- Make failure observable and bounded; repeated silent reconnect loops are not
  resilience.

## Child-agent lifecycle

Every spawned child must have one parent, a bounded role, one terminal event,
and deterministic retirement. Completion without cleanup is a failed run.

- On success, failure, cancellation, or parent abort, close the child's tool
  calls and transcript, terminate only its owned processes, and release its
  resources.
- Return a bounded cited result to the parent. Store large tool output once as
  a content-addressed artifact; never multiply it across active transcripts.
- Keep root/user tasks visible. Automatically archive terminal child tasks only
  after the configured retention window; archival must be reversible and must
  never be implemented as deletion.
- Record an archive failure once with the exact child ID and error. Do not retry
  it in an open loop or let one bad record block cleanup of unrelated children.
- Doctor/status must report active root tasks, active children, terminal children
  awaiting retirement, retained bytes, and cleanup failures separately.

## Canonical evidence

Read `docs/handoffs/CODING_INTELLIGENCE_COMMAND_CENTER_HANDOFF.md`, then
`docs/handoffs/LOCAL_CONTINUITY_WORKING_HANDOFF.md`, before changing lifecycle,
routing, or qualification behavior. Current live state must still be checked;
handoffs are claims, not substitutes for process, listener, model, diff, and
test evidence.
