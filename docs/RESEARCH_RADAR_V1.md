# Research Radar v1 operator contract

Research Radar v1 is a continuous, local-first research intake. Once per UTC
day it searches a bounded arXiv window, resolves every discovered paper through
an exact-ID primary request, records immutable base/version identities, checks
known versions on a weekly cycle, verifies explicit GitHub and Hugging Face
metadata links, and emits deterministic metadata-only triage. It never
implements, adopts, rejects, or promotes a paper.

```text
bounded arXiv window -> exact-ID resolution -> immutable versions
                                              |
explicit GitHub/HF URL -> primary metadata ---+
                                              |
                        deterministic triage -> operator decision required
```

PDF/source download, full-text claim extraction, benchmark validation, code
execution, experiments, and recursive improvement are outside this intake.
They remain governed by `RESEARCH_RADAR_DECISION.md`.

## Configuration

`radar-sync` accepts one strict JSON configuration. The installed production
profile is `windows/research-radar.config.json`; a smaller offline evaluator
profile is `tests/fixtures/research_radar/sync_config.json`. Its contract is:

- `categories`: exact arXiv categories used in the daily query;
- `discovery_terms`: bounded literal applicability phrases for coding agents,
  memory/compaction, routing, context, local inference/evaluation, security/tool
  use, and governed research; unsafe query grammar and Boolean tokens fail;
- `triage`: named active questions, observed failures, mutable layers, and
  locally supported categories;
- `budgets`: aggregate requests, bytes, items, artifact checks, request
  timeout, and wall time;
- `cadence`: a 1–168 hour discovery lookback and a 1–30 day exact-version
  recheck interval;
- `verify_artifacts`: whether explicit GitHub/Hugging Face URLs receive bounded
  primary-metadata checks.

`retry_count` must be exactly zero in v1. A scheduler run never turns a network
failure into repeated paid or unbounded work. Categories and term groups are
strictly bounded. Unknown keys fail closed.

Validate a configuration without network or filesystem writes with
`bin\continuity.cmd radar-config-validate --config <path>`. The Windows
installer invokes this same validator before either its dry-run plan or apply.

## Daily command

The command is read-only unless `--apply` is present:

```powershell
bin\continuity.cmd radar-sync `
  --root "$env:LOCALAPPDATA\CodingIntelligence\ResearchRadar" `
  --config D:\path\to\research-radar.config.json
```

An explicit UTC evaluation time makes a run reproducible:

```powershell
bin\continuity.cmd radar-sync `
  --root "$env:LOCALAPPDATA\CodingIntelligence\ResearchRadar" `
  --config D:\path\to\research-radar.config.json `
  --as-of 2026-08-20T17:00:00Z
