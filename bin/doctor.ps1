param([switch]$Json)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Read-only by design: no task lifecycle calls, model loads, downloads, hashes,
# cache writes, or elevation.
$repoRoot = Split-Path -Parent $PSScriptRoot
$qwenTaskName = "Coding Intelligence Excalibur Qwen3.8"
$qwenNativeTaskName = "Coding Intelligence Excalibur Qwen3.8 Native"
$ollamaTaskName = "AnimeFrontier Excalibur Ollama"
$qwenUrl = "http://127.0.0.1:8818"
$ollamaUrl = "http://127.0.0.1:11434"
$researchRoot = "D:\11_CS\00_REPOS\AI Research Mastery"
$qwenModelRoot = Join-Path $researchRoot "models\Qwen3.8-27B-GGUF"
$ollamaRoot = Join-Path $env:USERPROFILE ".ollama\models"

function Get-RepositoryState {
    $version = "unknown"
    $match = [regex]::Match(
        (Get-Content -LiteralPath (Join-Path $repoRoot "pyproject.toml") -Raw),
        '(?m)^version\s*=\s*"([^"]+)"'
    )
    if ($match.Success) { $version = $match.Groups[1].Value }

    $head = $null
    $branch = $null
    $dirty = $null
    $errorText = $null
    try {
        if ($null -eq (Get-Command git.exe -ErrorAction SilentlyContinue)) {
            throw "git.exe is not available on PATH"
        }
        $head = ((@(& git.exe -C $repoRoot rev-parse HEAD 2>$null)) -join "").Trim()
        if ($LASTEXITCODE -ne 0) { throw "git rev-parse failed" }
        $branch = ((@(& git.exe -C $repoRoot branch --show-current 2>$null)) -join "").Trim()
        if ($LASTEXITCODE -ne 0) { throw "git branch failed" }
        if ([string]::IsNullOrWhiteSpace($branch)) { $branch = "detached" }
        $changes = @(& git.exe -C $repoRoot status --porcelain=v1 --untracked-files=normal 2>$null)
        if ($LASTEXITCODE -ne 0) { throw "git status failed" }
        $dirty = $changes.Count -gt 0
    } catch {
        $errorText = $_.Exception.Message
    }
    [pscustomobject]@{
        root = $repoRoot
        packageVersion = $version
        branch = $branch
        commit = $head
        dirty = $dirty
        error = $errorText
    }
}

function Get-TaskState {
    param([string]$Name)
    try {
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $Name -ErrorAction SilentlyContinue
        $taskXml = [xml](Export-ScheduledTask -TaskName $Name -ErrorAction Stop)
        $triggerCount = @(
            $taskXml.SelectNodes("/*[local-name()='Task']/*[local-name()='Triggers']/*")
        ).Count
        $last = if ($null -eq $info) { $null } else { [long]$info.LastTaskResult }
        [pscustomobject]@{
            name = $Name
            installed = $true
            state = [string]$task.State
            principal = [string]$task.Principal.UserId
            runLevel = [string]$task.Principal.RunLevel
            logonType = [string]$task.Principal.LogonType
            triggerCount = $triggerCount
            lastResult = $last
            lastResultHex = if ($null -eq $last) { $null } else { "0x{0:X8}" -f ([uint32]$last) }
            error = $null
        }
    } catch {
        [pscustomobject]@{
            name = $Name
            installed = $false
            state = "Missing"
            principal = $null
            runLevel = $null
            logonType = $null
            triggerCount = $null
            lastResult = $null
            lastResultHex = $null
            error = $_.Exception.Message
        }
    }
}

try {
    $allListeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop)
    $listenerError = $null
} catch {
    $allListeners = @()
    $listenerError = $_.Exception.Message
}

