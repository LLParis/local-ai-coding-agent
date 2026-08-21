# Coding Intelligence: zero-to-operator curriculum

Status date: 2026-08-20

This is the learning and operating path for the sovereign Coding Intelligence
platform. Start with **Level 0** and the everyday workflow. The later levels
teach enough of the implementation to evaluate models, harnesses, memory, and
research ideas instead of trusting product names or benchmark claims.

Mobile source: [LLParis/local-ai-coding-agent](https://github.com/LLParis/local-ai-coding-agent).
The [2026 research-to-implementation map](RESEARCH_TO_IMPLEMENTATION_2026.md)
shows which current papers changed the live system and which remain post-live.

The repository is currently version `0.1.0`. The title "v1" names this
operator contract; it is not a claim that every candidate route has completed
the final tournament.

## Level 0: the whole system in one picture

```text
                         task request
                              |
                       router / policy
                        /           \
        hosted route (available)   sovereign route (local)
                 |                        |
              harness -------- gives a model tools and a loop
                 |                        |
               model --------- supplies diagnosis and proposals
                 |                        |
          isolated stage on the selected execution edge
                 |
        real project test + independent verifier
                 |
       evidence + durable Memory; verified apply by default
```

The nouns are deliberately separate:

| Part | Job | Current example |
|---|---|---|
| Model | Supplies learned reasoning and code generation | Qwen3.8, Devstral, Laguna |
| Harness | Supplies context, tools, the agent loop, budgets, and event protocol | the thin runner, Pi, DeepSeek Harness, hosted Codex |
| Router | Selects an evidence-qualified composition for the stated task and live runtime | `src/agent_continuity/routing.py` |
| Verifier | Tries to disprove the proposed outcome independently | hidden project tests, then Devstral review |
| Memory | Preserves events, typed state, evidence, and retrievable facts beyond one context window | `%LOCALAPPDATA%\CodingIntelligence\MemoryV1` |
| Inference edge | Runs model weights | EXCALIBUR and, only for emergency triage, the Mac |
| Execution edge | Runs repository tools and builds | EXCALIBUR for ordinary work; Mac for Xcode/Swift |
| Stage | Isolated copy in which an edit is tested; retained on failure or `--stage-only`, cleaned after verified apply | the `stage` path in a result |

Pi is therefore a **harness**, not a model. DeepSeek Harness and Codex are also
harness/control environments. The intended fusion is the Coding Intelligence
control plane: it can keep the best mechanism from each without pretending that
one entire upstream product must win.

## Current truth map

Use these categories literally. "Installed" is not "qualified," and
"qualified" is not "wired into the everyday dispatcher."

| Capability | Current evidence-backed state |
|---|---|
| EXCALIBUR bounded Qwen lifecycle | Production-proven: one current-user Limited Scheduled Task owns one loopback server and exact child; normal switching has no UAC |
| Sovereign bounded implementation | Accepted for scoped Python, TypeScript, and PowerShell work; the everyday default loop runs Qwen3.8-27B Q6 Native 262K, and the bounded 32K `Qwen38` profile remains the task-file lane default |
| Independent local review | Devstral Small 2 is the accepted local verifier; project tests remain factual authority |
| Mac control edge | Production-proven transport and Codex SSH-host lifecycle; full Xcode is selected and Swift 6.3.3 is live |
| Sovereign Swift evidence | The task-file lane supports `execution_verifier: "mac-swift"`; its Qwen implementation → Mac Swift/Xcode test → Devstral path is live-proven |
| Qwen native 262K substrate | Accepted by `runs/qwen38-q6-native-long-context-qualification.json`: 60/60 unique runs and 1,128/1,128 scorer checks with zero retries/timeouts |
| Native route in the routing manifest | Accepted with the native 262K and real DeepSeek production evidence pinned in `config/routing-v1.json` |
| Durable Memory core | Deterministic 40-case suite accepted, including retrieval, scope, temporal truth, compaction checks, crash replay, and retention operations |
| Memory model-assisted/scale research | Model-assisted synthesis and the 100–100,000-item latency matrix remain post-live research, not blockers to durable production Memory |
| Memory maintenance | Limited current-user hourly owner is live-proven; bounded archive/eviction/rehydration behavior is proven in deterministic controls |
| Research Radar | Production-proven bounded daily metadata intake/currentness rail; it cannot implement, accept, reject, or adopt research automatically |
| Pi adapter | Pinned four-tool adapter and lifecycle contract tested against fake endpoints; live model qualification pending |
| DeepSeek Harness | Default production loop driving the Qwen3.8-27B Q6 Native 262K backend through the everyday `bin\coding.cmd` repository-objective command |
| Laguna XS 2.1 | Exact Q4_K_M artifact is verified; runtime/template/tool/task qualification pending |
| Model/harness tournament | Parked post-live research; the production router now selects the accepted DeepSeek/Qwen native route |
| Verified promotion into user work | The everyday `coding.cmd` path applies a verified result by default and cleans its stage; `--stage-only` preserves operator control. The task-file lane remains operator-controlled. |

The authoritative design/status references are [the production
handoff](PRODUCTION_HANDOFF_V1.md), [Routing v1](ROUTING_V1.md), [Memory
maintenance](MEMORY_MAINTENANCE_V1.md), [Research Radar v1](RESEARCH_RADAR_V1.md),
and [the harness fusion decision](HARNESS_FUSION_DECISION.md). Current code,
processes, listeners, artifacts, and results take precedence if a prose date has
drifted.

## Level 1: everyday operation

Run commands from the repository on EXCALIBUR:

```powershell
Set-Location 'D:\11_CS\00_REPOS\coding-intelligence-command-center'
bin\doctor.cmd
```

If the result is `Overall: READY`, the normal bounded backend has exactly one
owned loopback listener. `doctor` is read-only: it performs no inference,
download, service mutation, or elevation. Machine-readable output is:

```powershell
bin\doctor.cmd -Json
```

### The everyday repository-objective command

The everyday command takes a repository path and a plain objective; no task
file is required:

```powershell
bin\coding.cmd D:\11_CS\00_REPOS\my-project "Fix the described defect without changing tests."
```

The evidence-bound router selects DeepSeek Harness and the exact owned
`Qwen38Native` backend (Qwen3.8-27B Q6, native 262K) without UAC. The worker
retrieves relevant workspace Memory, continues the selected `--task-key` in a
fresh stage, and supplies dedicated file tools plus workspace-write PowerShell
for staged Git/build/diagnostic commands on full-repository jobs. Focused
`--scope` jobs use only dedicated scoped tools without PowerShell. The
repository's real command verifies
the staged result; materially new failure evidence allows at
most two targeted repairs, and an identical failure stops the run. A verified
result is applied to the source by default and its stage is cleaned;
`--stage-only` retains the stage without touching the workspace. After a passing run the
worker switches to Ollama and sends the objective and diff to
`devstral-small-2:24b` exactly once as an advisory reviewer; deterministic
execution remains final authority. All inference backends are then unloaded so
idle Coding Intelligence does not consume workstation VRAM.

Repeat `--scope` to focus a job on named repository-relative paths; without it
the job covers the full repository:

```powershell
bin\coding.cmd D:\11_CS\00_REPOS\my-project "Finish the focused production job" `
  --scope src\parser.py --scope docs --stage-only
```

The final JSON records scope, route, retrieved Memory, DeepSeek parent lineage,
and execution edge. Reuse `--task-key feature-name` for one continuing work
stream. `--harness qwen-code` is an explicit alternate. A UTF-8 JSON argv file
passed with `--verify-command-file` overrides verification for an unusual
project without batch-shell quoting problems. Automatic Apple execution routes
Swift package test/build, one unambiguous shared-scheme Xcode build, and plain
Swift typecheck to the trusted Mac. Swift-package execution is live-proven;
Xcode and plain-Swift modes are source-supported pending matching real projects.
There are no automatic model retries.

### Describe one task

The task-file lane below is retained for one-response scoped edits; everyday
repository work uses the command above. Create a task file from the checked-in
template:

```powershell
Copy-Item .\task.example.json .\my-task.json
```

Edit only the values in `my-task.json`:

```json
{
  "schema_version": 1,
  "backend": "Qwen38",
  "workspace": "D:\\11_CS\\00_REPOS\\my-project",
  "objective": "Fix the parser defect with the smallest complete edit. Preserve unrelated behavior and do not edit tests.",
  "mutable": ["src/parser.py"],
  "context": ["src", "AGENTS.md"],
  "verify_context": ["tests"],
  "test_command": ["py", "-3", "-m", "unittest", "discover", "-s", "tests", "-v"],
  "timeout": 180
}
```

- `mutable` is the exact one-to-four-file edit surface.
- `context` is visible to the model.
- `verify_context` is copied into the stage but hidden from the model prompt.
- `test_command` is the real project command, represented as an argument array.
- `backend` should be `Qwen38` for the accepted local path. `Ollama` is an
  explicit alternate-model trial, not automatic routing.

Validate the capsule without a model call, backend change, or Memory write:

```powershell
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\bin\coding-task.ps1 -Task .\my-task.json -PlanOnly
```

A valid plan says `status: planned`, `modelCalls: 0`, and
`automaticRetries: 0`.

Run the task and retain the one-line JSON result:

```powershell
bin\coding-task.cmd .\my-task.json | Tee-Object -FilePath .\my-task.result.json
```

Read the outcome:

```powershell
$result = Get-Content .\my-task.result.json -Raw | ConvertFrom-Json
$result | Select-Object status,taskId,sessionId,modelCalls,automaticRetries
$result.edit | Select-Object diagnosis,test_exit,stage,trajectory
$result.verifier | Select-Object status,verdict,reason,risks
$result.restore
$result.memory
```

### What one coding run actually does

```text
validated request
  -> durable task intent
  -> exact backend switch
  -> one Qwen structured-edit response
  -> scoped edit in retained stage
  -> runner-owned real test
  -> one Devstral verdict only after a passing test
  -> bounded Qwen restore
  -> terminal evidence and Memory state
```

There are zero automatic retries. A complete accepted run normally makes two
local model calls: one implementer and one verifier. The source workspace is
not changed by this command.

### Promote one verified result

This manual promotion path belongs to the task-file lane. The everyday
`bin\coding.cmd` command applies a verified result to the source by default,
so it needs no manual promotion; `--stage-only` keeps a verified diff in the
retained stage instead.

`verified` means the staged diff passed the named test, Devstral accepted it,
the default backend was restored, and terminal Memory was recorded. It does
not mean that an inadequate test magically proved every possible behavior.

For a Git workspace, first materialize and check the exact patch:

```powershell
$result = Get-Content .\my-task.result.json -Raw | ConvertFrom-Json
if ($result.status -ne 'verified') { throw 'Refusing to promote an unverified result.' }
$task = Get-Content .\my-task.json -Raw | ConvertFrom-Json
$patch = Join-Path $PWD 'my-task.patch'
[IO.File]::WriteAllText($patch, [string]$result.edit.diff, [Text.UTF8Encoding]::new($false))
git -C ([string]$task.workspace) apply --check -- $patch
```

Inspect `edit.diff`, `edit.test_output`, the verifier reason, and the retained
stage. If they are correct and the original workspace has not drifted:

```powershell
git -C ([string]$task.workspace) apply -- $patch
Push-Location ([string]$task.workspace)
try {
  $testExe = [string]$task.test_command[0]
  $testArgs = @($task.test_command | Select-Object -Skip 1)
  & $testExe @testArgs
}
finally { Pop-Location }
git -C ([string]$task.workspace) diff --check
git -C ([string]$task.workspace) diff
```

Do not force an old patch onto changed source. Make a fresh task from the
current workspace instead.

## Cloud available and cloud exhausted

### Hosted access available

Use hosted Codex for architecture, ambiguous diagnosis, research synthesis,
and final arbitration. It may still delegate a bounded implementation to the
local command when that saves hosted usage. The current pure router can model
this choice, but it is **not yet connected to `coding-task` as an automatic
dispatcher**.

### Hosted access exhausted or deliberately unavailable

The sovereign path requires no hosted model call:

```powershell
bin\doctor.cmd
bin\coding.cmd D:\11_CS\00_REPOS\my-project "Fix the described defect."
```

DeepSeek Harness drives Qwen3.8-27B Q6 Native 262K, the project test checks
behavior, and Devstral reviews as an advisory second opinion. If a task
exceeds the accepted surface—unsupported tools/language, ambiguous broad
architecture, or more than the allowed mutable set—the truthful outcome is to
stop or return unroutable, not silently claim frontier-equivalent judgment.

Automatic hosted-to-local handoff and additional specialist models remain
post-live improvements. Until automatic handoff is added, choose the local
command explicitly when hosted access is unavailable.

## Reading `doctor` correctly

| Field | Meaning |
|---|---|
| `Configured: ready` | Required Scheduled Tasks and core artifacts exist |
| `Tested: evidence-recorded` | Historical real-task evidence exists; doctor did not rerun it |
| `Live: ready` | Exactly one owned loopback backend satisfies current non-generating health checks |
| `Live: idle-ready` | Inference is intentionally unloaded; the on-demand tasks are installed and ready |
| `Active backend: Off` | Normal idle state; no model owns GPU memory or a loopback inference port |
| `Active backend: Qwen38` | Normal bounded Qwen is serving on `127.0.0.1:8818` |
| `Active backend: Qwen38Native` | The qualified 262K profile is serving, normally only for explicit work |
| `Active backend: Ollama` | The alternate model/verifier backend is serving on `127.0.0.1:11434` |
| Scheduled Task `Ready` | Standby, not failure |
| Scheduled Task `Running` | This backend currently owns its wrapper and child |
| `Conflict` | More than one backend/task/listener claims active ownership |
| `stopped-or-unhealthy` | No backend passed task, listener, health, model, and owner checks together |

The normal idle-ready state is `Off`, with all model tasks in standby. A coding
command starts its selected backend without UAC and unloads it again when the
run ends. Raw inference endpoints are loopback-only while active.

## Exact troubleshooting without UAC or broad kills

Normal repair uses the already-installed current-user Scheduled Tasks:

```powershell
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\windows\Switch-ExcaliburBackend.ps1 -Backend Off
bin\doctor.cmd -Json | Tee-Object .\doctor-after-switch.json
```

Rules:

1. If `doctor` is ready, do not repair anything.
2. For `Conflict` or `stopped-or-unhealthy`, make **one** exact switch to
   `Qwen38`, then run doctor once.
3. If the same failure remains, stop repeating it. Preserve the JSON and
   inspect the named task, listener PID, executable, parent, owner record, and
   error as one identity chain.
4. Never use `Stop-Process -Name python`, `taskkill /IM`, or another broad kill.
5. If an everyday doctor, switch, or coding task displays UAC, cancel it. The
   normal tasks run as the current user with `RunLevel=Limited`; an installer or
   wrong execution context was invoked.
6. `windows\Install-*` scripts are installation/update paths, not everyday
   recovery commands.

Inspect the three maintenance owners without changing them:

```powershell
$names = @(
  'Coding Intelligence - Codex Catalog Maintenance',
  'Coding Intelligence - Memory Maintenance',
  'Coding Intelligence - Research Radar'
)
foreach ($name in $names) {
  Get-ScheduledTask -TaskName $name
  Get-ScheduledTaskInfo -TaskName $name
}
```

## Memory: search, working sets, compaction, and retention

### What Memory preserves

The append-only hash-chained JSONL stream is authority. SQLite is a rebuildable
search projection. Typed state records the objective, constraints, criteria,
decisions, blockers, unknown tool outcomes, next actions, and artifact hashes.
Large content is stored once by digest. Compaction appends a checked derived
state; it does not erase raw history.

Every ordinary `coding-task` records its own intent and terminal outcome. Check
the operational store:

```powershell
$memoryRoot = Join-Path $env:LOCALAPPDATA 'CodingIntelligence\MemoryV1'
bin\continuity.cmd memory-verify --root $memoryRoot
```

Use `memory-rebuild` only to reconstruct the SQLite projection from verified
canonical events—not to rewrite history:

```powershell
bin\continuity.cmd memory-rebuild --root $memoryRoot
bin\continuity.cmd memory-verify --root $memoryRoot
```

### Search one completed run with exact scope

The search API never treats a missing scope field as a wildcard. Build the
exact scope from the task result and workspace identity:

```powershell
$result = Get-Content .\my-task.result.json -Raw | ConvertFrom-Json
$task = Get-Content .\my-task.json -Raw | ConvertFrom-Json
$workspace = [IO.Path]::GetFullPath([string]$task.workspace).ToLowerInvariant()
$bytes = [Text.Encoding]::UTF8.GetBytes($workspace)
$sha = [Security.Cryptography.SHA256]::Create()
try { $workspaceId = 'sha256:' + [Convert]::ToHexString($sha.ComputeHash($bytes)).ToLowerInvariant() }
finally { $sha.Dispose() }
$scope = [ordered]@{
  owner_id = 'local-owner'
  workspace_id = $workspaceId
  task_id = [string]$result.taskId
  agent_role = 'implementer'
  session_id = [string]$result.sessionId
}
[IO.File]::WriteAllText(
  (Join-Path $PWD 'memory-scope.json'),
  ($scope | ConvertTo-Json -Compress),
  [Text.UTF8Encoding]::new($false)
)
bin\continuity.cmd memory-search `
  --root $memoryRoot `
  --query 'parser verified outcome' `
  --scope .\memory-scope.json `
  --top-k 8
```

Retrieve one returned record by provenance-bearing ID:

```powershell
bin\continuity.cmd memory-get --root $memoryRoot --memory-id "episode:$($result.taskId)"
```

Pack typed state first and retrieved Memory second with the live model's exact
tokenizer:

```powershell
bin\continuity.cmd memory-pack `
  --root $memoryRoot `
  --task-id $result.taskId `
  --request 'Resume the verified parser task from its recorded outcome.' `
  --scope .\memory-scope.json `
  --tokenize-url http://127.0.0.1:8818/tokenize
```

`--offline-counter whitespace-v1` exists for deterministic tests only; do not
call it a production token count.

### Compaction

Compaction is an advanced two-phase operation, not an everyday summary button:

```powershell
bin\continuity.cmd compaction-validate `
  --previous .\previous-state.json `
  --candidate .\candidate-state.json `
  --source-range .\frozen-source-range.json `
  --citations .\citation-manifest.json
```

Only an accepted deterministic result is sent to an independently bound
semantic verifier. Its report must name the candidate digest and exactly the
`semantic_omissions` and `semantic_contradictions` checks. Then:

```powershell
bin\continuity.cmd compaction-commit `
  --previous .\previous-state.json `
  --candidate .\candidate-state.json `
  --source-range .\frozen-source-range.json `
  --citations .\citation-manifest.json `
  --semantic-verifier .\semantic-verifier.json `
  --output .\state-revisions\revision-0002.json
```

This CLI writes one immutable state file after both approvals. The everyday
worker separately uses live routing, workspace Memory retrieval, DeepSeek
compaction, and fresh-stage session lineage automatically.

### Retention and rehydration

The hourly current-user owner plans every run. Below 80% effective pressure it
does no physical GC. At or above 80%, its explicit configured apply path is
bounded by items, bytes, and time. It protects active tasks/state, active or
disputed memories, authoritative evidence, production claims, and `core`
records. Archive/eviction leaves canonical lifecycle evidence and tombstones;
rehydration requires exact bytes, digest, tombstone, scope, and provenance.

Inspect the latest immutable maintenance result:

```powershell
$maintenanceRuns = Join-Path $memoryRoot 'maintenance\runs'
$latest = Get-ChildItem -LiteralPath $maintenanceRuns -File |
  Sort-Object LastWriteTimeUtc -Descending |
  Select-Object -First 1
Get-Content -LiteralPath $latest.FullName -Raw | ConvertFrom-Json
```

Do not manually delete blobs, JSONL segments, tombstones, the SQLite file, or
archive records.

## Research Radar: from a paper to a governed experiment

The installed daily task searches a bounded applicable arXiv window, resolves
exact paper IDs, verifies explicit GitHub/Hugging Face metadata, rechecks known
versions, and emits deterministic triage. Paper abstracts are untrusted input.
Radar does not execute repository code or grant adoption authority.

Inspect the latest owned run:

```powershell
$radarLast = Join-Path $env:LOCALAPPDATA 'CodingIntelligence\ResearchRadarRuntime\last-run.json'
$run = Get-Content -LiteralPath $radarLast -Raw | ConvertFrom-Json
$run.summary | ConvertTo-Json -Depth 8
```

Healthy currentness requires `status: current`, `caught_up: true`, a bounded
request/byte ledger, and zero automatic adoption/rejection authority. A green
Scheduled Task alone is not enough.

Validate the production configuration without network or writes:

```powershell
bin\continuity.cmd radar-config-validate `
  --config .\windows\research-radar.config.json
```

Preview a cycle without persistence:

```powershell
bin\continuity.cmd radar-sync `
  --root "$env:LOCALAPPDATA\CodingIntelligence\ResearchRadar" `
  --config .\windows\research-radar.config.json
```

`--apply` is an explicit mutation. `--offline` allows only that day's validated
cache; a cache miss is `runtime_blocked`, not a paper rejection.

Use Radar in this order:

1. Read the exact versioned metadata and artifact receipt.
2. State the relevant claim as a falsifiable hypothesis.
3. Decide whether it maps to an observed failure or active research question.
4. If it does, create a finite experiment capsule with a pinned implementation,
   evaluator controls, baseline, compute/time budget, and stopping condition.
5. Preserve `accepted`, `rejected`, `inconclusive`, `evaluator_invalid`, and
   `runtime_blocked` as different outcomes.
6. Adopt only after repeated applicable evidence and an explicit operator
   decision.

Research Radar v1 deliberately does not perform full-text evidence extraction
or autoresearch. Those remain governed next layers.

## The Mac Apple edge

From EXCALIBUR, verify the primary without changing it:

```powershell
ssh -o BatchMode=yes -o ConnectTimeout=10 coding-intelligence-mac `
  '~/.local/libexec/coding-intelligence/codex-ssh-host-primary.sh --check'
ssh -o BatchMode=yes -o ConnectTimeout=10 coding-intelligence-mac `
  'xcode-select -p; swift --version | head -n 2'
```

The healthy response has one LaunchAgent-owned supervisor, one exact app-server
child, one Unix socket owner, and zero TCP listeners. Xcode should resolve to
`/Applications/Xcode.app/Contents/Developer`.

The Mac is the execution edge for Xcode, SwiftUI, signing, simulators, and Apple
tools; EXCALIBUR remains the inference command center. One sovereign Swift
fixture has proven this composition in `runs/swift-mac-edge.json`.

For ordinary Swift packages, use `bin\coding.cmd` normally; `Package.swift`
selects the accepted DeepSeek+Mac route automatically. The exact task-file
`execution_verifier: "mac-swift"` lane remains available for manually bounded
capsules. Both paths use pinned Mac/Xcode/SSH identities, return bound output
and source hashes, clean the exact remote temporary root, and then perform the
normal Devstral/final-Memory steps.

## Failure and verdict language

### Ordinary coding-task outcomes

| Result | Meaning | Operator action |
|---|---|---|
| `verified` | Staged project test passed, Devstral accepted, backend restored, Memory finalized | Inspect evidence; optionally promote and rerun test in source |
| `rejected` | Test passed but independent review found a concrete mismatch/risk | Do not promote; inspect verifier reason |
| `failed` | Model contract, edit, test, verifier execution, restore, or Memory finalization failed | Diagnose the exact failed phase; do not blindly retry |
| `outcome_unknown` | A durable model/tool intent exists without an observed terminal result | Inspect effects before any replay |

### Research/qualification outcomes

| Verdict | Meaning |
|---|---|
| `accepted` | A valid applicable evaluator and cited evidence support this role |
| `rejected` | A valid applicable evaluator disproved this role/task hypothesis |
| `inconclusive` | Evidence is insufficient or mixed; it is not rejection |
| `evaluator_invalid` | The test/request/scorer was wrong or contaminated; it does not count against the candidate |
| `runtime_blocked` | The intended trial could not run on its pinned substrate; it does not count as a model loss |
| `not_run` | No applicable trial has occurred |

Audit an unexpected failure once for prompt omission, stale fixtures,
overconstrained scoring, runtime mismatch, leakage, and verifier defects. Audit
an unexpected success for leakage and weak tests with equal seriousness.

## Evidence and logs

| Question | First place to look |
|---|---|
| Is a backend live and exactly owned? | `bin\doctor.cmd -Json` |
| What did one task-file run see/change/test? | `edit.trajectory` and `edit.stage` from its result |
| What did one everyday `coding.cmd` run see/change/test? | `trajectory`, `stage`, `scope`, and `focused` fields from its JSON result |
| What was durably recorded? | `%LOCALAPPDATA%\CodingIntelligence\MemoryV1\events`, `memory.sqlite3`, and `memory-verify` |
| Did native Qwen pass long context? | `runs/qwen38-q6-native-long-context-qualification.json` |
| Did the Mac Swift route pass? | `runs/live-foundation-completion.json` and `runs/swift-mac-edge.json` |
| Is memory maintenance healthy? | newest `%LOCALAPPDATA%\CodingIntelligence\MemoryV1\maintenance\runs\*.json` |
| Is Radar current? | `%LOCALAPPDATA%\CodingIntelligence\ResearchRadarRuntime\last-run.json` |
| What routes are presently authorized? | `config/routing-v1.json` plus every pinned evidence hash |
| What proved the production DeepSeek harness? | `runs/live-foundation-completion.json` |

Treat repository docs as claims until their cited artifact, process, listener,
hash, and task state agree.

## Adding a model, harness, or research idea

Use one shared promotion discipline:

1. **State the job.** A model may be an implementer, verifier, knowledge expert,
   vision expert, or fast diagnostic. A harness may improve tool use without
   changing model intelligence.
2. **Pin the substrate.** Record exact weights/digest, quant, context/KV,
   runtime build, template, reasoning/sampling, harness commit, tools, and edge.
3. **Freeze a real task.** Include the model-visible request, mutable scope,
   hidden behavioral verifier, time/call budget, and stopping condition.
4. **Validate the evaluator first.** Require positive, negative, malformed,
   semantic-equivalence, mutation, and evaluator-invalid controls.
5. **Preflight without inference.** Parse fixtures, prove exact ownership,
   confirm source hashes, and ensure result aggregation retains failures.
6. **Run one response per trial.** Zero automatic retry; retain every terminal
   artifact and distinguish runtime/evaluator failure from candidate failure.
7. **Repeat representative tasks.** Diagnosis → scoped edit → real test →
   independent verdict matters more than a health prompt or vendor benchmark.
8. **Compare like with like.** The best fully qualified single expert is the
   baseline. A team/fusion route wins only with a positive matched gain over
   that exact expert.
9. **Update authority deliberately.** Revise the routing manifest and evidence
   hashes, rerun its mutation/selection tests, and keep prior candidates and
   invalid trials visible.
10. **Teach the operator change.** Document when the new route is selected,
    its costs/limits, how it fails, and how to inspect its evidence.

Tournament V2 is a parked post-live scientific asset. It does not authorize or
block the live `coding.cmd` command.

## Level 2–6: learn backward from operation to frontier research

This path makes the working system the laboratory, then fills in fundamentals.
Advance by artifacts you can explain and reproduce, not hours watched.

### Level 2 — operating foundations

Learn PowerShell argument arrays, processes/parent-child ownership, ports and
loopback, Scheduled Tasks, SSH/Tailscale, Git diff/apply, JSON schemas, file
hashes, Python virtual environments, and unit-test exit codes.

Deliverables:

- explain every field in one `doctor -Json` result;
- trace one process from task → wrapper → child → listener;
- plan and run one bounded task, explain its diff/test/verdict, and promote it;
- verify Memory and explain why rebuilding SQLite does not rewrite authority.

### Level 3 — local inference and model systems

Learn transformers at a conceptual level, tokenization, prefill versus decode,
attention/KV cache, dense versus mixture-of-experts models, quantization,
sampling/reasoning controls, tool schemas, chat templates, GPU residency, host
RAM spill, and why advertised maximum context is not usable-context proof.

Deliverable: reproduce one pinned profile description from artifact through
runtime/template/context settings and explain each performance/quality tradeoff.

### Level 4 — agent and continuity engineering

Learn the observe/decide/tool/result loop, harness versus model attribution,
side-effect idempotency, exact ownership/cancellation, append-only event
sourcing, typed projections, lexical/trigram/vector retrieval tradeoffs,
temporal supersession, provenance, context packing, compaction invariants, and
fresh-process replay.

Deliverable: diagram one complete coding run and one crash boundary, including
why an unmatched side effect becomes `outcome_unknown` rather than replayed.

### Level 5 — evaluation and experimental design

Learn falsifiable hypotheses, baselines, treatments, held-out fixtures,
mutation testing, semantic-equivalence controls, leakage, repeated trials,
confidence intervals, paired comparisons, ablation studies, factorial designs,
latency/quality tradeoffs, and evaluator validity.

Deliverables:

- design a six-control evaluator before calling a model;
- analyze the 60-run long-context aggregate without confusing throughput with
  correctness;
- run a small matched harness or model experiment and produce an immutable
  keep/discard ledger with honest terminal verdicts.

### Level 6 — frontier research practice

Use Research Radar as discovery, then read primary papers and source. Build a
claim/evidence table, identify the mutable system layer, reproduce the baseline,
run a finite experiment, independently verify the result, and publish enough
artifacts for falsification. Contribute corrections, evaluators, or mechanisms
upstream before claiming a new architecture.

Suggested research sequence:

1. exact/temporal/hybrid retrieval under 100–100,000-item Memory scale;
2. loss-checked compaction versus larger raw context at fixed task quality;
3. production DeepSeek versus Pi/Qwen Code mechanisms on questions raised by
   real use;
4. model/quant/KV/template effects on real project coherence;
5. single expert versus evidence-sharing routed experts;
6. bounded autoresearch: one mutable surface, frozen evaluator, finite budget,
   preserved failures, and explicit operator adoption.

Career-ready output is a reproducible repository, experiment cards, raw and
aggregated evidence, negative results, a short technical report, and the
ability to defend why the evaluator—not enthusiasm—supports the conclusion.

## Post-live expansion boundary

The sovereign coding foundation is live. Next expansions are optional and run
after ordinary use: automatic hosted-to-local handoff, plain-command Swift
detection, additional specialist models, the parked Tournament V2, Memory
scale/synthesis research, and governed full-text experiments above Research
Radar. None should interrupt the working DeepSeek/Qwen/Devstral path.

Do not call the system frontier-equivalent without later comparative evidence;
do call the current local production path usable, owned, and evidence-backed.