```

Add `--apply` to persist a successful run. `--offline` forbids network access
and reads only the content-addressed cache for that UTC day. An unavailable
cache is reported as `runtime_blocked_cache_miss`; it is not a paper rejection.

`radar-ingest` and `radar-transition` remain available for bounded exact-ID or
offline fixture intake and explicit operator lifecycle decisions. They are
also dry-run by default.

## Discovery, identity, and rechecks

- The daily arXiv query uses an exact `[start, end)` UTC interval, selected
  categories AND explicit applicability terms, newest-first order, and hard
  frontier/backfill limits. Broad category-only ingestion is not permitted.
- Search results create only a discovery observation. Each base ID is then
  fetched through the official exact-ID API before it can become
  `metadata_verified`.
- Every run reads a newest-first frontier page so fresh research is never
  starved by old backlog. A separate fixed-window cursor spends its own bounded
  allowance on historical backfill. OpenSearch total/start/items are mandatory;
  duplicate version entries fail. A term/category/config change reseeds the
  historical cursor, and a changing result total restarts that fixed window at
  zero rather than trusting shifted offsets.
- Currentness telemetry exposes relevant `arrival_rate_per_day`, frontier and
  total/net backfill capacity, `backlog_count`, conservative
  oldest-unprocessed age, lag, catch-up runs, and `caught_up`. Catch-up uses
  `total capacity - relevant arrival rate`, not raw page size. If relevant
  arrival meets/exceeds capacity while backlog exists, the top-level result is
  `runtime_blocked` and the CLI exits nonzero; a green task can never describe
  an arithmetically permanent backlog as continuous/current.
- The canonical paper identity is its versionless arXiv base ID. Every `vN`
  record is immutable and retains prior-version lineage.
- Known base IDs are rechecked at least weekly under the configured cadence.
  Large catalogs advance through a persistent lexical cursor over multiple
  daily runs rather than exceeding one run's budget. The completed timestamp
  moves only when the cycle reaches the end.
- A daily content-addressed cache stores only responses that passed their
  provider parser and identity checks. A process death before state commit can
  resume from that cache without another request.
- The mutable cadence file is checksummed and atomically replaced after all
  immutable records and the run ledger are durable. Corrupt state is never
  silently reset.
- `metadata_verified` additionally requires an immutable primary receipt bound
  to the exact-ID request/final locator, response and normalized-entry hashes,
  expected IDs, observation, version, and non-public live plan authority.
  Manual/offline `primary_metadata_verified=true` text has zero authority.
  Legacy records remain preserved but unresolved until exact-ID refetch or a
  strictly validated cache replay writes that receipt.

## Artifact verification

Only a syntactically valid repository/model URL explicitly present in the
paper's Atom metadata is eligible. The external text is data: it is never used
as a command, prompt, shell argument, hostname, or installation instruction.
The verifier constructs requests only for three allowlisted primary hosts:

- `api.github.com`: canonical repository identity, default branch, exact commit
  SHA, archive/fork state, and reported license;
- `huggingface.co/api`: canonical model/dataset/space identity, exact Hub
  revision, and reported card/tag license;
- `export.arxiv.org`: discovery and exact paper metadata.

No repository or model content is cloned, downloaded, imported, or executed.
The default network adapter does not follow redirects. HTTP failures, identity
mismatches, malformed JSON, missing
revisions, and missing licenses retain explicit `unverified_*` states. They do
not reject the paper. Provenance distinguishes an explicit paper-metadata URL
from still-unverified author/project ownership.

Artifact candidates enter a bounded, checksummed persistent queue. Selection
advances only after its observation is durable, and already observed identities
do not jump ahead of deferred candidates on the next version-recheck page.
Transient failures receive one later attempt on an explicit bounded date—never
a same-run retry. At 100 failed scheduled attempts the immutable observation is
marked `manual_review_required_max_attempts` and becomes terminal until an
operator revisit or superseding artifact; it cannot silently reset to attempt
zero.

## Deterministic triage

Triage separately reports 0–100 applicability, evidence, feasibility, and
recency scores. The rules and feature hits are stored with the profile hash.
Current evidence and feasibility are deliberately capped because intake has
not inspected full text, validated a benchmark, or measured local compute.

The only automatic suggestions are `experiment`, `watch`, or `manual_review`.
Every record states:

```text
automatic_lifecycle_transition = false
automatic_adoption             = false
automatic_rejection            = false
operator_decision_required     = true
```

Literal term matching is deterministic and treats paper text as untrusted
input. The Radar never interprets instructions found in an abstract.

## Stored records

```text
cache/http/<UTC-day>/<request-id>/...    validated content-addressed responses
papers/<base>/identity.json              stable versionless identity
papers/<base>/versions/vN.json           immutable primary metadata
observations/arxiv/*.json                discovery/exact source observations
authority/arxiv/<base>/vN/*.json         exact-ID primary authority receipts
lifecycle/<base>/vN/*.json               ordered hash-chained lifecycle
artifacts/<provider>/<base>/vN/*.json    verified or truthful-unverified metadata
triage/<base>/vN/*.json                  deterministic metadata-only assessment
sync/runs/*.json                         immutable logical run and budget policy
state/sync-state.json                    checksummed atomic cadence cursor
```

Concurrent applied syncs have one OS-file-lock owner. A second invocation
observes the committed cadence and exits `current`. Immutable writes are
idempotent; conflicting content never overwrites an existing identity.

## Windows current-user schedule

The installer is also dry-run by default:

```powershell
windows\Install-ResearchRadar.ps1 `
  -ConfigPath windows\research-radar.config.json
```

`-ConfigPath` can be omitted to use that adjacent production profile. The plan
performs no directory or Scheduled Task mutation. After review, add `-Apply`.
The installer refuses an elevated process and creates one current-user
task named `Coding Intelligence - Research Radar` with:

- `Interactive` logon and `Limited` run level;
- one exact deployed PowerShell runner action;
- one daily trigger, `StartWhenAvailable`, ten-minute limit;
- `IgnoreNew` multiple-instance policy plus an exact runtime file lock;
- transactional task/file rollback if post-registration identity checks fail.

The runtime pins the sync-config SHA-256. Each invocation records its launcher
PID, start/end, exit code, bounded output, and output hash in `last-run.json`.
It never opens a UAC prompt and does not need administrator rights.

## Live scoped-query proof

On 2026-08-20 the current production query—not the retired broad category-only
query—returned 29 applicability-scoped candidates in its 48-hour window. That is 14.5
results/day against 20/day frontier and 30/day total discovery capacity, with
15.5/day net backfill capacity. The first bounded run retained a nine-item
historical tail; the second processed it and reported `backlog_count=0`,
`caught_up=true`, and `current`. A third same-day Scheduled Task launch made
zero requests and recovered the persisted currentness evidence. All three task
launches ended `Ready`, runner exit `0`, and `LastTaskResult=0`; the next daily
run is scheduled for 06:15 local time. No automatic adoption or rejection
authority was enabled.

The scheduler does not import mutable repository source. The installer stages
the runner, dedicated stdlib-only entrypoint, sync config, `__init__.py`, and
`research_radar.py` under the
current-user runtime; parses/self-checks them; hashes every deployed file; then
atomically swaps the runtime and registers one exact Limited daily task. The
previous runtime and task XML are last-known-good rollback until post-install
action/principal/single-trigger/hash verification succeeds. The runner verifies
all five deployed file hashes plus the manifest's own hash before every import.

A live isolation probe changed the repository `research_radar.py` hash while
the deployed module remained unchanged. The task still ran only the deployed
package, returned `current` with zero requests, and ended `LastTaskResult=0`;
the source edit was then removed and source/deployed hashes matched again.
Repository edits therefore have no effect until the explicit transactional
installer is rerun.

The deployed package deliberately contains only `__init__.py` and
`research_radar.py`; it does not include the general continuity CLI, Memory, or
routing modules. A second live probe added an unrelated package source file:
the five-file deployed manifest remained identical, the file was absent from
the runtime, and the task remained `current`/`LastTaskResult=0` with zero
requests. The probe file was then removed.

## Honest remaining boundary

Research Radar v1 is a trustworthy metadata intake and prioritization rail, not
autoresearch. A paper can move beyond triage only through a separate immutable
experiment capsule with declared compute/time/token/storage budgets, a
validated evaluator, repeated held-out results, an independent verifier, and
an explicit operator disposition. Evidence-valid full-text claim extraction is
deferred rather than approximated with keyword summaries.

V1 also has a deliberate finite operational horizon: immutable cache, run, and
triage records have no governed retention/GC yet, and catalog scanning stops at
10,000 paper identities rather than becoming unbounded. Storage retention,
content-addressed cache GC, and indexed catalog traversal must land before that
horizon; no current claim extends beyond it.
