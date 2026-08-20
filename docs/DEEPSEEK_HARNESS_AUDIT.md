# DeepSeek Harness bounded audit

Date: 2026-08-20

## Decision

DeepSeek Harness is the best-fit upstream substrate examined in this slice for the future interactive continuity layer, but it is not a drop-in replacement for the command center's proven thin execution lane. Keep `bin/coding-task.cmd` and the isolated `local_edit.py` path as the production runner. Reuse DeepSeek's event/session design now, and later place a pinned DeepSeek headless runtime behind the same task/report contract as an optional adapter. Do not install its plugin ecosystem or give its shipped profiles production authority yet.

This conclusion comes from the official `deepseek-ai/deepseek-harness` source at commit `141eb6fef83422698aef7a981029e843e8161534` (`0.1.0-rc.8`, 2026-08-19). The project is MIT licensed, but its own README labels the release a developer preview with expected compatibility-breaking changes (`README.md:7-11,57`; `package.json:3-4`). The audit was read-only: no packages were installed, no plugin was loaded, and no Harness process touched user work.

## What the source actually provides

| Area | Confirmed behavior | Consequence here |
|---|---|---|
| Composition | Every major component is a replaceable Cordis plugin. Ordered bundle, profile, home, and command-line patch layers compose the runnable tree; a patch replaces a row's whole `config` (`docs/architecture.md:9-35`). | Good substrate for adapters, but pin the upstream commit and own one small profile because upstream config changes can break an override. |
| Headless execution | The shipped headless bundle creates one fresh persisted Agent, submits one task, waits for quiescence, flushes the session, prints the last assistant text, and exits. It opens no port (`packages/bundle/headless/README.md:5-20`; `packages/bundle/headless/src/index.ts:96-149`). | Fits bounded qualification and batch work. It does not itself provide interactive continuation, and every invocation creates a fresh UUID session (`packages/bundle/headless/src/index.ts:111-127`). |
| Local model transport | `dsh-llm-pi-ai` accepts hand-declared self-hosted or OpenAI-compatible routes with explicit protocol, base URL, model catalog, capacity, reasoning, and compatibility settings; a route may be unauthenticated (`packages/llm/llm-pi-ai/README.md:5-12,47-78,93-115`). | EXCALIBUR's llama.cpp and Ollama endpoints can be adapter targets without changing their lifecycle ownership. Exact endpoint compatibility still needs one real task proof. |
| Model selection | The default-model service supplies one process default to newly created Agents; an existing session keeps its recorded route (`packages/core/agent-default-model/README.md:5-12,20-25`). Provider metadata is explicitly not a routing whitelist (`packages/llm/llm/README.md:17-35`). | DeepSeek supplies provider/model seams, not the command center's quality-aware task router. Qualification results and current VRAM state must remain command-center inputs. |
| Tool execution | Native tools run through pre-policy, monotonic guards, execution wrappers, post-policy, finalization, and one durable result (`docs/tool-execution-pipeline.md:4-60`). Code Mode can collapse tools behind a generated typed `run_code` SDK, but the documentation does not claim a universal token reduction (`packages/core/tools/README.md:116-170`). | Adopt the ordered outcome model. Treat Code Mode as a later A/B candidate; Qwen must prove it beats the existing structured-patch contract on the same tasks. |
| Durable continuity | The session is an append-only source of truth; model history, resume, fork, transcript, telemetry, and request reconstruction derive from logged events (`docs/architecture.md:92-96`; `packages/core/session/README.md:5-16,61-73`). Semantic checkpoints flush before a model request, before a top-level external-effect tool, and before the next step (`packages/session/session-checkpoint-policy/README.md:5-23`). | This is the right continuity model. Map the existing trajectory and checkpoint records to the same turn/step/model/tool/verifier concepts without replacing the working runner first. |
| Crash ambiguity | Recovery distinguishes a request whose tool never started from a durable call whose outcome is unknown; the latter is not blindly replayed (`packages/session/session-persistence-jsonl/README.md:40-60`). | Adopt this rule for future interactive resume. It prevents duplicated side effects while allowing read-only or idempotent recovery. |
| Telemetry | Capture is a separate plugin and the base profile leaves export disabled. If enabled, the service ships no redaction rules and can export complete event bodies (`packages/bundle/base/cordis.patch.yml:129-160`; `packages/session/session-telemetry/README.md:19-35,45-49`). | Keep upstream export disabled. The command center retains local evidence and applies its own secret filtering before any future sharing. |

## Adopt now

1. Use one append-only execution ledger for the future interactive layer, with explicit turn, step, model request, tool/edit, verifier, and terminal records. Preserve exact provider/model identity, prompt/request hashes, output, diff, timings, and verification result. The current `continuity-trajectory.json` already captures the required evidence fields as a bounded-run snapshot; it is not yet an event store and does not need to become one before the interactive layer exists.
2. Use semantic checkpoints at the three meaningful boundaries: before model dispatch, before any future side-effecting tool dispatch, and before the next model step. Checkpoint count is not a quality goal; these three boundaries make restart state unambiguous.
3. Keep the headless lifecycle semantics for bounded work: one task, one fresh run, wait for owned work to settle, flush evidence, return a nonzero failure status when the terminal turn did not complete.
4. Preserve the ordered tool outcome pipeline: policy before the body, one authoritative normalized result after the body, and independent verification outside the implementer's request context.
5. Preserve exact provider/model attribution and treat model routes as replaceable adapters, not hard-coded names in task logic.

