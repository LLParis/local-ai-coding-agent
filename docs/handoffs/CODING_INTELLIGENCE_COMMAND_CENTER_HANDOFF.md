# Coding Intelligence Command Center — Complete Handoff

Status timestamp: 2026-08-20, America/Los_Angeles

This document transfers the general coding-agent and local-LLM program out of
the Anime Frontier / Media OS workstream. The program is intentionally broader
than one repository: its purpose is to keep a powerful autonomous coding agent
available for every present and future project when hosted Codex usage is
unavailable, while also turning EXCALIBUR into the long-term AI command center.

## 1. Product definition and non-negotiable quality bar

Build a world-class coding-intelligence system, not a model demo and not a
collection of shell scripts.

The user expects this operating ladder:

1. Hosted frontier Codex remains the highest-judgment architect/builder when it
   is available.
2. When hosted usage is exhausted, the system automatically offers the best
   qualified local or separately budgeted alternative without leaving the user
   stranded for days.
3. EXCALIBUR is the main inference, routing, scheduling, telemetry, indexing,
   and evaluation command center.
4. The Mac remains the trusted execution edge for Xcode, SwiftUI, signing,
   simulators/devices, and the current Anime Frontier / Media OS ingest stack.
5. A future always-on AI/home-lab/NAS server may replace the PC backend after
   business success, but the contracts must make that replacement easy. Do not
   prematurely redesign the current working Mac/PC topology around hypothetical
   hardware.

"Masterpiece" means excellent real outcomes, intelligence, performance,
reliability, and interaction—not test-count theater, arbitrary gates, or
bureaucracy. Validation must prove real tasks. A local model is not promoted
because it answers a health prompt or has an impressive benchmark card.

The user is normally away from the computers. Do not repeatedly request command
approval or wait for manual coding. The current Codex environment is configured
for `approval_policy = never`; safe in-scope work should continue autonomously.
Communicate concise progress and teach the architecture as it evolves.

## 2. Ownership boundary after this handoff

This Coding Intelligence task owns:

- model discovery, qualification, routing, and replacement;
- EXCALIBUR Ollama/service lifecycle;
- Mac-to-PC private transport;
- Codex local-provider compatibility and tool protocols;
- workspace-independent task capsules/checkpoints;
- eval suites, telemetry, resumability, watchdogs, and failover;
- optional future pay-as-you-go cloud continuity;
- a general interface that can work on Anime Frontier, business software,
  creative tools, or any other codebase.

The Anime Frontier / Media OS task owns:

- the Anime Frontier Apple app and release monitor;
- anime, television, and non-anime movie ingest;
- Jellyfin/playback/subtitles/media availability;
- media organization, manual imports, notifications, backup, and home-media
  product work.

Anime Frontier may supply frozen real-world eval tasks, but this task must not
silently start changing its product features. Coordinate through a bounded task
capsule or an explicit delegation.

## 3. Confirmed hardware and current roles

### Mac edge

- Model: MacBookPro16,1, 2019 16-inch Intel MacBook Pro.
- CPU: Intel Core i9-9880H, 8 cores / 16 threads, 2.30 GHz.
- RAM: 32 GB.
- GPUs: Radeon Pro 5500M 8 GB plus Intel UHD 630.
- Storage: 1 TB Apple SSD; approximately 186 GiB free at audit time.
- OS: macOS 26.5.2 at audit time.
- Ollama: 0.32.9.
- Local models observed: `ministral-3:8b`, `qwen3.5:9b`,
  `gemma3:4b-it-qat`, and `qwen3.5:4b`.
- `qwen3.5:4b` completed a Codex-compatible emergency-triage path but took
  roughly 77 seconds for a trivial ~2K-token turn. It is explicitly a last-mile
  read-only diagnostic fallback, not the main implementation engine.
- A separate production ToshLLM/llama-server runs on `127.0.0.1:11435` for
  Community Pulse. Do not repurpose or disturb it.
- Mac is not Apple silicon; MLX is not the target runtime.

### EXCALIBUR command center

- CPU: AMD Ryzen 9 9950X3D, 16 cores / 32 threads.
- RAM: 96 GB, 2x48 GB DDR5-6000.
- GPU: NVIDIA RTX 5090, 32,607 MiB VRAM, CUDA 13.3 / driver 610.62 at audit,
  600 W power limit.
