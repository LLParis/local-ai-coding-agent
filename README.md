# Coding Intelligence Command Center

This repository is the general-purpose local coding-agent continuity lane.
EXCALIBUR provides local inference and the Mac remains the trusted Apple and
media-tool execution edge. Product work from Anime Frontier or any other
project stays outside this repository unless it is supplied as a frozen
qualification fixture or explicitly delegated.

GitHub/mobile home: [LLParis/local-ai-coding-agent](https://github.com/LLParis/local-ai-coding-agent).
See the [2026 research-to-implementation map](docs/RESEARCH_TO_IMPLEMENTATION_2026.md)
for the current arXiv sweep and the exact mechanisms adopted or parked.

The everyday production loop is focused and truthful: DeepSeek Harness drives
the local Qwen3.8-27B Q6 Native 262K model through an inspect/edit/test loop in
an isolated stage, the repository's real command verifies the staged
result, and Devstral reviews a passing diff once as an advisory second opinion
while deterministic execution remains final authority. A verified result is
applied to the source by default; `--stage-only` keeps it in the retained stage.

See [`docs/OUTCOME_FIRST_CONTRACT.md`](docs/OUTCOME_FIRST_CONTRACT.md) for the
trial stopping rules.

Current evidence and research:

- [`v1 production handoff`](docs/PRODUCTION_HANDOFF_V1.md)
- [`zero-to-operator guide`](docs/USER_GUIDE_ZERO_TO_OPERATOR.md)
- [`research-to-architecture synthesis`](docs/RESEARCH_SYNTHESIS.md)
- [`harness fusion decision`](docs/HARNESS_FUSION_DECISION.md)
- [`memory architecture decision`](docs/MEMORY_ARCHITECTURE_DECISION.md)
- [`long-context and knowledge decision`](docs/LONG_CONTEXT_DECISION.md)
- [`August 2026 community research`](docs/AUGUST_2026_COMMUNITY_RESEARCH.md)
- [`Research Radar and governed autoresearch decision`](docs/RESEARCH_RADAR_DECISION.md)
- [`Research Radar ingestion v1 operator contract`](docs/RESEARCH_RADAR_V1.md)
- [`evidence-driven routing v1`](docs/ROUTING_V1.md)
- [`DeepSeek Harness audit`](docs/DEEPSEEK_HARNESS_AUDIT.md)
- [`bounded model tournament`](docs/TOURNAMENT_V1.md)

## One-command status

Before a task, inspect the live system without starting or stopping anything:

```powershell
bin\doctor.cmd
```

Use `bin\doctor.cmd -Json` for the same report as structured JSON. The doctor
distinguishes installed configuration, recorded test evidence, and current
live readiness; it performs no model call and requires no elevation.

## Everyday command

The everyday command takes a repository and a plain objective:

```powershell
bin\coding.cmd D:\path\to\project "Fix the described defect without changing tests."
```

The evidence-bound router selects the default DeepSeek route and exact owned
`Qwen38Native` backend (Qwen3.8-27B Q6, native 262K) without UAC. Before the
first model call, the worker retrieves bounded workspace Memory and starts a
fresh-stage DeepSeek continuation under the selected `--task-key`. Full-repository
jobs give the agent dedicated file tools plus workspace-write PowerShell for staged Git,
builds, and diagnostics; focused `--scope` jobs use only the dedicated scoped
tools without PowerShell. Qwen Code's pinned `basedpyright` and `tsc` commands
are on the diagnostic path. The repository's real command verifies the staged
result; materially new failure evidence allows at most two targeted repairs, and
an identical failure stops the run. Promotion is transactional: an external backup is
written, one apply runs, the manifest is compared, failure triggers rollback,
and cleanup happens only after success; `--stage-only` retains the stage without
touching the workspace. After a passing run the worker switches to Ollama and
sends the objective and diff to `devstral-small-2:24b` exactly once as an
advisory reviewer; deterministic execution remains final authority. The backend
is restored afterward.

Repeat `--scope` to focus a job on named repository-relative paths; without it
the job covers the full repository:

```powershell
bin\coding.cmd D:\path\to\project "Finish the focused production job" `
  --scope src\agent_continuity\production_worker.py --scope docs --stage-only
```

The effective requested scope, route decision, retrieved memories, session
lineage, and execution edge are recorded in the final JSON. `--task-key`
continues one durable workspace workstream. `--harness qwen-code` remains an
explicit model-aligned alternate; Qwen Code never claims DeepSeek session
lineage. For unusual repositories, pass a UTF-8 JSON argv array with
`--verify-command-file`; ordinary projects use automatic Python/Node/Rust/Go/.NET
detection. Automatic Apple execution supports Swift package test/build, one
unambiguous shared-scheme Xcode build, and plain Swift typecheck; Swift packages
route to the trusted Mac while Qwen stays on EXCALIBUR. Xcode and plain-Swift
modes are source-supported but not yet live-proven until matching real projects
exercise them. The verified Qwen3.8/DeepSeek/Mac Swift repair run
`404f6f14-c535-4f5d-ae4d-541f98b7a536` is live Swift-package evidence. There
are no automatic model retries.

The paired live foundation record—including the applied Memory/continuity run
`c72309f7-edc3-4fc9-9423-f2af7506e334`—is
[`runs/live-foundation-completion.json`](runs/live-foundation-completion.json).

The same command records a durable task intent before model dispatch and a
terminal evidence-backed state afterward. Production Memory compacts complete
claims plus cryptographic provenance under the per-memory token cap; it keeps
hashes and locators rather than duplicating prompts, model output, diffs, or
test logs.
The task-file lane `bin\coding-task.cmd` remains available for one-response
scoped edits; it is no longer the everyday entrypoint.

## Native context and harness candidates

The everyday production loop runs on the frozen Q6/native-262K/Q4-KV/MTP-off
profile: DeepSeek Harness switches to `Qwen38Native` for the job and restores
the idle default afterward. The bounded task-file lane keeps the 32K `Qwen38`
profile. Operators can also switch backends manually:

```powershell
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File windows\Switch-ExcaliburBackend.ps1 -Backend Qwen38Native
```

Restore normal operation with `-Backend Qwen38`. Both paths are current-user,
loopback-only, exact-owner Scheduled Tasks and do not display UAC.

Pi 0.84.2 remains the qualification-ready lightweight interactive adapter.
DeepSeek Harness is the default everyday loop for the Qwen3.8-27B Q6 Native
262K backend. See `runs/adapter-install-state.json` for recorded adapter state.

## Memory commands

The everyday runner writes Memory v1 automatically. Operators can also inspect
or repair it explicitly:

```powershell
bin\continuity.cmd memory-verify --root "$env:LOCALAPPDATA\CodingIntelligence\MemoryV1"
bin\continuity.cmd memory-rebuild --root "$env:LOCALAPPDATA\CodingIntelligence\MemoryV1"
```

`memory-search`, `memory-get`, `memory-pack`, `compaction-validate`, and
`compaction-commit` expose the same exact-scope retrieval and loss-checked state
boundaries. Run `bin\continuity.cmd --help` for their full arguments.

## Research Radar metadata intake

`radar-ingest` converts a bounded exact-ID arXiv Atom response into immutable
paper/version, source-observation, and lifecycle records. `radar-transition`
advances one explicit evidence-backed lifecycle event. Both commands are
read-only dry runs unless `--apply` is supplied; neither downloads or executes
paper code and neither can implement a candidate. See
[`docs/RESEARCH_RADAR_V1.md`](docs/RESEARCH_RADAR_V1.md).

## Windows: run one local edit

Python 3.11 or newer is required. The repository can be installed as a normal
CLI:

```powershell
py -3 -m pip install -e .
continuity --help
```

It can also run directly without installation:

```powershell
bin\continuity.cmd local-edit `
  --workspace D:\path\to\project `
  --objective "Fix the described defect without changing tests." `
  --mutable src\package\module.py `
  --context src\package `
  --verify-context tests `
  --base-url http://127.0.0.1:11434/v1 `
  --model gpt-oss-20b:latest `
  -- py -3 -m unittest discover -s tests -v
```

`--context` is visible to the model. Each `--verify-context` path is copied into
the isolated stage so the test command can use authoritative fixtures, but its
contents are excluded from the model prompt. Mutable files are always visible
and cannot be placed under verifier-only context.

The command makes exactly one `POST /v1/chat/completions` request. The endpoint
must be plain HTTP on loopback. A response is rejected unless it contains a
non-empty string diagnosis and between one and four well-formed, scoped edits,
even when the server ignores the requested JSON schema. The JSON report contains
the retained stage path, diff, test output, timing, and `model_calls: 1`.

## macOS: the same thin lane

```sh
./bin/continuity local-edit \
  --workspace /path/to/project \
  --objective 'Fix the described defect without changing tests.' \
  --mutable src/package/module.py \
  --context src/package \
  --verify-context tests \
  --base-url http://127.0.0.1:12434/v1 \
  --model gpt-oss-20b:latest \
  -- python3 -m unittest discover -s tests -v
```

## Explicit Mac repair-rail commands

The SSH tunnel and Codex launcher are macOS-only commands. Importing or using
`local-edit` on Windows does not load their `fcntl`, `lsof`, or macOS runtime
path dependencies.

- `tunnel-ensure` owns the loopback-only SSH forward from Mac port 12434 to
  EXCALIBUR port 11434.
- `launch` validates a current checkpoint and starts the named EXCALIBUR Codex
  profile with `workspace-write` and approval `never`.
- `emergency-triage` is the explicit, read-only Mac repair rail and still requires
  `--confirm EXPLICIT-TRIAGE-ONLY`.

The runtime override is `CODING_INTELLIGENCE_RUNTIME_DIR`. The former
`ANIME_FRONTIER_CONTINUITY_RUNTIME_DIR` name remains a compatibility fallback.
Likewise, the serialized v1 checkpoint schema identifier remains unchanged so
existing task capsules continue to validate; renaming that wire contract needs
an explicit migration.

## Checkpoints

Create checkpoints outside the scoped workspace so the checkpoint does not make
itself stale:

```sh
./bin/continuity checkpoint-create \
  --workspace /path/to/project \
  --task /private/state/task.json \
  --output /private/state/checkpoint.json

./bin/continuity checkpoint-validate \
  --workspace /path/to/project \
  --checkpoint /private/state/checkpoint.json \
  --require-current
```

Checkpoint writes are atomic and refuse to replace different content. The JSON
uses a canonical SHA-256 envelope, allowlisted task fields, secret-pattern
rejection, bounded scope, and file/VCS fingerprints without diff contents.

## Focused verification

The focused suite uses only local fake HTTP servers; it does not call an
inference service:

```powershell
py -3 -m unittest -v
```

```sh
python3 -m unittest -v
```