These are architecture contracts, not a request to port DeepSeek's 7,800-file implementation into this repository.

## Wrap as an optional adapter

The future `DeepSeekHarnessAdapter` should sit behind the same bounded task capsule and normalized JSON report as the existing thin lane:

```text
task capsule -> existing EXCALIBUR backend switch -> isolated stage
             -> pinned DeepSeek headless adapter -> normalized trajectory/diff
             -> existing hidden tests -> existing independent verifier
```

The adapter should own only Harness translation. EXCALIBUR's Scheduled Tasks continue to own elevated backend start, stop, and exact process ownership; the Harness talks to the resulting loopback HTTP endpoint. DeepSeek permission settings are application policy, not Windows elevation, so they cannot replace the already working no-UAC lifecycle path.

Build a command-center-specific headless profile rather than using either shipped profile unchanged. The shipped headless template is exactly `base + headless` (`packages/boot/app-boot/src/profile.ts:113-125`) and therefore inherits the broad base tool roster. The shipped `minimal` Agent preset is also unsuitable as-is: it mounts a persistent shell and deliberately shadows the sandboxed filesystem with bare `fs-local` (`apps/cli/config/agent-presets/minimal/agent.cordis.yml:1-7,21-88`). A bounded adapter must expose only the commands and mutable paths named by the task capsule and must emit the same evidence fields as the thin lane.

Before any promotion, require one disposable cross-check: the adapter must complete the same frozen task under the same one-response budget, produce an equivalent or better patch, pass the same hidden test, and return truthful trajectory data. Failure leaves the existing runner untouched.

## Defer

- **Code Mode.** Its typed SDK and nested dispatch are promising for multi-tool research and orchestration, but it adds another model contract. Compare `native`, `code`, and the current structured-patch response on the same Qwen tasks before choosing it.
- **Interactive Web UI and Trajectory view.** The UI is a useful operator surface after the headless adapter proves outcome parity. The trajectory component is a projection over session events, not the source of truth (`packages/client/ui-trajectory/README.md:5-17`).
- **Dynamic profiles, HMR, and third-party bundles.** Pin one reviewed composition first. The project warns that compatibility breaks are expected, and a patch replaces a complete row config.
- **Workflow and large subagent orchestration.** DeepSeek can fan out model-written workflows, including explicit provider/model overrides (`packages/workflow/tool-workflow/src/index.ts:138-148`), but it does not automatically know which local model is qualified for a task. Add a role only when the tournament demonstrates a measurable benefit.
- **Windows ACL confinement.** Revisit only if an interactive tool-using agent needs an OS-enforced write boundary that the simpler stage-copy protocol cannot provide.

## Reject from the production default

- **Automatic model retry.** The base bundle mounts `dsh-llm-retry`, whose normal default allows five retries and whose `always` mode can retry forever (`packages/bundle/base/cordis.patch.yml:69-73`; `packages/llm/llm-retry/README.md:5-12,39-52`). Qualification remains one model response. A future interactive lane gets a small explicit budget and the existing two-identical-failure circuit breaker.
- **Autonomous Ralph loops.** The base profile configures up to 64 rounds and only starts repeat reminders at calls 3, 5, and 8 (`packages/bundle/base/cordis.patch.yml:377-394`). That conflicts with the command center's outcome-first stopping rule.
- **Approval prompts as an automation strategy.** The base default is `workspace-write + ask`; `danger-full-access + never` disables prompts, but `never` rejects approval-required escalation rather than auto-approving it (`packages/bundle/base/cordis.patch.yml:166-205`; `packages/interaction/user-approval/README.md:5-13,29-33`). Backend elevation stays in pre-owned Scheduled Tasks.
- **The Windows ACL runner as today's default.** It requires caller-owned directories, can spend tens of seconds or minutes propagating the first workspace grant, restricts writes but not reads or network, reports only partial enforcement, and cannot capture piped output from confined grandchildren (`packages/sandbox/sandbox-windows-acl/README.md:73-82,93-102`). It would add latency and failure modes to a stage boundary that already works.
- **Unrestricted third-party or self-authored plugins.** A user preset is as privileged as the plugins it names (`packages/preset/agent-presets/README.md:133-135`). No plugin is installed or activated merely because it is discoverable.
- **Raw outbound telemetry.** Export stays disabled unless a reviewed redaction policy and explicit destination are supplied.

## Promotion boundary

DeepSeek Harness becomes a production execution option only after its pinned adapter repeatedly matches or beats the thin lane on diagnosis, scoped edit, hidden verification, elapsed time, and truthful telemetry. It becomes the interactive continuity substrate only after restart/resume proves that incomplete tool calls are classified correctly and no backend lifecycle action produces UAC prompts. Until then, it is an official, high-value upstream reference and experimental adapter target—not the production authority.

## Pinned primary source

- Repository: https://github.com/deepseek-ai/deepseek-harness
- Audited commit: https://github.com/deepseek-ai/deepseek-harness/tree/141eb6fef83422698aef7a981029e843e8161534
