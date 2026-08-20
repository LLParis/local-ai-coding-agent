param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Qwen38", "Qwen38Native", "Ollama")]
    [string]$Backend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$endpointQwen = "http://127.0.0.1:8818"
$endpointOllama = "http://127.0.0.1:11434"
$backendDefinitions = @{
    Qwen38 = [ordered]@{
        task = "Coding Intelligence Excalibur Qwen3.8"
        port = 8818
        profileName = "Bounded"
        profile = "q6-text/medium/q8_0/32768/mtp3"
        alias = "arm-qwen38-q6-text"
        contextTokens = 32768
        cacheType = "q8_0"
        reasoningBudget = 2048
        mtp = $true
        ownerPath = Join-Path $env:LOCALAPPDATA "CodingIntelligence\Qwen38\qwen38-owner.json"
    }
    Qwen38Native = [ordered]@{
        task = "Coding Intelligence Excalibur Qwen3.8 Native"
        port = 8818
        profileName = "Native"
        profile = "q6-text/medium/q4_0/262144/mtp-off"
        alias = "arm-qwen38-q6-native-262k"
        contextTokens = 262144
        cacheType = "q4_0"
        reasoningBudget = 16384
        mtp = $false
        ownerPath = Join-Path $env:LOCALAPPDATA "CodingIntelligence\Qwen38Native\qwen38-owner.json"
    }
    Ollama = [ordered]@{
        task = "AnimeFrontier Excalibur Ollama"
        port = 11434
        ownerPath = Join-Path $env:LOCALAPPDATA "AnimeFrontier\AgentContinuity\service-owner.json"
    }
}

function Get-TaskState {
    param([Parameter(Mandatory = $true)][string]$Name)
    return [string](Get-ScheduledTask -TaskName $Name -ErrorAction Stop).State
}

function Get-PortListeners {
    param([Parameter(Mandatory = $true)][int]$Port)
    return @(
        Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object { [int]$_.LocalPort -eq $Port }
    )
}

function Wait-BackendStopped {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [int[]]$ProcessIds = @()
    )
    $definition = $backendDefinitions[$Name]
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $state = Get-TaskState -Name ([string]$definition.task)
        $listeners = @(Get-PortListeners -Port ([int]$definition.port))
        $liveProcessIds = @(
            foreach ($candidateId in @($ProcessIds | Select-Object -Unique)) {
                if ($candidateId -gt 0) {
                    $process = Get-Process -Id $candidateId -ErrorAction SilentlyContinue
                    if ($null -ne $process) {
                        $process.Dispose()
                        $candidateId
                    }
                }
            }
        )
        if (
            $state -ne "Running" -and
            $listeners.Count -eq 0 -and
            $liveProcessIds.Count -eq 0
        ) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "$Name did not stop within $TimeoutSeconds seconds."
}

function Stop-Backend {
    param([Parameter(Mandatory = $true)][string]$Name)
    $definition = $backendDefinitions[$Name]
    $owner = $null
    if (Test-Path -LiteralPath $definition.ownerPath -PathType Leaf) {
        $owner = Get-Content -LiteralPath $definition.ownerPath -Raw | ConvertFrom-Json
        if (
            [int]$owner.schemaVersion -ne 2 -or
            [string]$owner.taskName -ne [string]$definition.task
        ) {
            throw "$Name has an unrecognized owner record; refusing teardown."
        }
    }
    $ownedProcessIds = if ($null -eq $owner) {
        @()
    } else {
        @([int]$owner.wrapperPid, [int]$owner.serverPid)
    }
    if ((Get-TaskState -Name ([string]$definition.task)) -eq "Running") {
        Stop-ScheduledTask -TaskName ([string]$definition.task)
        Wait-BackendStopped -Name $Name -TimeoutSeconds 45 -ProcessIds $ownedProcessIds
    } elseif ($ownedProcessIds.Count -gt 0) {
        foreach ($candidateId in $ownedProcessIds) {
            $process = Get-Process -Id $candidateId -ErrorAction SilentlyContinue
            if ($null -ne $process) {
                $process.Dispose()
                throw "$Name is not Running but an owner-record process still exists."
            }
        }
    }
    if ($null -ne $owner -and (Test-Path -LiteralPath $definition.ownerPath -PathType Leaf)) {
        $currentOwner = Get-Content -LiteralPath $definition.ownerPath -Raw | ConvertFrom-Json
        if (
            [string]$currentOwner.instanceId -ne [string]$owner.instanceId -or
            [int]$currentOwner.wrapperPid -ne [int]$owner.wrapperPid -or
            [int]$currentOwner.serverPid -ne [int]$owner.serverPid
        ) {
            throw "$Name owner record changed during teardown; refusing cleanup."
        }
        Remove-Item -LiteralPath $definition.ownerPath -Force
    }
}