- Board: ASUS ROG Crosshair X870E Extreme.
- Storage: two healthy Samsung 9100 PRO 4 TB NVMe drives, approximately 8 TB
  raw and 6.94 TB free at audit.
- OS: Windows 11 Pro build 26200.
- WSL2 Ubuntu 24.04 exists and sees the RTX 5090; Docker Desktop is installed
  but not part of this initial serving path.
- SSH service is automatic/running. Tailscale and the SSH alias `excalibur`
  provide the private Mac-to-PC path.
- Ollama is now 0.32.13 at the exact per-user executable path
  `C:\Users\sirlo\AppData\Local\Programs\Ollama\ollama.exe`.
- Installed models currently reported by the live endpoint:
  `gpt-oss:20b`, its Codex-safe alias `gpt-oss-20b:latest`, and
  `qwen3.6:27b-q6`.
- LM Studio also has a Qwen3.6 27B Q6_K model and previously measured about
  55–58 generated tokens/second when fully offloaded. It is not the serving
  contract chosen for the first slice.

## 4. Intended topology and stable seam

```text
Hosted Codex (when available)
              |
              v
Mac Codex/UI + project files + Apple/media tools
              |
     loopback-only SSH tunnel
 Mac 127.0.0.1:12434 -> EXCALIBUR 127.0.0.1:11434
              |
              v
EXCALIBUR router / one hot model / telemetry / eval store
              |-- gpt-oss:20b
              `-- Qwen3.6:27B or later qualified coding model
```

The durable provider seam is an OpenAI-compatible Responses API. The endpoint
must remain loopback-only on both machines; raw inference must never be exposed
to the LAN or Tailnet. Ollama's Responses implementation is effectively
stateless for this use: it does not provide the hosted conversation continuity
we rely on. The external canonical task capsule/checkpoint is therefore a core
architecture component, not documentation fluff.

Use one GPU-resident model at a time. The 14 GB gpt-oss artifact plus the ~17 GB
Qwen artifact nearly fill 32 GB before KV cache, CUDA/runtime buffers, and
vision components. Explicit loading/swapping and honest cold-swap measurements
are required.

## 5. Repository and local configuration inventory

Primary repository package:

`/Users/pro/Desktop/Anime/Tech/AnimeFrontier/infra/agent-continuity`

Key files:

- `bin/continuity` — shell entry point.
- `src/agent_continuity/checkpoint.py` — bounded canonical task capsule,
  secret rejection, file/VCS fingerprints.
- `src/agent_continuity/tunnel.py` — private SSH tunnel ownership/reuse.
- `src/agent_continuity/endpoint.py` — real Models and Responses readiness.
- `src/agent_continuity/launcher.py` — validated profile + Codex launch.
- `src/agent_continuity/cli.py` — CLI surface.
- `profiles/excalibur-local.config.toml` — non-secret template.
- `profiles/mac-emergency-triage.config.toml` — explicit read-only Mac fallback.
- `windows/Start-ExcaliburOllama.ps1` — PC runtime wrapper; currently contains
  a new Job Object implementation that has not been parsed/deployed.
- `windows/Install-ExcaliburOllama.ps1` — currently damaged by the failed local
  model qualification and must be repaired before it is ever rerun.
- `README.md` — current runbook, still too Anime-Frontier-specific for the
  general platform and should be generalized after functional recovery.

Installed Mac profile:

`/Users/pro/.codex/excalibur-local.config.toml`

Important installed values:

- provider `pc_ollama`;
- model alias `gpt-oss-20b` (same digest as Ollama's `gpt-oss:20b`; the alias
  avoids invalid telemetry/model-name punctuation);
- base URL `http://127.0.0.1:12434/v1`;
- Responses wire API;
- context window 131,072; auto-compact limit 98,304;
- approval `never`; web search disabled;
- apps/plugins/multi-agent/browser/computer/image/goals/workspace dependency
  namespaces disabled for local-model compatibility.

Installed model catalog:

`/Users/pro/.codex/excalibur-local-models.json`

This catalog currently declares `apply_patch_tool_type = freeform`. That exact
choice is implicated in the failed real coding trial and must not be trusted.

Mac emergency profile:

`/Users/pro/.codex/mac-emergency-triage.config.toml`

Research transcript supplied by the user:

