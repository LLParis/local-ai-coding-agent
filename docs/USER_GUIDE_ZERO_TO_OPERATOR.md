# Coding Intelligence: zero-to-operator guide

This guide is for operating the system, not developing it. The normal Windows
workflow is two commands: run the doctor, then give `coding-task` one bounded
task file.

## What this system does

The Command Center gives a local coding model real source context, lets it make
one bounded edit in a retained working copy, and runs the project's real test
command against that copy. It returns the diagnosis, diff, test output, timing,
and complete trajectory as JSON. The original project is not changed by the
model run.

```text
coding-task.json
       |
       v
backend switcher --> Qwen3.8 Q6 (default) or explicit alternate backend
       |                         one implementation call
       v
retained working copy --> real project test --pass--> Devstral review
       |                                      |              |
       |                                      +--reject------+--> JSON result
       |                                                     |
       +------------------------fail--------------------------+

original project ------------------------------------ remains unchanged
```

The retained copy is real code running real tests. It exists so a bad local
answer cannot overwrite the only copy of the project.

## The current roles

| Responsibility | Current choice | What is proven |
|---|---|---|
| Local implementation | Qwen3.8 27B Q6 | Best result in the bounded local tournament; verified work in Python, TypeScript, and corrected PowerShell tasks |
| Local review | Devstral Small 2 24B | Correct verdict in 3/3 verifier trials |
| Factual acceptance | The project's real tests/contracts | A passing model explanation alone never counts |
| Fast small-task candidate | gpt-oss 20B | Passed earlier tiny tasks but failed the harder tranche |
| Research/vision candidate | Gemma 4 31B QAT | Installed, but not promoted as implementer or verifier |
| High-judgment arbitration | Hosted Codex, while available | Architecture, ambiguous decisions, and final review |

The everyday command automatically invokes Devstral after the implementation
passes its deterministic project test. It then restores Qwen as the ready
default backend before returning.

## Step 1: check the system

Open PowerShell in the repository and run:

```powershell
bin\doctor.cmd
```

The happy-path result is:

```text
Overall: READY
Active backend: Qwen38
Configured: ready
Tested: evidence-recorded
Live: ready
```

`doctor` is read-only. It does not start or stop services, load a model, run an
inference request, write files, request elevation, or display UAC. For a
machine-readable report:

```powershell
bin\doctor.cmd -Json
```

The three proof levels deliberately mean different things:

- **Configured**: required tasks, commands, and model files exist.
- **Tested**: real-task evidence is recorded in this repository. The doctor
  does not silently rerun it.
- **Live**: exactly one owned model server is healthy on loopback right now.

`Ready` is a normal Scheduled Task standby state. It does not mean the task
failed. `Running` identifies the active backend.

## Step 2: describe one coding task

Copy the template:

```powershell
Copy-Item task.example.json my-task.json
```

Edit `my-task.json`. A task looks like this:

```json
{
  "schema_version": 1,
  "backend": "Qwen38",
  "workspace": "D:\\11_CS\\00_REPOS\\my-project",
  "objective": "Fix the parser defect with the smallest complete code change. Do not edit tests.",
  "mutable": [
    "src/parser.py"
  ],
  "context": [
    "src",
    "AGENTS.md"
  ],
  "verify_context": [
    "tests"
  ],
  "test_command": [
    "py",
    "-3",
    "-m",
    "unittest",
    "discover",
    "-s",
    "tests",
    "-v"
  ],
  "timeout": 180
}
```

Field meanings:

| Field | What to put there |
|---|---|
| `backend` | Use `Qwen38` normally. Use `Ollama` only for an explicit alternate-model trial. |
| `workspace` | Absolute path to the real project. |
| `objective` | One defect and the intended outcome. Include known constraints. |
| `mutable` | The one to four exact files the model may change. Start narrower and expand only when the diagnosis requires it. |
| `context` | Source, contracts, and local instructions the model needs to understand the task. |
| `verify_context` | Tests or fixtures copied into the working copy but hidden from the model prompt. |
| `test_command` | A JSON array containing the real command and each argument separately. |
| `timeout` | Whole-number seconds from 10 through 1800. |

Good objectives name the observed defect and the stopping condition. Avoid
asking for a broad rewrite when one exact behavior is broken.

## Step 3: validate without spending a model call

Before the real run, validate and display the plan:

```powershell
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File bin\coding-task.ps1 -Task .\my-task.json -PlanOnly
```

This checks the task schema and paths. A valid plan reports `modelCalls: 0` and
does not switch a backend or call a model.

## Step 4: run the local coding task

Run the task and retain its report:

```powershell
bin\coding-task.cmd .\my-task.json | Tee-Object -FilePath .\my-task.result.json
```

The command performs this bounded sequence:

1. Select the named, pre-owned backend without UAC.
2. Make exactly one implementation request.
3. Apply only the permitted replacements in a retained working copy.
4. Run the exact test command in that copy.
5. If the test passes, switch to Ollama and request one independent Devstral
   verdict on the objective, staged diff, and test evidence.
6. Restore Qwen as the default live backend and return one JSON report.

An accepted successful run uses two local model calls: one implementer and one
verifier. An implementation or test failure normally stops after one. A
verifier rejection or verifier error uses two. Automatic retries are always
zero.

Before the implementation request, the command also records a durable task
intent and typed state. The final report records test, verifier, backend
restore, and terminal evidence. Large prompt/output/diff bodies remain in the
retained stage; active memory stores their hashes and locators rather than
duplicating them.