function Get-ProcessIdentity {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    if ($null -eq $process) {
        throw "Process $ProcessId has no Win32_Process identity."
    }
    return $process
}

function Assert-QwenReady {
    param([Parameter(Mandatory = $true)][string]$Name)
    $definition = $backendDefinitions[$Name]
    if ((Get-TaskState -Name ([string]$definition.task)) -ne "Running") {
        throw "$Name Scheduled Task is not Running."
    }
    $listeners = @(Get-PortListeners -Port 8818)
    if (
        $listeners.Count -ne 1 -or
        [string]$listeners[0].LocalAddress -ne "127.0.0.1"
    ) {
        throw "$Name does not own one exact IPv4-loopback listener."
    }
    if (-not (Test-Path -LiteralPath $definition.ownerPath -PathType Leaf)) {
        throw "$Name owner record is missing."
    }
    $owner = Get-Content -LiteralPath $definition.ownerPath -Raw | ConvertFrom-Json
    $typedProfile = $owner.PSObject.Properties.Name -contains "profileName"
    $profileMatches = if ($typedProfile) {
        [string]$owner.profileName -eq [string]$definition.profileName -and
        [string]$owner.profile -eq [string]$definition.profile -and
        [string]$owner.modelAlias -eq [string]$definition.alias -and
        [int]$owner.contextTokens -eq [int]$definition.contextTokens -and
        [string]$owner.cacheType -eq [string]$definition.cacheType -and
        [bool]$owner.mtp -eq [bool]$definition.mtp -and
        [int]$owner.reasoningBudget -eq [int]$definition.reasoningBudget
    } else {
        $Name -eq "Qwen38" -and
        [string]$owner.profile -eq [string]$definition.profile -and
        [string]$owner.modelAlias -eq [string]$definition.alias
    }
    if (
        [int]$owner.schemaVersion -ne 2 -or
        [string]$owner.taskName -ne [string]$definition.task -or
        [string]$owner.endpoint -ne $endpointQwen -or
        [int]$owner.localPort -ne 8818 -or
        [int]$owner.serverPid -ne [int]$listeners[0].OwningProcess -or
        -not $profileMatches -or
        -not [bool]$owner.jobKillOnClose -or
        -not [bool]$owner.jobAssignmentVerified
    ) {
        throw "$Name owner record does not attest the exact selected profile."
    }
    $server = Get-ProcessIdentity -ProcessId ([int]$owner.serverPid)
    $wrapper = Get-ProcessIdentity -ProcessId ([int]$owner.wrapperPid)
    if (
        [int]$server.ParentProcessId -ne [int]$wrapper.ProcessId -or
        [int]$owner.serverParentPid -ne [int]$wrapper.ProcessId -or
        -not [string]::Equals(
            [string]$server.ExecutablePath,
            [string]$owner.serverExecutablePath,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "$Name wrapper-child ownership is not exact."
    }
    $command = [string]$server.CommandLine
    $requiredCommandFragments = @(
        "--alias $($definition.alias)",
        "--ctx-size $($definition.contextTokens)",
        "--parallel 1",
        "--cache-type-k $($definition.cacheType)",
        "--cache-type-v $($definition.cacheType)",
        "--reasoning-effort medium",
        "--reasoning-budget $($definition.reasoningBudget)"
    )
    foreach ($fragment in $requiredCommandFragments) {
        if ($command.IndexOf($fragment, [StringComparison]::Ordinal) -lt 0) {
            throw "$Name command line is missing exact fragment: $fragment"
        }
    }
    if ([bool]$definition.mtp) {
        if (
            $command.IndexOf("--spec-type draft-mtp", [StringComparison]::Ordinal) -lt 0 -or
            $command.IndexOf("--spec-draft-n-max 3", [StringComparison]::Ordinal) -lt 0
        ) {
            throw "$Name command line does not attest MTP3."
        }
    } elseif ($command.IndexOf("--spec-type none", [StringComparison]::Ordinal) -lt 0) {
        throw "$Name command line does not attest MTP-off."
    }
    $health = Invoke-RestMethod -Uri "$endpointQwen/health" -TimeoutSec 3
    $models = Invoke-RestMethod -Uri "$endpointQwen/v1/models" -TimeoutSec 3
    $props = Invoke-RestMethod -Uri "$endpointQwen/props" -TimeoutSec 3
    $modelIds = @($models.data | ForEach-Object { [string]$_.id })
    if (
        [string]$health.status -ne "ok" -or
        [string]$definition.alias -notin $modelIds -or
        [string]$props.model_alias -ne [string]$definition.alias -or
        [int]$props.default_generation_settings.n_ctx -ne [int]$definition.contextTokens -or
        [int]$props.total_slots -ne 1 -or
        [string]$props.model_ftype -ne "Q6_K" -or
        [bool]$props.modalities.vision
    ) {
        throw "$Name health/model/props do not match the exact selected profile."
    }
    return [pscustomobject]@{
        owner = $owner
        listenerPid = [int]$listeners[0].OwningProcess
        props = $props
    }
}

function Assert-OllamaReady {
    $definition = $backendDefinitions.Ollama
    if ((Get-TaskState -Name ([string]$definition.task)) -ne "Running") {
        throw "Ollama Scheduled Task is not Running."
    }
    $listeners = @(Get-PortListeners -Port 11434)
    if (
        $listeners.Count -ne 1 -or
        [string]$listeners[0].LocalAddress -ne "127.0.0.1"
    ) {
        throw "Ollama does not own one exact IPv4-loopback listener."
    }
    $owner = Get-Content -LiteralPath $definition.ownerPath -Raw | ConvertFrom-Json
    if (
        [int]$owner.schemaVersion -ne 2 -or
        [string]$owner.taskName -ne [string]$definition.task -or
        [int]$owner.serverPid -ne [int]$listeners[0].OwningProcess -or
        -not [bool]$owner.jobKillOnClose -or
        -not [bool]$owner.jobAssignmentVerified
    ) {
        throw "Ollama owner record does not match the exact listener."
    }
    $version = Invoke-RestMethod -Uri "$endpointOllama/api/version" -TimeoutSec 3
    if ([string]::IsNullOrWhiteSpace([string]$version.version)) {
        throw "Ollama version endpoint is not ready."
    }
    return [pscustomobject]@{
        owner = $owner
        listenerPid = [int]$listeners[0].OwningProcess
    }
}

function Wait-BackendReady {
    param([Parameter(Mandatory = $true)][string]$Name)
    $timeoutSeconds = if ($Name -eq "Qwen38Native") {
        180
    } elseif ($Name -eq "Qwen38") {
        120
    } else {
        60
    }
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSeconds)
    $lastError = $null
    do {
        try {
            if ($Name -eq "Ollama") {
                return Assert-OllamaReady
            }
            return Assert-QwenReady -Name $Name
        } catch {
            $lastError = $_
            Start-Sleep -Milliseconds 500
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "$Name did not reach exact readiness within $timeoutSeconds seconds: $($lastError.Exception.Message)"
}

function Assert-OtherBackendsStopped {
    param([Parameter(Mandatory = $true)][string]$Selected)
    foreach ($name in @("Qwen38", "Qwen38Native", "Ollama")) {
        if ($name -eq $Selected) {
            continue
        }
        $definition = $backendDefinitions[$name]
        if ((Get-TaskState -Name ([string]$definition.task)) -eq "Running") {
            throw "$name remained Running after selecting $Selected."
        }
    }
    if ($Selected -eq "Ollama") {
        if (@(Get-PortListeners -Port 8818).Count -ne 0) {
            throw "A Qwen listener remained after selecting Ollama."
        }
    } elseif (@(Get-PortListeners -Port 11434).Count -ne 0) {
        throw "An Ollama listener remained after selecting $Selected."
    }
}

function Get-RunningBackend {
    $running = @(
        foreach ($name in @("Qwen38", "Qwen38Native", "Ollama")) {
            if ((Get-TaskState -Name ([string]$backendDefinitions[$name].task)) -eq "Running") {
                $name
            }
        }
    )
    if ($running.Count -gt 1) {
        throw "Multiple managed inference tasks are Running: $($running -join ', ')"
    }
    if ($running.Count -eq 0) {
        return $null
    }
    return $running[0]
}

function Start-AndProveBackend {
    param([Parameter(Mandatory = $true)][string]$Name)
    $definition = $backendDefinitions[$Name]
    if ((Get-TaskState -Name ([string]$definition.task)) -ne "Running") {
        Start-ScheduledTask -TaskName ([string]$definition.task)
    }
    $proof = Wait-BackendReady -Name $Name
    Assert-OtherBackendsStopped -Selected $Name
    return $proof
}

$started = [Diagnostics.Stopwatch]::StartNew()
$previous = Get-RunningBackend
if ($null -ne $previous) {
    Wait-BackendReady -Name $previous | Out-Null
    Assert-OtherBackendsStopped -Selected $previous
}
$proof = $null

try {
    foreach ($name in @("Qwen38", "Qwen38Native", "Ollama")) {
        if ($name -ne $Backend) {
            Stop-Backend -Name $name
        }
    }
    $proof = Start-AndProveBackend -Name $Backend
} catch {
    $switchError = $_
    try {
        foreach ($name in @("Qwen38", "Qwen38Native", "Ollama")) {
            Stop-Backend -Name $name
        }
        if ($null -ne $previous) {
            Start-AndProveBackend -Name $previous | Out-Null
        }
    } catch {
        throw "Backend switch failed: $($switchError.Exception.Message). Exact rollback also failed: $($_.Exception.Message)"
    }
    throw "Backend switch failed; exact previous backend '$previous' was restored: $($switchError.Exception.Message)"
}

$started.Stop()
$definition = $backendDefinitions[$Backend]
[pscustomobject]@{
    status = "ready"
    backend = $Backend
    profileName = if ($Backend -eq "Ollama") { $null } else { [string]$definition.profileName }
    profile = if ($Backend -eq "Ollama") { $null } else { [string]$definition.profile }
    modelAlias = if ($Backend -eq "Ollama") { $null } else { [string]$definition.alias }
    port = [int]$definition.port
    listenerPid = [int]$proof.listenerPid
    swapSeconds = [math]::Round($started.Elapsed.TotalSeconds, 3)
    previousBackend = $previous
    qwenTaskState = Get-TaskState -Name ([string]$backendDefinitions.Qwen38.task)
    qwenNativeTaskState = Get-TaskState -Name ([string]$backendDefinitions.Qwen38Native.task)
    ollamaTaskState = Get-TaskState -Name ([string]$backendDefinitions.Ollama.task)
    mutualExclusionProven = $true
    exactOwnershipProven = $true
    uacPrompt = $false
} | ConvertTo-Json -Compress