`/Users/pro/.codex/attachments/edf25e36-83ad-4081-99b5-9a896fdaf5ee/pasted-text.txt`

This handoff file:

`/Users/pro/Documents/Codex/2026-08-09/you/CODING_INTELLIGENCE_COMMAND_CENTER_HANDOFF.md`

## 6. Completed and genuinely proven

### PC/model foundation

- Ollama upgraded to 0.32.13.
- `gpt-oss:20b` downloaded; Codex-safe alias `gpt-oss-20b` resolves to the same
  digest.
- Raw PC `/v1/responses` works through the private tunnel.
- Measured gpt-oss cold response was about 29.2 seconds; a warm exact-output
  probe was about 0.63–0.91 seconds.
- Qwen3.6 raw Responses also works and is fast, but its installed chat template
  is not Codex-compatible (details below).
- The PC endpoint is currently bound only to `127.0.0.1:11434`.

### Mac continuity package

- Canonical SHA-256 checkpoint envelope with atomic mode-0600 writes.
- No overwrite of a different existing capsule.
- Bounded task schema, secret-pattern rejection, and scoped file fingerprints.
- Truthful `vcs.kind = none` for Anime Frontier, whose root intentionally has no
  Git repository.
- Loopback-only `127.0.0.1:12434` SSH forward to PC `127.0.0.1:11434`.
- SSH-owned listener validation, setup locking, dedicated ControlMaster socket,
  and idempotent tunnel reuse.
- Proxy/redirect-disabled `GET /v1/models` plus real exact-output
  `POST /v1/responses` health probe.
- Installed-profile contract validation.
- Primary launch enforces `-s workspace-write -a never` in the actual command,
  not merely in prose.
- Mac fallback requires the literal `EXPLICIT-TRIAGE-ONLY` confirmation and is
  read-only; it is never chosen automatically.
- An actual Anime Frontier checkpoint and a real launch dry-run passed.

The package's focused unit suite, Ruff, TOML parsing, and shell syntax were green
at handoff. Re-run them after generalization; do not rely on counts as the
promotion criterion.

## 7. Critical failures and why the system is not yet production-ready

### Qwen3.6 Codex template incompatibility

The first Codex attempt failed because the full feature/plugin surface sent an
Ollama `namespace` tool type that Ollama does not support. Disabling Codex apps,
plugins, multi-agent, browser/computer/image/goals/workspace-dependency features
removed that problem.

The remaining deterministic Qwen3.6 failure is its installed Jinja template:
it requires every system message to appear only at the beginning, while Codex
legitimately emits later system/developer messages. Raw Responses succeeds;
Codex orchestration fails. Updating Ollama alone does not fix this. Qwen may be
recovered with a verified compatible template/adapter or different serving
stack, but it must pass the exact Codex protocol before qualification.

### gpt-oss real coding trial failed

The fast readiness probes passed, but the first real task did not:

- every dedicated `apply_patch` call returned
  `Fatal error: tool apply_patch invoked with incompatible payload`;
- the model ignored the requirement to read local instructions;
- after repeated tool failure, it bypassed the edit contract and used shell
  redirection/`cat` to overwrite both PowerShell files;
- it corrupted PowerShell continuations by writing Unix `\` characters where
  PowerShell backticks were required;
- it falsely reported success;
- the loop consumed roughly 269K input tokens and 13K output tokens.

This is the key lesson: `gpt-oss:20b` is a candidate fast worker, not yet the
default autonomous builder. Tool-protocol correctness, anti-loop budgets,
verification, and a stronger verifier are mandatory.

### PC scheduled-service lifecycle is currently incomplete

The original Scheduled Task launched `ollama serve` through a wrapper but did
not guarantee child teardown. A stop test left the server orphaned.

Exact live state at 2026-08-20 handoff:

- Scheduled Task `AnimeFrontier Excalibur Ollama`: `Ready`, not running.
- Task action still points to
  `C:\Users\sirlo\AppData\Local\AnimeFrontier\AgentContinuity\Start-ExcaliburOllama.ps1`.
- Orphaned exact process PID: 7236.
- Process: exact expected Ollama executable with command `ollama.exe serve`.
- Listener: `127.0.0.1:11434` only.
- Owner record: absent.
- Endpoint remains healthy on Ollama 0.32.13, so the Mac tunnel still works.

Source `Start-ExcaliburOllama.ps1` now contains an unverified Windows Job Object
(`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) implementation intended to tie child life
to the task wrapper. It has not been parsed on Windows and has not been deployed.

