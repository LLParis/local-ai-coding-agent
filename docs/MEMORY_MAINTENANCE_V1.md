# Memory v1 maintenance and rehydration

Date: 2026-08-20

## Outcome

Memory v1 has one deterministic maintenance owner. Every run first
produces a retention plan and dry-run result. No physical GC occurs below 80%
effective pressure. At or above 80%, physical application still requires the
explicit `--apply` path and is capped by item count, hot bytes released, and
wall time. The effective pressure is the greater of Memory v1 hot-blob budget
usage and the containing volume's measured usage.

The implementation is
`src/agent_continuity/memory_maintenance.py`. It performs no network or model
calls. Each owned run writes one canonical, digest-bearing, fsynced evidence
record beneath `<memory-root>/maintenance/runs/`. A kernel-released run lock and
the Scheduled Task's `IgnoreNew` policy provide single-instance ownership.

Default bounds are:

- 25 eligible payloads;
- 1 GiB of hot payload bytes;
- 30 seconds inside the Python owner;
- five minutes as the independent Windows Scheduled Task execution limit.

Canonical ceilings apply below every caller, including direct Python and CLI
use: at most 100 items, 1 TiB of hot bytes, 300 seconds inside one Python run,
a 100 TiB configured hot-blob budget, and 30 seconds waiting for the exact run
lock. Mutation flags require literal booleans; strings such as `"false"`,
numbers, nulls, and containers are rejected before root creation or planning.

Eligibility remains owned by the retention projection. Exact scope filters,
active tasks, active/disputed memories, authoritative evidence, active task
state, production claims, and `core` retention all remain protected. The runner
does not enumerate or delete arbitrary filesystem paths.

`load_maintenance_evidence(...)` independently validates the complete report
schema, canonical bytes, self-hash, UUID/filename/locator identity, exact
resolved Memory root, pressure arithmetic, limits, status-dependent execution,
and retained event identities. Unknown/missing fields, malformed/noncanonical
JSON, path/root mismatch, and content or digest mutation are rejected. Store,
maintenance, and Windows owners reject pre-existing symlinks, junctions,
reparse points, and internal hard links before opening event, SQLite, lock,
module, runner, or evidence paths. A hostile external swap after the final
check remains an operating-system boundary, not a claimed guarantee.

## Canonical rehydration

An evicted digest cannot return merely because matching bytes are appended
again. `MemoryStore.rehydrate_blob(...)` requires all of the following:

- exact expected SHA-256 and byte count;
- exact latest eviction tombstone SHA-256;
- the complete exact scope-key set;
- the complete source-event provenance set;
- typed byte provenance with a matching source digest.

The owned lifecycle is:

```text
validate identity and current eviction
        -> fsync event-owned stage
        -> append and fsync blob/rehydrated
        -> atomically promote exact bytes
        -> project availability=hot
```

The tombstone is never removed or rewritten. A later eviction chains to the
previous tombstone, and rebuild orders archive, eviction, and rehydration by
those causal links rather than trusting wall-clock order.

Fresh-process reconciliation covers every interruption boundary:

- stage without a canonical event: remove only that orphan stage;
- canonical event plus stage: replay promotes it;
- canonical event plus hot bytes but stale projection: replay verifies and
  marks it hot;
- conflicting, missing, cross-scope, stale-tombstone, or digest-mismatched
  input: fail closed without resurrection.

Two concurrent rehydration attempts serialize on the canonical store lock. One
may append the lifecycle event; the other observes that the target is no longer
evicted and is rejected.

## Windows current-user owner

The exact task name is `Coding Intelligence - Memory Maintenance`.

`windows/Install-MemoryMaintenance.ps1` is a no-write plan unless `-Apply` is
present. Apply refuses an elevated session, deploys the finite Python module
set plus `windows/Run-MemoryMaintenance.ps1`, and registers only the current
user with:

- `RunLevel=Limited`;
- `LogonType=Interactive`;
- `MultipleInstances=IgnoreNew`;
- one exact hidden PowerShell action;
- one bounded repeating trigger;
- no stored password and no UAC path.

`windows/Test-MemoryMaintenanceTask.ps1` independently reads back the exact
current-user SID, run/logon levels, action, working directory, one enabled
trigger, start boundary, repetition interval, `IgnoreNew`, enabled state, and
five-minute execution limit. Deployed Python, runner, and verifier sources are
hash-bound in the runtime configuration before use.

Example planning command:

```powershell
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\windows\Install-MemoryMaintenance.ps1 `
  -MemoryRoot 'D:\path\to\synthetic-memory-v1'
```

Add `-Apply` only after the returned action, current-user SID, root, and bounds
are correct. The installer verifies the installed principal, action, working
directory, and single-instance setting and rolls back its exact task/files if
verification fails.

Rollback removes only the replaceable task/runtime owner; it never deletes the
Memory root:

```powershell
Unregister-ScheduledTask `
  -TaskName 'Coding Intelligence - Memory Maintenance' `
  -Confirm:$false
```

After confirming the resolved target remains exactly below current-user
`LOCALAPPDATA\CodingIntelligence`, the deployed runtime directory may also be
removed and regenerated by the installer. Preserve
`%LOCALAPPDATA%\CodingIntelligence\MemoryV1`; its events and evidence are the
asset, not uninstall debris.

## Evidence status

- Deterministic retention baseline reproduced before extension: 29 focused
  tests plus 92 subtests.
- Maintenance/rehydration controls cover malformed authority and over-limit
  zero-write behavior, sub-threshold dry run, exact 80%
  activation, scope/item/byte/wall bounds, canonical evidence, equivalent valid
  provenance, whitespace/control rejection, content mutation, thread/process
  duplicate rehydration, causal re-eviction/rebuild, and all crash locations.
- Current-source synthetic lifecycle evidence is recorded in
  `runs/memory-maintenance-scheduler-proof.json`: Limited current-user task,
  exact trigger/action/settings, ValidateOnly plus scheduled below-pressure
  runs, zero canonical events/blobs, then exact task/root cleanup.
- The canonical empty current-user root is now initialized at
  `%LOCALAPPDATA%\CodingIntelligence\MemoryV1`; no unrelated data was imported.
  `Coding Intelligence - Memory Maintenance` remains installed and `Ready`
  under the current user with `Limited`, `Interactive`, `IgnoreNew`, one hourly
  trigger, and a five-minute task cap. Its first scheduled run returned `0` at
  1,636 basis points effective pressure, performed no GC, and left the root at
  zero canonical events/blobs. Exact hashes and read-back evidence are in
  `runs/memory-maintenance-production-proof.json`.