function Get-ListenerState {
    param([int]$Port)
    $details = @()
    foreach ($row in @($allListeners | Where-Object { [int]$_.LocalPort -eq $Port })) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$row.OwningProcess)" -ErrorAction SilentlyContinue
        $details += [pscustomobject]@{
            localAddress = [string]$row.LocalAddress
            localPort = [int]$row.LocalPort
            processId = [int]$row.OwningProcess
            processName = if ($null -eq $process) { $null } else { [string]$process.Name }
            executablePath = if ($null -eq $process) { $null } else { [string]$process.ExecutablePath }
        }
    }
    [pscustomobject]@{
        port = $Port
        count = $details.Count
        exactIpv4Loopback = $details.Count -eq 1 -and $details[0].localAddress -eq "127.0.0.1"
        anyNonLoopback = @($details | Where-Object { $_.localAddress -notin @("127.0.0.1", "::1") }).Count -gt 0
        listeners = $details
        discoveryError = $listenerError
    }
}

function Get-BackendHealth {
    param(
        [ValidateSet("Qwen38", "Qwen38Native", "Ollama")][string]$Backend,
        [object]$Listener
    )
    if ($Listener.count -eq 0) {
        return [pscustomobject]@{ status = "inactive"; health = $null; exactModelPresent = $false; version = $null; error = $null }
    }
    try {
        if ($Backend -in @("Qwen38", "Qwen38Native")) {
            $expectedAlias = if ($Backend -eq "Qwen38") {
                "arm-qwen38-q6-text"
            } else {
                "arm-qwen38-q6-native-262k"
            }
            $expectedContext = if ($Backend -eq "Qwen38") { 32768 } else { 262144 }
            $health = Invoke-RestMethod -Uri "$qwenUrl/health" -TimeoutSec 3
            $models = Invoke-RestMethod -Uri "$qwenUrl/v1/models" -TimeoutSec 3
            $props = Invoke-RestMethod -Uri "$qwenUrl/props" -TimeoutSec 3
            $exact = $expectedAlias -in @($models.data | ForEach-Object { [string]$_.id })
            $ready = (
                [string]$health.status -eq "ok" -and
                $exact -and
                [string]$props.model_alias -eq $expectedAlias -and
                [int]$props.default_generation_settings.n_ctx -eq $expectedContext -and
                [int]$props.total_slots -eq 1 -and
                [string]$props.model_ftype -eq "Q6_K" -and
                -not [bool]$props.modalities.vision
            )
            return [pscustomobject]@{
                status = if ($ready) { "ready" } else { "other-profile-or-unhealthy" }
                health = [string]$health.status
                exactModelPresent = $exact
                modelAlias = [string]$props.model_alias
                contextTokens = [int]$props.default_generation_settings.n_ctx
                modelType = [string]$props.model_ftype
                textOnly = -not [bool]$props.modalities.vision
                totalSlots = [int]$props.total_slots
                version = $null
                error = $null
            }
        }
        $version = Invoke-RestMethod -Uri "$ollamaUrl/api/version" -TimeoutSec 3
        $tags = Invoke-RestMethod -Uri "$ollamaUrl/api/tags" -TimeoutSec 3
        $names = @($tags.models | ForEach-Object { [string]$_.name })
        $exact = "gpt-oss-20b:latest" -in $names -or "gpt-oss:20b" -in $names
        $ready = -not [string]::IsNullOrWhiteSpace([string]$version.version) -and $exact
        [pscustomobject]@{
            status = if ($ready) { "ready" } else { "unhealthy" }
            health = "ok"
            exactModelPresent = $exact
            version = [string]$version.version
            error = $null
        }
    } catch {
        [pscustomobject]@{ status = "unreachable"; health = $null; exactModelPresent = $false; version = $null; error = $_.Exception.Message }
    }
}

