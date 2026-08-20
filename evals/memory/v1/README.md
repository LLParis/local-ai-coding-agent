# Frozen Memory v1 evaluation

This directory contains the synthetic, deterministic forty-case Memory v1
qualification suite required by `docs/MEMORY_ARCHITECTURE_DECISION.md`. It
creates a new temporary store for every case. It never reads user memory, calls
a model, uses the network, or mutates a project workspace.

## Frozen matrix

`manifest.json` declares exactly five cases in each tranche:

1. exact path, symbol, command, error, task, event, hash, and effect recall;
2. paraphrase recall with behaviorally meaningful lexical anchors;
3. English/Chinese recall with proper-name and numeric identity anchors;
4. current and historical temporal truth, including retirement;
5. exact owner, workspace, task, role, and session isolation;
6. conflicts, authoritative correction, no-memory, provenance, and secret rejection;
7. repeated loss-checked compaction of every typed state invariant;
8. real process-death replay plus protected and eligible retention cases.

Every case explicitly declares seed events, query text, exact scopes, `as_of`,
expected and forbidden memory IDs, an expected typed-state profile, invariant
families, evidence requirements, the `2048/4096/512/max-8` context budget,
structured expected answer facts, and whether the later model-assisted phase is
required. `suite.lock.json` pins the canonical manifest digest.

## Verdict integrity

Each case validates the evaluator before its candidate result is trusted:

- a golden positive control;
- a semantically equivalent control with reordered set-valued data;
- a declared behavior-negative control;
- a malformed candidate-output control, rejected by the output contract;
- a malformed evaluator record, classified `evaluator_invalid`;
- an independent fabricated-memory mutation.

The allowed terminal outcomes are `accepted`, `rejected`, `inconclusive`,
`evaluator_invalid`, and `runtime_blocked`. Invalid controls are retained in the
report but never enter candidate win/loss denominators. The model-assisted phase
is declared `not_run`, requires three one-response trials where applicable, and
makes zero model calls here.

## Fresh-process replay

Crash cases start a new Python interpreter. A test-only failpoint exits that
child with code 73 after the final canonical JSONL record is flushed and
fsynced, but before SQLite projection. The parent measures the exact raw versus
projected one-event gap, opens the store in a new process context, and requires
replay plus a full projection rebuild to reproduce the same logical rows.

The retention cases now exercise the real deterministic lifecycle. Each first
obtains and records a dry-run plan. `retention-04` terminals the synthetic task
and proves the active/evidenced memory blob remains hot with explicit pin
reasons. `retention-05` creates an exact-scope canonical citation, terminals the
task, archives cited bulk with a verified local Zstandard artifact, evicts
eligible scratch, and then verifies canonical action events, tombstone digests,
bounded excerpts, byte counts, source-event provenance, fresh-process replay,
and projection rebuild. A missing Zstandard primitive is reported
`runtime_blocked`; a missing retention API remains `inconclusive`; neither is
misreported as a candidate rejection or a successful no-op.
The focused store tests separately advance cited bulk through its additional
90-day archive TTL and prove final archive eviction preserves the linked
tombstone history and canonical action chain.

## Run

Run the complete deterministic matrix:

```powershell
py -3 evals\memory\v1\suite.py --output runs\memory-v1-deterministic.json
```

Run one case:

```powershell
py -3 evals\memory\v1\suite.py --case crash-02-side-effect-unknown
```

The aggregate report recomputes all gates from per-case evidence. It records
small-fixture search latency and truthful channel availability. The required
100/1,000/10,000/100,000 corpus latency matrix remains an explicit not-run
phase with no invented production threshold.