Local inference does not consume hosted-model tokens. It still uses the PC's
GPU, memory, electricity, and time.

## Step 5: read the result

Load the report in PowerShell:

```powershell
$result = Get-Content .\my-task.result.json -Raw | ConvertFrom-Json
$result.status
$result.edit.diagnosis
$result.edit.diff
$result.edit.test_output
$result.edit.stage
$result.edit.trajectory
```

The important fields are:

| Field | Meaning |
|---|---|
| `status` | `verified` means the staged edit passed its test and Devstral accepted it. `rejected` means the test passed but Devstral rejected it. `failed` covers implementation, test, verifier execution, or backend-restore failure. |
| `edit.diff` | Exact proposed source change. |
| `edit.test_exit` | `0` is a passing command; anything else failed. |
| `edit.test_output` | Complete project-test output from the retained stage. |
| `edit.stage` | Retained working-copy directory containing the proposed files. |
| `edit.trajectory` | Prompt, request hash, raw response, candidate edit, diff, timing, and test evidence. |
| `verifier.verdict` | Devstral's `accept` or `reject` decision when the project test passed. |
| `verifier.reason` | Independent review explanation; `verifier.risks` lists up to three concrete concerns. |
| `restore.status` | Whether Qwen was restored as the default live backend before return. |
| `modelCalls` | Normally `2` for a completed review, `1` when implementation/testing stops early, or `0` if no model request began. |
| `automaticRetries` | Always zero. |
| `rawFailure` | Wrapper or protocol error when no structured edit report could be produced. |

Treat `verified` as “this patch passed the requested evidence and independent
review,” not “every possible behavior is perfect.” If the test command was
weak, strengthen that real command before trusting the result.

## Promoting a verified result

The current v1 does not silently apply model output to the original project.
For a Git workspace, the most direct manual promotion is:

```powershell
$result = Get-Content .\my-task.result.json -Raw | ConvertFrom-Json
if ($result.status -ne "verified") { throw "Refusing to promote an unverified result." }
$task = Get-Content .\my-task.json -Raw | ConvertFrom-Json
$patch = Join-Path $PWD "my-task.patch"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($patch, [string]$result.edit.diff, $utf8NoBom)
git -C ([string]$task.workspace) apply --check -- $patch
git -C ([string]$task.workspace) apply -- $patch
```

Then run the same project test in the original workspace and inspect `git diff`
before committing. If files changed in the original workspace after the local
trial began, do not force the patch; create a fresh task from the current state.

## Backend selection and recovery without UAC

Normal operation uses existing Scheduled Tasks and should not show UAC. To
restore the default backend:

```powershell
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File windows\Switch-ExcaliburBackend.ps1 -Backend Qwen38
```

To select the Ollama alternate explicitly:

```powershell
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File windows\Switch-ExcaliburBackend.ps1 -Backend Ollama
```

Run `bin\doctor.cmd` again after either command. If a normal doctor,
backend-switch, or coding-task command displays UAC, cancel it: that is not the
expected execution path. The installer scripts under `windows\Install-*` are
not everyday commands.

If the doctor reports `Conflict`, do not kill broad process names. Run one exact
backend switch, then run the doctor once more. If the same ownership failure
remains, stop repeating it and hand the doctor JSON to the coding agent for
diagnosis.

## What failure means

- `status: failed` with `test_exit` nonzero: the model produced a scoped patch,
  but the real test disproved it. Read the diagnosis, diff, and test output.
- `status: rejected`: the project test passed, but Devstral found the diff did
  not satisfy the objective or carried a concrete risk. Read
  `verifier.reason` and `verifier.risks`; do not promote it.
- `verifier.status: failed`: the implementation test passed, but no trustworthy
  independent verdict was obtained. The overall result remains failed.
- `rawFailure` is populated: the endpoint or structured edit contract failed
  before a testable patch existed.
- `stopped-or-unhealthy` from the doctor: no backend currently satisfies task,
  exact-loopback, health, model, and owner checks together.
- A missing model entry: the manifest or required artifact is incomplete; the
  doctor never downloads it automatically.

Do not feed an identical failed request back in an open retry loop. Improve the
objective or missing context once; after the second identical failure, change
the model or simplify the task.

## Glossary

- **Backend**: the local server process exposing a model API.
- **Model**: the weights doing the diagnosis and edit proposal.
- **Harness**: the surrounding task, tool, context, lifecycle, and telemetry
  machinery.
- **Working copy / stage**: the retained copy where the model edit and test run.
- **Verifier**: an independent test or reviewer that judges the patch.
- **Trajectory**: the exact record of what the model saw, returned, changed,
  and how verification ended.
- **Loopback**: `127.0.0.1`; the raw model endpoint is available only on this PC.

## Honest current boundary

Windows operation, backend lifecycle, Qwen implementation, deterministic
verification, durable run memory, the Mac/Swift execution edge, PC-to-Mac Codex
SSH, and automatic terminal-child retirement are working. The native 262K
profile is lifecycle-proven and its final matrix is running. Pi is ready for a
live held-out trial; DeepSeek Harness still requires a scoped live-event plugin.
Automatic patch promotion is not enabled, and current evidence does not yet
establish a 90% or 95% frontier-equivalence rate.

For implementation evidence and current limitations, see
[`PRODUCTION_HANDOFF_V1.md`](PRODUCTION_HANDOFF_V1.md). For raw tournament
results, see [`TOURNAMENT_V1.md`](TOURNAMENT_V1.md).