function Get-OwnershipState {
    param(
        [string]$OwnerPath,
        [string]$TaskName,
        [string]$Endpoint,
        [object]$Listener,
        [object]$Task,
        [string]$ExpectedProfileName,
        [string]$ExpectedProfile,
        [string]$ExpectedAlias,
        [int]$ExpectedContextTokens,
        [string]$ExpectedCacheType,
        [bool]$ExpectedMtp
    )
    if ($Task.state -ne "Running") {
        return [pscustomobject]@{
            status = "standby"
            exact = $null
            ownerPath = $OwnerPath
            wrapperPid = $null
            serverPid = $null
            failures = @()
        }
    }
    if ($Listener.count -eq 0) {
        return [pscustomobject]@{ status = "mismatch"; exact = $false; ownerPath = $OwnerPath; wrapperPid = $null; serverPid = $null; failures = @("Running task has no listener") }
    }

    $failures = @()
    $owner = $null
    try {
        $owner = Get-Content -LiteralPath $OwnerPath -Raw -ErrorAction Stop | ConvertFrom-Json
    } catch {
        $failures += "owner record is missing or invalid"
    }
    if ($null -ne $owner) {
        $server = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$owner.serverPid)" -ErrorAction SilentlyContinue
        $wrapper = Get-Process -Id ([int]$owner.wrapperPid) -ErrorAction SilentlyContinue
        if ([string]$owner.taskName -ne $TaskName) { $failures += "task name mismatch" }
        if ([string]$owner.endpoint -ne $Endpoint) { $failures += "endpoint mismatch" }
        if (-not [string]::IsNullOrWhiteSpace($ExpectedProfileName)) {
            $hasTypedProfile = $owner.PSObject.Properties.Name -contains "profileName"
            if ($hasTypedProfile) {
                if ([string]$owner.profileName -ne $ExpectedProfileName) { $failures += "profile name mismatch" }
                if ([string]$owner.profile -ne $ExpectedProfile) { $failures += "profile contract mismatch" }
                if ([string]$owner.modelAlias -ne $ExpectedAlias) { $failures += "model alias mismatch" }
                if ([int]$owner.contextTokens -ne $ExpectedContextTokens) { $failures += "context mismatch" }
                if ([string]$owner.cacheType -ne $ExpectedCacheType) { $failures += "KV cache mismatch" }
                if ([bool]$owner.mtp -ne $ExpectedMtp) { $failures += "MTP mismatch" }
            } elseif (
                $ExpectedProfileName -ne "Bounded" -or
                [string]$owner.profile -ne $ExpectedProfile -or
                [string]$owner.modelAlias -ne $ExpectedAlias
            ) {
                $failures += "typed profile fields are missing"
            }
        }
        if ([string]$owner.localAddress -ne "127.0.0.1") { $failures += "owner is not IPv4 loopback" }
        if (-not [bool]$owner.jobKillOnClose -or -not [bool]$owner.jobAssignmentVerified) { $failures += "job ownership flags missing" }
        if ($Listener.count -ne 1 -or [int]$owner.serverPid -ne [int]$Listener.listeners[0].processId) { $failures += "listener PID mismatch" }
        if ([int]$owner.serverParentPid -ne [int]$owner.wrapperPid) { $failures += "recorded child mismatch" }
        if ($null -eq $server -or [int]$server.ParentProcessId -ne [int]$owner.wrapperPid) { $failures += "live child mismatch" }
        if ($null -ne $server -and -not [string]::Equals([string]$server.ExecutablePath, [string]$owner.serverExecutablePath, [StringComparison]::OrdinalIgnoreCase)) { $failures += "server executable mismatch" }
        if ($null -eq $wrapper) { $failures += "wrapper process missing" }
        if ($null -ne $wrapper) { $wrapper.Dispose() }
    }
    $exact = $failures.Count -eq 0
    [pscustomobject]@{
        status = if ($exact) { "exact" } else { "mismatch" }
        exact = $exact
        ownerPath = $OwnerPath
        wrapperPid = if ($null -eq $owner) { $null } else { [int]$owner.wrapperPid }
        serverPid = if ($null -eq $owner) { $null } else { [int]$owner.serverPid }
        failures = $failures
    }
}

