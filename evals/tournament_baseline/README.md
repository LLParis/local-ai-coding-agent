# Coding Intelligence Command Center

This repository is the general-purpose local coding-agent continuity lane.
EXCALIBUR provides local inference and the Mac remains the trusted Apple and
media-tool execution edge. Product work from Anime Frontier or any other
project stays outside this repository unless it is supplied as a frozen
qualification fixture or explicitly delegated.

The working baseline is deliberately thin: one model response proposes one to
four exact replacements in an isolated copy, then one real project command
verifies that staged result. The source workspace is never changed.

See [`docs/OUTCOME_FIRST_CONTRACT.md`](docs/OUTCOME_FIRST_CONTRACT.md) for the
trial stopping rules.

## Everyday command

Create a schema-v1 task file from `task.example.json`, then run:

```powershell
bin\coding-task.cmd D:\path\to\coding-task.json
```

That one command switches to the selected owned backend without UAC, sends one
structured edit request, runs verifier-only project tests in the retained stage,
and returns one JSON report. `Qwen38` is the current default implementation
backend based on the bounded local tournament; `Ollama` remains available for
explicit fallback trials.

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

These tests use only local fake HTTP servers; they do not call an inference
service:

```powershell
py -3 -m unittest tests.test_local_edit -v
```

```sh
python3 -m unittest tests.test_local_edit -v
```
