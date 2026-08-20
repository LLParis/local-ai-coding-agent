# Slice A — EXCALIBUR Ollama lifecycle proof

Captured on 2026-08-19 PDT / 2026-08-20 UTC.

## Outcome

Slice A passed. The legacy ownerless Ollama process was reconciled by its exact audited PID/start-time identity, the hardened Scheduled Task runtime was deployed without pulling a model, two external stop/start cycles killed the exact child and restored fresh ownership, the installer reran idempotently, and the existing Mac SSH tunnel recovered without replacement.

This is service-lifecycle and connectivity evidence only. It is not evidence that any local model is qualified to edit user work.

## Deployed artifacts

- Source/runtime script: `Start-ExcaliburOllama.ps1`
  - SHA-256: `13483e018651f68653ccfa9d7909ea06a82cdb7ca27c03adae57bee904b8c8a7`
- Installer: `Install-ExcaliburOllama.ps1`
  - SHA-256: `02827e3b3631dfc90b70fea3c67171e540f2391dd4f71af06d9c60ec09ed33d8`
- Production runtime: `C:\Users\sirlo\AppData\Local\AnimeFrontier\AgentContinuity\Start-ExcaliburOllama.ps1`
- Task: `AnimeFrontier Excalibur Ollama`
- Task action: canonical `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`, with the hardened runtime path as its exact `-File` argument.
- Endpoint: exact IPv4 loopback `127.0.0.1:11434`.

The legacy AnimeFrontier task/path names were deliberately retained during recovery to avoid mixing lifecycle repair with Slice C generalization.

## Pre-deployment gates

- The final source hashes matched their staged EXCALIBUR copies.
- Windows PowerShell 5.1.26100.9168 parser: zero errors.
- PowerShell 7.6.5 parser: zero errors.
- Harmless `-ValidateOnly` Job Object interop test: exit 0; kill-on-close configured.
- Existing task arguments matched the installer expression byte-for-byte: 171 UTF-8 bytes.
- A Windows PowerShell 5.1 disposable control proved the exact four-argument `File.Replace` topology with a real rollback path:
  - source removed;
  - destination contained the replacement;
  - rollback artifact contained the prior destination.
- Semantic review found no remaining P0/P1 deployment blocker.

## Exact legacy reconciliation

The installer accepted only this one-time audited tuple:

- PID: `7236`
- Start UTC: `2026-08-20T04:35:13.1837982Z`
- Executable: `C:\Users\sirlo\AppData\Local\Programs\Ollama\ollama.exe`
- Command: exact `ollama.exe serve` form
- Listener: sole `127.0.0.1:11434`, owned by PID 7236
- Original parent PID 22152: absent
- Owner record: absent

It did not run a broad name kill or stop an unresolved port owner.

## Lifecycle evidence

### Initial deployment

- Instance: `7405d24b-12ed-4616-8770-e97017a6f099`
- Wrapper: PID 44280, start `2026-08-20T05:49:05.8746692Z`
- Server: PID 2772, start `2026-08-20T05:49:06.7651311Z`
- Exact parent relation: 44280 → 2772
- Task Running; schema-v2 owner matched live paths, commands, start times, runtime hash, endpoint, listener PID, named Job Object, and assignment flags.
- Real `/v1/responses` output matched `EXCALIBUR_LIFECYCLE_OK` exactly.

### First external stop/start

`Stop-ScheduledTask` reached the bounded stopped state:

- Task Ready
- Wrapper PID 44280 absent
- Server PID 2772 absent
- No matching runtime wrapper
- No exact `ollama serve` process
- No port 11434 listener

The remaining owner file was proven to be a stale lease for that exact dead instance. A fresh task start replaced it with:

- Instance: `730f74cb-b0ee-4ab1-b197-247d4bf93ee7`
- Wrapper/server: 23180 → 20812
- Both start identities newer than the prior instance
- Ownership, API surfaces, inventory, and exact Responses probe all passed.

### Idempotent installer rerun

The repaired installer was rerun with `-SkipPull` and no legacy override:

- It validated the live schema-v2 owner before stopping anything.
- It created task XML and runtime rollback artifacts.
- It atomically redeployed the same runtime hash.
- It registered exactly one task and started exactly one wrapper/child/listener chain.
- It reported model inventory preserved.
- New instance: `9e7be6c5-f694-492f-8241-0de6b9ca0784`, 43892 → 11760.

### Second external stop/start and final state

The second direct stop again proved the prior wrapper, child, exact serve process, and listener absent. The final fresh start passed all independent checks:

- Task: Running, Enabled, `IgnoreNew`
- Instance: `05b9d574-b485-46db-adee-bd769c8a26ce`
- Wrapper: PID 20908, start `2026-08-20T05:52:33.7409973Z`
- Server: PID 668, start `2026-08-20T05:52:34.6545844Z`
- Exact parent relation: 20908 → 668
- Listener: exactly one `127.0.0.1:11434`, owned by PID 668
- Runtime SHA-256: `13483e018651f68653ccfa9d7909ea06a82cdb7ca27c03adae57bee904b8c8a7`
- Ollama: `0.32.13`
- Exact Responses probe: passed

## Model preservation

The inventory remained identical before deployment, after deployment, after each restart, and after the idempotent installer rerun:

| Name | Digest | Size |
|---|---|---:|
| `gpt-oss:20b` | `17052f91a42e97930aa6e28a6c6c06a983e6a58dbb00434885a0cf5313e376f7` | 13,793,441,244 bytes |
| `gpt-oss-20b:latest` | `17052f91a42e97930aa6e28a6c6c06a983e6a58dbb00434885a0cf5313e376f7` | 13,793,441,244 bytes |
| `qwen3.6:27b-q6` | `b061b23b6727fcfdf4c2987a513c0f1e5192b58d116d9c281551d5c4622596b9` | 22,082,528,973 bytes |

No model pull ran. The standalone Qwen3.8 GGUF installation on `D:` was outside this Ollama store and was not modified.

## Mac edge recovery

- Existing tunnel process remained PID 84123 throughout the service outages.
- Listener remained private at Mac `127.0.0.1:12434`.
- Final package-native endpoint gate through the tunnel:
  - models: 3
  - models latency: 224 ms
  - Responses latency: 785 ms
  - status: `ready`

## Remaining notes

- Forced Scheduled Task termination cannot run the wrapper's `finally` block, so stopped state may retain a stale owner lease. The verifier accepts it only when its exact wrapper/server PID-and-start identities are proven dead; a fresh wrapper holds the singleton lock, proves the port empty, and replaces the stale lease.
- SSH repeatedly warned that the connection did not negotiate a post-quantum key exchange. The private transport worked and this did not block Slice A, but transport hardening remains follow-up work.
- Health prompts and these connectivity checks are explicitly marked non-qualification evidence. Model promotion remains gated on repeated diagnosis → safe edit → verification tasks in Slice B and the tournament.