function Get-DirectModel {
    param([string]$Name, [string]$Role, [string]$Path, [long]$ExpectedBytes)
    $file = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    $installed = $null -ne $file -and ($ExpectedBytes -eq 0 -or [long]$file.Length -eq $ExpectedBytes)
    [pscustomobject]@{
        name = $Name
        role = $Role
        source = "direct GGUF"
        installed = $installed
        bytes = if ($null -eq $file) { $null } else { [long]$file.Length }
        location = $Path
    }
}

function Get-OllamaModel {
    param([string]$Name, [string]$Role, [string]$ManifestRelativePath)
    $manifestPath = Join-Path (Join-Path $ollamaRoot "manifests") $ManifestRelativePath
    $installed = $false
    $bytes = $null
    $modelDigest = $null
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -ErrorAction Stop | ConvertFrom-Json
        $missing = 0
        $sum = [long]0
        foreach ($layer in @($manifest.layers)) {
            $sum += [long]$layer.size
            $blob = Join-Path (Join-Path $ollamaRoot "blobs") ([string]$layer.digest).Replace(":", "-")
            if (-not (Test-Path -LiteralPath $blob -PathType Leaf)) { $missing++ }
            if ([string]$layer.mediaType -eq "application/vnd.ollama.image.model") { $modelDigest = [string]$layer.digest }
        }
        $installed = @($manifest.layers).Count -gt 0 -and $missing -eq 0
        $bytes = $sum
    } catch {
        $installed = $false
    }
    [pscustomobject]@{
        name = $Name
        role = $Role
        source = "Ollama manifest"
        installed = $installed
        bytes = $bytes
        modelDigest = $modelDigest
        location = $manifestPath
    }
}

$repository = Get-RepositoryState
$qwenTask = Get-TaskState $qwenTaskName
$qwenNativeTask = Get-TaskState $qwenNativeTaskName
$ollamaTask = Get-TaskState $ollamaTaskName
$qwenListener = Get-ListenerState 8818
$ollamaListener = Get-ListenerState 11434
$qwenHealth = Get-BackendHealth Qwen38 $qwenListener
$qwenNativeHealth = Get-BackendHealth Qwen38Native $qwenListener
$ollamaHealth = Get-BackendHealth Ollama $ollamaListener
$qwenOwnership = Get-OwnershipState `
    (Join-Path $env:LOCALAPPDATA "CodingIntelligence\Qwen38\qwen38-owner.json") `
    $qwenTaskName $qwenUrl $qwenListener $qwenTask `
    "Bounded" "q6-text/medium/q8_0/32768/mtp3" "arm-qwen38-q6-text" 32768 "q8_0" $true
$qwenNativeOwnership = Get-OwnershipState `
    (Join-Path $env:LOCALAPPDATA "CodingIntelligence\Qwen38Native\qwen38-owner.json") `
    $qwenNativeTaskName $qwenUrl $qwenListener $qwenNativeTask `
    "Native" "q6-text/medium/q4_0/262144/mtp-off" "arm-qwen38-q6-native-262k" 262144 "q4_0" $false
$ollamaOwnership = Get-OwnershipState `
    (Join-Path $env:LOCALAPPDATA "AnimeFrontier\AgentContinuity\service-owner.json") `
    $ollamaTaskName $ollamaUrl $ollamaListener $ollamaTask "" "" "" 0 "" $false

$models = @(
    Get-DirectModel "Qwen3.8 27B Q6" "default local implementer" (Join-Path $qwenModelRoot "Qwen3.8-27B-Q6_K.gguf") 22884408288
    Get-OllamaModel "Devstral Small 2 24B" "local verifier" "registry.ollama.ai\library\devstral-small-2\24b"
    Get-OllamaModel "gpt-oss 20B alias" "tiny-task fallback" "registry.ollama.ai\library\gpt-oss-20b\latest"
    Get-DirectModel "Qwen3.8 27B Q8" "higher-fidelity reference" (Join-Path $qwenModelRoot "Qwen3.8-27B-Q8_0.gguf") 29047086048
    Get-OllamaModel "Gemma 4 31B QAT" "research and vision candidate" "hf.co\google\gemma-4-31B-it-qat-q4_0-gguf\latest"
    Get-OllamaModel "Qwen3.6 27B Q6" "prior candidate" "registry.ollama.ai\library\qwen3.6\27b-q6"
)

