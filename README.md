# Coding Intelligence Command Center

This repository is the general-purpose local coding-agent continuity lane.
EXCALIBUR provides local inference and the Mac remains the trusted Apple and
media-tool execution edge. Product work from Anime Frontier or any other
project stays outside this repository unless it is supplied as a frozen
qualification fixture or explicitly delegated.

The working baseline is deliberately thin: one implementation response proposes
one to four exact replacements in an isolated copy, one real project command
tests that staged result, and Devstral independently reviews a passing diff. The
source workspace is never changed.

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

Create a schema-v1 task file from `task.example.json`, then run:

```powershell
bin\coding-task.cmd D:\path\to\coding-task.json
```

That one command switches to the selected owned backend without UAC, sends one
structured edit request, and runs verifier-only project tests in the retained
stage. After a passing edit it switches to Ollama, sends the objective, scoped
diff, and test result to `devstral-small-2:24b` exactly once, enforces a strict
accept/reject verdict, and reports that result. Every non-PlanOnly outcome then
restores Qwen as the idle default. The JSON report separates implementer,
verifier, backend-swap, implementation-exit, final-process-exit, and restore
telemetry. There are no automatic retries or automatic patch promotion;
`-PlanOnly` makes zero model or backend calls.

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

## Explicit Mac edge commands

The SSH tunnel and Codex launcher are macOS-only commands. Importing or using
`local-edit` on Windows does not load their `fcntl`, `lsof`, or macOS runtime
path dependencies.

- `tunnel-ensure` owns the loopback-only SSH forward from Mac port 12434 to
  EXCALIBUR port 11434.
- `launch` validates a current checkpoint and starts the named EXCALIBUR Codex
  profile with `workspace-write` and approval `never`.
- `emergency-triage` is the explicit, read-only Mac fallback and still requires
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