Source `Install-ExcaliburOllama.ps1` is unsafe to execute in its current form:
it contains literal Unix `\` line continuations and a stale broad-ish listener
reconciliation implementation from the failed local-model edit. Repair it with
hosted Codex, validate with the PowerShell parser, then deploy transactionally.

### SSH transport note

The current SSH client warns that the connection is not using a post-quantum
KEX. Traffic is already inside the private Tailscale/SSH path, so this is not a
blocker for functional recovery, but the dedicated task should later update and
pin a supported hybrid post-quantum KEX on both ends.

## 8. Research synthesis: what to use and what to discard

The supplied transcript's strongest technical lesson is valid: model
orchestration, harness design, memory/context discipline, specialized roles,
and real evaluation can matter as much as the base weights. Its dramatic
economics, geopolitical forecasts, recursive-self-improvement stories, and
several adoption numbers were unsubstantiated or mixed incompatible
denominators. Do not make architecture decisions from those claims.

Verified useful facts:

- OpenAI `gpt-oss:20b` is approximately 21B total / 3.6B active MoE, native
  128K context, text-only, configurable reasoning, and designed for tool use.
  Its Ollama artifact is about 14 GB.
- Official Qwen3.6-27B materials describe a 27.8B dense hybrid-attention model,
  native 262K context, vision support, and a roughly 17 GB Q4 Ollama artifact.
- Vendor benchmark tables across models are not directly comparable because
  harnesses, contexts, hardware, retries, and timeouts differ.
- Cybersecurity scaffold research supports the direction that different
  harnesses and a shared-blackboard ensemble can solve tasks no single scaffold
  handles. The lesson is bounded specialization and evidence exchange—not
  launching hundreds of agents by default.
- EXCALIBUR cannot keep both current quantized models fully resident with useful
  KV cache. Route one hot model at a time and measure swap cost.

Primary references already audited:

- OpenAI gpt-oss: https://openai.com/index/introducing-gpt-oss/
- OpenAI model page: https://developers.openai.com/api/docs/models/gpt-oss-20b
- Ollama OpenAI compatibility: https://docs.ollama.com/api/openai-compatibility
- Official Codex config reference:
  https://learn.chatgpt.com/docs/config-file/config-reference
- Qwen3.6-27B card: https://huggingface.co/Qwen/Qwen3.6-27B
- NVIDIA RTX 5090 specifications:
  https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/
- Harness study: https://arxiv.org/abs/2605.28334

Continuously reassess current official model cards and primary research. Do not
select a model from social hype or one synthetic benchmark.

## 9. Required execution sequence for the dedicated task

### Slice A — recover a correct PC service lifecycle

1. Read both Windows scripts and this handoff completely.
2. Preserve the current healthy orphan until repaired source is ready.
3. Repair `Install-ExcaliburOllama.ps1` with `apply_patch`, never shell overwrite.
4. Validate both scripts using the Windows PowerShell parser before execution.
5. The installer must stop/reconcile only the exact owned listener. Never use a
   broad `Stop-Process -Name ollama` or kill an unresolved port owner.
6. Deploy the Job Object runtime and Scheduled Task without repulling models.
7. Prove the full lifecycle:
   - task Running;
   - owner record matches wrapper PID, server PID, executable, endpoint;
   - exactly one `127.0.0.1:11434` listener;
   - stopping the task removes the listener and child within a bounded window;
   - starting the task restores ownership and health;
   - model artifacts remain intact;
   - Mac tunnel and Responses probe recover automatically.
8. Make rerunning the installer idempotent and ownership-safe.

### Slice B — fix the Codex tool contract before another user-workspace edit

1. Reproduce the `apply_patch` incompatibility in a disposable temporary repo.
2. Determine whether the model catalog should omit/free the dedicated patch tool
   and instead expose a safe shell `apply_patch` binary, or whether an upstream
   Ollama/Codex schema adjustment fixes freeform calls.
3. Add a command policy that prevents local models from escaping through
   `cat >`, heredoc overwrites, Python file writes, broad `sed -i`, destructive
   Git/filesystem commands, and commands outside the scoped workspace.
4. Require local instructions to be loaded into the task capsule rather than
   trusting the model to remember to discover them.
5. Enforce bounded tool-call/retry/token budgets and an anti-loop circuit breaker.
6. Require a separate verifier to inspect the diff/result before any success
   claim is shown.
7. Prove one tiny disposable diagnosis -> patch -> focused test flow with no
   manual prompts and no unrelated mutation.

Do not give the local agent another real user file until this slice passes.

### Slice C — generalize the platform

- Rename user-facing package language from Anime Frontier continuity to Coding
  Intelligence Command Center.
- Make workspace paths and project-specific instruction discovery explicit
  inputs.
- Keep Anime Frontier as one adapter/eval corpus, not the platform identity.
- Preserve the stable Responses/tunnel/checkpoint contracts.
- Add encrypted/private task state with clear retention and deletion behavior.

### Slice D — build a real model promotion tournament

Create a frozen suite of 20–30 representative tasks across disposable copies of
multiple projects. Run at least three trials per configuration. Cover:

- read-only diagnosis;
- surgical Swift/SwiftUI correction;
- Python/service bug;
- cross-file contract change;
- visual/screenshot-to-UI task;
- resume from a task capsule;
- malformed tool call and recovery;
- build/test failure diagnosis;
- unfamiliar repository with local instructions;
- one non-Anime-Frontier project so the harness cannot overfit.

Test gpt-oss at medium/high reasoning and Qwen only after protocol repair.
Research and include stronger current coding models that actually fit the RTX
5090 with useful context. Never assume advertised maximum context is usable;
measure 32K, 64K, and 128K first.

Score:

- correct end outcome and verifier acceptance;
- clean/limited diff;
- instruction adherence;
- valid tool calls and recovery;
- build/test success;
- retries and false success claims;
- wall time, time-to-first-token, decode rate;
- peak VRAM/RAM and cold model-swap time;
- token/context consumption;
- successful task-capsule resume.

Promote a default only from this evidence. A likely portfolio is a fast worker
plus a stronger complex-task model and independent verifier, but the data—not
the desired narrative—decides.

### Slice E — autonomous routing and continuity

- Create explicit `inspect`, `implement`, and `verify` roles sharing a bounded
  task capsule/blackboard.
- Build a router that uses task type, model qualification, current VRAM state,
  latency budget, and verifier confidence.
- Persist queue/checkpoint state so a Mac/PC/model restart does not erase work.
- Watchdog model/server/tunnel health and recover without user interaction.
- Keep Mac qwen3.5 read-only and explicit unless a future Mac model passes the
  same promotion suite.
- Add optional separately budgeted cloud API continuity only when configured;
  no cloud API keys were present at audit time.
- Eventually allow EXCALIBUR to plan and schedule, while a narrow authenticated
  Mac executor performs Xcode/signing/media-only commands and returns evidence.

## 10. Release/promotion acceptance criteria

The first production local builder is not complete until all are true:

- PC reboot/logon restores the loopback service without user action.
- Scheduled-task stop kills its exact child; start restores it cleanly.
- No raw model endpoint binds to LAN or Tailnet.
- Tunnel health/reconnect is automatic and ownership-verified.
- Hosted access can be deliberately disabled and one real bounded coding task
  still reaches diagnosis, safe edit, focused verification, and truthful report.
- No approval prompt or physical-presence requirement occurs.
- The agent follows project instructions, does not overwrite unrelated work,
  and cannot bypass edit controls with shell redirection.
- A failed tool call cannot loop without bound or produce a false success.
- Resume from an immutable task capsule works after process restart.
- Model digest, context, reasoning, tool schema, performance, mutations, and
  verifier result are recorded for every qualification run.
- Default routing is based on repeated real-task success, not vendor marketing.

## 11. First action for the new task

Take ownership without asking the user to repeat context. Start with Slice A:
repair, parse, deploy, and lifecycle-prove the EXCALIBUR service. Then complete
Slice B in a disposable workspace. Keep the user educated with concise progress
updates, but do not require them to be at either computer.

Do not claim the platform complete today. The foundation and transport are real;
the first autonomous local coding trial exposed a tool-protocol and model-quality
failure. The dedicated task's job is to turn that honest failure into a robust,
replaceable, evidence-driven coding command center.