$qwenLive = $qwenTask.state -eq "Running" -and $qwenNativeTask.state -ne "Running" -and $qwenListener.exactIpv4Loopback -and $qwenHealth.status -eq "ready" -and $qwenOwnership.exact -eq $true -and $ollamaListener.count -eq 0
$qwenNativeLive = $qwenNativeTask.state -eq "Running" -and $qwenTask.state -ne "Running" -and $qwenListener.exactIpv4Loopback -and $qwenNativeHealth.status -eq "ready" -and $qwenNativeOwnership.exact -eq $true -and $ollamaListener.count -eq 0
$ollamaLive = $ollamaTask.state -eq "Running" -and $qwenTask.state -ne "Running" -and $qwenNativeTask.state -ne "Running" -and $ollamaListener.exactIpv4Loopback -and $ollamaHealth.status -eq "ready" -and $ollamaOwnership.exact -eq $true -and $qwenListener.count -eq 0
$runningTaskCount = @(
    @($qwenTask, $qwenNativeTask, $ollamaTask) |
        Where-Object { $_.state -eq "Running" }
).Count
$active = if ($runningTaskCount -gt 1 -or ($qwenListener.count -gt 0 -and $ollamaListener.count -gt 0)) { "Conflict" } elseif ($qwenLive) { "Qwen38" } elseif ($qwenNativeLive) { "Qwen38Native" } elseif ($ollamaLive) { "Ollama" } else { "None" }

$required = @($models | Where-Object { $_.name -in @("Qwen3.8 27B Q6", "Devstral Small 2 24B", "gpt-oss 20B alias") })
$configured = $qwenTask.installed -and $qwenNativeTask.installed -and $ollamaTask.installed -and @($required | Where-Object { -not $_.installed }).Count -eq 0
$evidence = @(
    "runs\qwen3.8-q6-capped\powershell.run.json",
    "runs\cross-language\qwen-typescript.json",
    "runs\devstral-verifier.json",
    "runs\final-end-to-end.json",
    "docs\TOURNAMENT_V1.md"
)
$evidencePresent = @($evidence | Where-Object { Test-Path -LiteralPath (Join-Path $repoRoot $_) -PathType Leaf })
$overall = if (-not $configured) { "attention" } elseif ($active -eq "Conflict") { "conflict" } elseif ($active -eq "None") { "stopped-or-unhealthy" } else { "ready" }
$next = if ($overall -eq "ready") {
    "Run: bin\coding-task.cmd D:\path\to\coding-task.json"
} elseif ($configured -and $active -ne "Conflict") {
    "Restore Qwen: powershell.exe -NoProfile -ExecutionPolicy Bypass -File windows\Switch-ExcaliburBackend.ps1 -Backend Qwen38"
} elseif ($active -eq "Conflict") {
    "Do not run a coding task until exact backend ownership is reconciled."
} else {
    "Review missing tasks or model entries below."
}

$report = [pscustomobject]@{
    schemaVersion = 1
    generatedAtUtc = [DateTime]::UtcNow.ToString("o")
    operation = [pscustomobject]@{ readOnly = $true; elevated = $false; changesMade = $false }
    overall = $overall
    activeBackend = $active
    nextAction = $next
    repository = $repository
    proofLevels = [pscustomobject]@{
        configured = [pscustomobject]@{ status = if ($configured) { "ready" } else { "incomplete" }; meaning = "Required tasks and core model artifacts exist." }
        tested = [pscustomobject]@{ status = if ($evidencePresent.Count -eq $evidence.Count) { "evidence-recorded" } else { "evidence-incomplete" }; evidence = $evidencePresent; meaning = "Historical real-task evidence; doctor does not rerun tests." }
        live = [pscustomobject]@{ status = if ($active -in @("Qwen38", "Qwen38Native", "Ollama")) { "ready" } else { "not-ready" }; meaning = "Exactly one owned loopback backend answers non-generating health checks now." }
    }
    backends = [pscustomobject]@{
        qwen38 = [pscustomobject]@{ profileName = "Bounded"; profile = "q6-text/medium/q8_0/32768/mtp3"; task = $qwenTask; listener = $qwenListener; health = $qwenHealth; ownership = $qwenOwnership; liveReady = $qwenLive }
        qwen38Native = [pscustomobject]@{ profileName = "Native"; profile = "q6-text/medium/q4_0/262144/mtp-off"; task = $qwenNativeTask; listener = $qwenListener; health = $qwenNativeHealth; ownership = $qwenNativeOwnership; liveReady = $qwenNativeLive }
        ollama = [pscustomobject]@{ task = $ollamaTask; listener = $ollamaListener; health = $ollamaHealth; ownership = $ollamaOwnership; liveReady = $ollamaLive }
    }
    models = $models
}

if ($Json) {
    $report | ConvertTo-Json -Depth 12
} else {
    $commit = if ([string]::IsNullOrWhiteSpace([string]$repository.commit)) { "unknown" } else { $repository.commit.Substring(0, [Math]::Min(12, $repository.commit.Length)) }
    $dirty = if ($repository.dirty -eq $true) { "dirty" } elseif ($repository.dirty -eq $false) { "clean" } else { "unknown" }
    Write-Output "Coding Intelligence Doctor"
    Write-Output "Overall: $($overall.ToUpperInvariant())"
    Write-Output "Active backend: $active"
    Write-Output "Repository: v$($repository.packageVersion) $($repository.branch)@$commit ($dirty)"
    Write-Output ""
    Write-Output "Proof levels"
    Write-Output "  Configured: $($report.proofLevels.configured.status)"
    Write-Output "  Tested:     $($report.proofLevels.tested.status) (recorded evidence; not rerun)"
    Write-Output "  Live:       $($report.proofLevels.live.status)"
    Write-Output ""
    Write-Output "Backends"
    Write-Output "  Qwen38 Bounded: task=$($qwenTask.state); listener=$($qwenListener.count) on 127.0.0.1:8818; health=$($qwenHealth.status); owner=$($qwenOwnership.status); profile=q6-text/medium/q8_0/32768/mtp3"
    Write-Output "  Qwen38 Native: task=$($qwenNativeTask.state); listener=$($qwenListener.count) on 127.0.0.1:8818; health=$($qwenNativeHealth.status); owner=$($qwenNativeOwnership.status); profile=q6-text/medium/q4_0/262144/mtp-off"
    Write-Output "  Ollama: task=$($ollamaTask.state); listener=$($ollamaListener.count) on 127.0.0.1:11434; health=$($ollamaHealth.status); owner=$($ollamaOwnership.status)"
    foreach ($item in @([pscustomobject]@{ name = "Qwen38 shared port"; value = $qwenListener }, [pscustomobject]@{ name = "Ollama"; value = $ollamaListener })) {
        foreach ($listener in @($item.value.listeners)) { Write-Output "    $($item.name) PID $($listener.processId): $($listener.executablePath)" }
    }
    Write-Output ""
    Write-Output "Installed models"
    foreach ($model in $models) {
        $mark = if ($model.installed) { "OK" } else { "MISSING" }
        Write-Output "  [$mark] $($model.name) - $($model.role)"
    }
    Write-Output ""
    Write-Output "Next: $next"
    Write-Output "Doctor made no changes and requested no elevation."
}

if ($overall -eq "ready") { exit 0 }
exit 1
