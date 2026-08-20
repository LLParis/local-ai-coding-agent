param(
    [ValidateSet("Bounded", "Native")]
    [string]$Profile = "Bounded",
    [switch]$ValidateOnly,
    [ValidateRange(0, 2147483647)]
    [int]$ExpectedUnmanagedServerPid = 0,
    [string]$ExpectedUnmanagedServerStartTimeUtc = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$profiles = @{
    Bounded = [ordered]@{
        taskName = "Coding Intelligence Excalibur Qwen3.8"
        otherQwenTaskName = "Coding Intelligence Excalibur Qwen3.8 Native"
        stateName = "Qwen38"
        profileId = "q6-text/medium/q8_0/32768/mtp3"
        modelAlias = "arm-qwen38-q6-text"
        contextTokens = 32768
        cacheType = "q8_0"
        reasoningBudget = 2048
        mtp = $true
        jobNamespace = "Qwen38"
    }
    Native = [ordered]@{
        taskName = "Coding Intelligence Excalibur Qwen3.8 Native"
        otherQwenTaskName = "Coding Intelligence Excalibur Qwen3.8"
        stateName = "Qwen38Native"
        profileId = "q6-text/medium/q4_0/262144/mtp-off"
        modelAlias = "arm-qwen38-q6-native-262k"
        contextTokens = 262144
        cacheType = "q4_0"
        reasoningBudget = 16384
        mtp = $false
        jobNamespace = "Qwen38Native"
    }
}
$selectedProfile = $profiles[$Profile]
$taskName = [string]$selectedProfile.taskName
$otherQwenTaskName = [string]$selectedProfile.otherQwenTaskName
$stateRoot = Join-Path $env:LOCALAPPDATA ("CodingIntelligence\{0}" -f $selectedProfile.stateName)
$runtimeScript = Join-Path $stateRoot "Start-ExcaliburQwen38.ps1"
$ownerPath = Join-Path $stateRoot "qwen38-owner.json"
$otherStateRoot = Join-Path $env:LOCALAPPDATA (
    "CodingIntelligence\{0}" -f $(if ($Profile -eq "Bounded") { "Qwen38Native" } else { "Qwen38" })
)
$otherOwnerPath = Join-Path $otherStateRoot "qwen38-owner.json"
$rollbackRoot = Join-Path $stateRoot "rollback"
$sourceRuntime = Join-Path $PSScriptRoot "Start-ExcaliburQwen38.ps1"
$windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$taskArguments = if ($Profile -eq "Bounded") {
    # Preserve the existing bounded task action byte-for-byte. The wrapper's
    # default profile is Bounded.
    '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $runtimeScript
} else {
    '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Profile Native' -f $runtimeScript
}
$endpoint = "http://127.0.0.1:8818"
$localPort = 8818
$projectRoot = "D:\11_CS\00_REPOS\AI Research Mastery"
$serverRuntimePath = Join-Path $projectRoot "artifacts\runtime\llama.cpp\b10435\bin\llama-server.exe"
$modelPath = Join-Path $projectRoot "models\Qwen3.8-27B-GGUF\Qwen3.8-27B-Q6_K.gguf"

$fixedServerArguments = @(
    "--model", $modelPath,
    "--alias", ([string]$selectedProfile.modelAlias),
    "--ctx-size", ([string]$selectedProfile.contextTokens),
    "--parallel", "1",
    "--gpu-layers", "999",
    "--flash-attn", "on",
    "--cache-type-k", ([string]$selectedProfile.cacheType),
    "--cache-type-v", ([string]$selectedProfile.cacheType),
    "--fit", "off",
    "--jinja",
    "--reasoning-format", "deepseek",
    "--host", "127.0.0.1",
    "--port", "8818",
    "--cors-origins", "localhost",
    "--no-cors-credentials",
    "--no-ui",
    "--metrics",
    "--no-context-shift",
    "--reasoning", "on",
    "--reasoning-effort", "medium",
    "--reasoning-budget", ([string]$selectedProfile.reasoningBudget),
    "--reasoning-preserve"
)
if ([bool]$selectedProfile.mtp) {
    $fixedServerArguments += @("--spec-type", "draft-mtp", "--spec-draft-n-max", "3")
} else {
    $fixedServerArguments += @("--spec-type", "none")
}

if (($ExpectedUnmanagedServerPid -eq 0) -ne ([string]::IsNullOrWhiteSpace($ExpectedUnmanagedServerStartTimeUtc))) {
    throw "Unmanaged reconciliation requires both the exact PID and start time, or neither."
}

$expectedUnmanagedStart = $null
if ($ExpectedUnmanagedServerPid -ne 0) {
    $parsedStart = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse(
        $ExpectedUnmanagedServerStartTimeUtc,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal,
        [ref]$parsedStart
    )) {
        throw "The expected unmanaged server start time is not a valid timestamp."
    }
    $expectedUnmanagedStart = $parsedStart.ToUniversalTime()
}

if (-not (Test-Path -LiteralPath $sourceRuntime -PathType Leaf)) {
    throw "The companion Qwen3.8 runtime wrapper is missing."
}
if (-not (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf)) {
    throw "Windows PowerShell 5.1 is missing from its canonical path."
}

function Assert-ScriptParses {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $Path,
        [ref]$tokens,
        [ref]$errors
    ) | Out-Null
    if (@($errors).Count -ne 0) {
        $details = @($errors | ForEach-Object {
            "line $($_.Extent.StartLineNumber): $($_.Message)"
        }) -join "; "
        throw "PowerShell parser rejected ${Path}: $details"
    }
}

function ConvertTo-SafeCommandLineArgument {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Value
    )

    if ($Value.IndexOf('"') -ge 0 -or $Value.IndexOf("`r") -ge 0 -or $Value.IndexOf("`n") -ge 0) {
        throw "A fixed llama-server argument contains an unsupported quote or newline."
    }
    if ($Value.Length -eq 0) {
        return '""'
    }
    if ($Value -match '\s') {
        return '"' + $Value + '"'
    }
    return $Value
}

function Get-FixedServerArgumentLine {
    return (@($fixedServerArguments | ForEach-Object {
        ConvertTo-SafeCommandLineArgument -Value ([string]$_)
    }) -join " ")
}

function Get-ProcessIdentity {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    $native = Get-Process -Id $ProcessId -ErrorAction Stop
    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if (-not $cim) {
            throw "Process $ProcessId has no Win32_Process identity."
        }
        $executablePath = if ([string]::IsNullOrWhiteSpace([string]$cim.ExecutablePath)) {
            ""
        } else {
            [System.IO.Path]::GetFullPath([string]$cim.ExecutablePath)
        }
        # Get-Process.StartTime can be unavailable for an SSH-session-owned
        # process even when Win32_Process exposes its creation identity.
        $creationTime = [DateTime]$cim.CreationDate
        return [pscustomobject]@{
            processId = $ProcessId
            parentProcessId = [int]$cim.ParentProcessId
            processName = [string]$native.ProcessName
            executablePath = $executablePath
            commandLine = [string]$cim.CommandLine
            startTimeUtc = $creationTime.ToUniversalTime().ToString("o")
        }
    } finally {
        $native.Dispose()
    }
}

function Test-SameUtcTimestamp {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )
    try {
        $leftTime = [DateTimeOffset]::Parse($Left).ToUniversalTime()
        $rightTime = [DateTimeOffset]::Parse($Right).ToUniversalTime()
        # Win32_Process.CreationDate is rounded slightly compared with
        # Get-Process.StartTime. PID, path, parent, command line, and listener
        # ownership are checked separately; tolerate only this sub-ms rounding.
        return [Math]::Abs($leftTime.UtcTicks - $rightTime.UtcTicks) -le (
            [TimeSpan]::TicksPerMillisecond
        )
    } catch {
        return $false
    }
}

function Assert-QwenServerIdentity {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Identity,
        [switch]$AllowProtectedMetadata
    )

    if ([string]$Identity.processName -ne "llama-server") {
        throw "Port 8818 is not owned by a llama-server process."
    }

    $expectedPath = [System.IO.Path]::GetFullPath($serverRuntimePath)
    if (-not [string]::IsNullOrWhiteSpace([string]$Identity.executablePath)) {
        if (-not [string]::Equals(
            [string]$Identity.executablePath,
            $expectedPath,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Port 8818 is owned by an unexpected llama-server executable."
        }
    } elseif (-not $AllowProtectedMetadata) {
        throw "The llama-server executable path is unavailable; exact ownership cannot be proven."
    }

    if (-not [string]::IsNullOrWhiteSpace([string]$Identity.commandLine)) {
        $argumentLine = Get-FixedServerArgumentLine
        $commandPattern = '^\s*"?' + [regex]::Escape($expectedPath) + '"?\s+' + [regex]::Escape($argumentLine) + '\s*$'
        if ([string]$Identity.commandLine -notmatch $commandPattern) {
            throw "The llama-server command line does not match the fixed Qwen3.8 profile."
        }
    } elseif (-not $AllowProtectedMetadata) {
        throw "The llama-server command line is unavailable; exact ownership cannot be proven."
    }
}

function Get-PortListenerRows {
    try {
        return @(
            Get-NetTCPConnection -State Listen -ErrorAction Stop |
                Where-Object { [int]$_.LocalPort -eq $localPort }
        )
    } catch {
        throw "Unable to inspect port $localPort; listener discovery failed."
    }
}

function Get-ExactListener {
    param(
        [switch]$AllowProtectedMetadata
    )

    $listeners = @(Get-PortListenerRows)
    if ($listeners.Count -eq 0) {
        return $null
    }
    if ($listeners.Count -ne 1) {
        throw "Expected zero or one listener on port $localPort; found $($listeners.Count)."
    }
    $listener = $listeners[0]
    if ([string]$listener.LocalAddress -ne "127.0.0.1") {
        throw "Port $localPort is exposed outside the exact IPv4 loopback address."
    }
    $identity = Get-ProcessIdentity -ProcessId ([int]$listener.OwningProcess)
    Assert-QwenServerIdentity -Identity $identity -AllowProtectedMetadata:$AllowProtectedMetadata
    return [pscustomobject]@{
        localAddress = [string]$listener.LocalAddress
        localPort = [int]$listener.LocalPort
        owningProcess = [int]$listener.OwningProcess
        process = $identity
    }
}

function Get-ExistingTask {
    try {
        $matches = @(
            Get-ScheduledTask -ErrorAction Stop |
                Where-Object {
                    [string]$_.TaskName -eq $taskName -and
                    [string]$_.TaskPath -eq "\"
                }
        )
    } catch {
        throw "Unable to inspect Scheduled Tasks; task ownership cannot be proven."
    }
    if ($matches.Count -gt 1) {
        throw "The Qwen3.8 task identity is not unique."
    }
    if ($matches.Count -eq 0) {
        return $null
    }
    return $matches[0]
}

function Get-OtherProfileLiveOwnership {
    $otherTask = Get-ScheduledTask -TaskName $otherQwenTaskName -ErrorAction SilentlyContinue
    if ($null -eq $otherTask -or [string]$otherTask.State -ne "Running") {
        return $null
    }
    $listeners = @(Get-PortListenerRows)
    if (
        $listeners.Count -ne 1 -or
        [string]$listeners[0].LocalAddress -ne "127.0.0.1"
    ) {
        throw "The other Running Qwen3.8 profile does not own one exact loopback listener."
    }
    if (-not (Test-Path -LiteralPath $otherOwnerPath -PathType Leaf)) {
        throw "The other Running Qwen3.8 profile has no owner record."
    }
    try {
        $otherOwner = Get-Content -LiteralPath $otherOwnerPath -Raw | ConvertFrom-Json
    } catch {
        throw "The other Running Qwen3.8 profile owner record is invalid."
    }
    if (
        [int]$otherOwner.schemaVersion -ne 2 -or
        [string]$otherOwner.taskName -ne $otherQwenTaskName -or
        [string]$otherOwner.endpoint -ne $endpoint -or
        [int]$otherOwner.serverPid -ne [int]$listeners[0].OwningProcess -or
        -not [bool]$otherOwner.jobKillOnClose -or
        -not [bool]$otherOwner.jobAssignmentVerified
    ) {
        throw "The other Running Qwen3.8 profile owner record does not match its listener."
    }
    $server = Get-ProcessIdentity -ProcessId ([int]$otherOwner.serverPid)
    $wrapper = Get-ProcessIdentity -ProcessId ([int]$otherOwner.wrapperPid)
    if (
        [string]$server.processName -ne "llama-server" -or
        [int]$server.parentProcessId -ne [int]$wrapper.processId -or
        [int]$otherOwner.serverParentPid -ne [int]$wrapper.processId -or
        -not (Test-SameUtcTimestamp $server.startTimeUtc $otherOwner.serverStartTimeUtc) -or
        -not (Test-SameUtcTimestamp $wrapper.startTimeUtc $otherOwner.wrapperStartTimeUtc) -or
        -not [string]::Equals(
            [string]$server.executablePath,
            [System.IO.Path]::GetFullPath($serverRuntimePath),
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "The other Running Qwen3.8 profile wrapper-child identity is not exact."
    }
    return [pscustomobject]@{
        task = $otherTask
        owner = $otherOwner
        wrapper = $wrapper
        server = $server
        listener = $listeners[0]
    }
}

function Get-RegisteredTriggerCount {
    $taskXml = [xml](Export-ScheduledTask -TaskName $taskName -ErrorAction Stop)
    return @(
        $taskXml.SelectNodes(
            "/*[local-name()='Task']/*[local-name()='Triggers']/*"
        )
    ).Count
}

function Assert-OwnedTaskDefinition {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Task
    )

    if ([string]$Task.TaskPath -ne "\") {
        throw "The Qwen3.8 task resolves outside the root task folder."
    }
    $expectedSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    try {
        $taskSid = (
            New-Object System.Security.Principal.NTAccount([string]$Task.Principal.UserId)
        ).Translate([System.Security.Principal.SecurityIdentifier]).Value
    } catch {
        throw "The Qwen3.8 task principal cannot be resolved to the current user."
    }
    if ($taskSid -ne $expectedSid) {
        throw "The Qwen3.8 task principal is not the current user."
    }
    if (
        [string]$Task.Principal.RunLevel -ne "Limited" -or
        [string]$Task.Principal.LogonType -ne "Interactive"
    ) {
        throw "The Qwen3.8 task must use current-user Interactive/Limited execution."
    }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        throw "The Qwen3.8 task does not have exactly one action."
    }
    $execute = [Environment]::ExpandEnvironmentVariables([string]$actions[0].Execute)
    if (
        [string]$execute -ne "powershell.exe" -and
        -not [string]::Equals($execute, $windowsPowerShell, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "The Qwen3.8 task action does not use the owned Windows PowerShell executable."
    }
    if ([string]$actions[0].Arguments -ne $taskArguments) {
        throw "The Qwen3.8 task action does not target the owned runtime wrapper."
    }
    if ((Get-RegisteredTriggerCount) -ne 0) {
        throw "The Qwen3.8 task must remain on-demand and have no triggers."
    }
    if ([string]$Task.Settings.MultipleInstances -ne "IgnoreNew") {
        throw "The Qwen3.8 task does not reject concurrent task instances."
    }
    if ([string]$Task.Settings.ExecutionTimeLimit -ne "PT0S") {
        throw "The Qwen3.8 task must have no execution time limit."
    }
    if (-not [bool]$Task.Settings.AllowDemandStart) {
        throw "The Qwen3.8 task does not allow explicit on-demand starts."
    }
}

function Read-OwnerRecord {
    if (-not (Test-Path -LiteralPath $ownerPath -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $ownerPath -Raw | ConvertFrom-Json
    } catch {
        throw "The Qwen3.8 owner record is unreadable; refusing reconciliation."
    }
}

function Assert-OwnerMatchesLiveService {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Owner,
        [Parameter(Mandatory = $true)]
        [object]$Listener,
        [Parameter(Mandatory = $true)]
        [object]$Task
    )

    if ([int]$Owner.schemaVersion -ne 2) {
        throw "The live Qwen3.8 owner record does not use schema version 2."
    }
    $parsedInstance = [Guid]::Empty
    if (-not [Guid]::TryParse([string]$Owner.instanceId, [ref]$parsedInstance)) {
        throw "The live Qwen3.8 owner record has an invalid instance ID."
    }
    $hasTypedProfile = $Owner.PSObject.Properties.Name -contains "profileName"
    $profileMatches = if ($hasTypedProfile) {
        [string]$Owner.profileName -eq $Profile -and
        [string]$Owner.profile -eq [string]$selectedProfile.profileId -and
        [string]$Owner.modelAlias -eq [string]$selectedProfile.modelAlias -and
        [int]$Owner.contextTokens -eq [int]$selectedProfile.contextTokens -and
        [string]$Owner.cacheType -eq [string]$selectedProfile.cacheType -and
        [bool]$Owner.mtp -eq [bool]$selectedProfile.mtp -and
        [int]$Owner.reasoningBudget -eq [int]$selectedProfile.reasoningBudget
    } else {
        $Profile -eq "Bounded" -and
        [string]$Owner.profile -eq "q6-text/medium/q8_0/32768/mtp3" -and
        [string]$Owner.modelAlias -eq "arm-qwen38-q6-text"
    }
    if (
        [string]$Owner.taskName -ne $taskName -or
        [string]$Owner.endpoint -ne $endpoint -or
        [string]$Owner.localAddress -ne "127.0.0.1" -or
        [int]$Owner.localPort -ne $localPort -or
        -not $profileMatches
    ) {
        throw "The live Qwen3.8 owner record has the wrong task, endpoint, or profile."
    }
    if ([int]$Owner.serverPid -ne [int]$Listener.owningProcess) {
        throw "The Qwen3.8 owner record server PID does not own the listener."
    }

    $server = $Listener.process
    Assert-QwenServerIdentity -Identity $server
    if (
        -not (Test-SameUtcTimestamp $Owner.serverStartTimeUtc $server.startTimeUtc) -or
        [int]$Owner.serverParentPid -ne [int]$server.parentProcessId -or
        -not [string]::Equals(
            [string]$Owner.serverExecutablePath,
            [string]$server.executablePath,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$Owner.serverCommandLine -ne [string]$server.commandLine
    ) {
        throw "The owner record does not match the live Qwen3.8 server identity."
    }

    $wrapper = Get-ProcessIdentity -ProcessId ([int]$Owner.wrapperPid)
    if (
        -not (Test-SameUtcTimestamp $Owner.wrapperStartTimeUtc $wrapper.startTimeUtc) -or
        [int]$server.parentProcessId -ne [int]$wrapper.processId -or
        -not [string]::Equals(
            [string]$Owner.wrapperExecutablePath,
            [string]$wrapper.executablePath,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$Owner.wrapperCommandLine -ne [string]$wrapper.commandLine -or
        -not [string]::Equals(
            [string]$wrapper.executablePath,
            $windowsPowerShell,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "The owner record does not match the live wrapper-to-child relationship."
    }
    $runtimePattern = '(?i)(?:^|\s)-File\s+"?' + [regex]::Escape($runtimeScript) + '"?(?:\s|$)'
    if ([string]$wrapper.commandLine -notmatch $runtimePattern) {
        throw "The live wrapper command line does not target the deployed Qwen3.8 runtime."
    }
    if (-not [string]::Equals(
        [System.IO.Path]::GetFullPath([string]$Owner.runtimeScriptPath),
        [System.IO.Path]::GetFullPath($runtimeScript),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "The Qwen3.8 owner record points to a different runtime path."
    }
    $expectedJobPrefix = "Local\CodingIntelligence.$($selectedProfile.jobNamespace).$($wrapper.processId)."
    if (
        -not ([string]$Owner.jobObjectName).StartsWith($expectedJobPrefix, [StringComparison]::Ordinal) -or
        -not [bool]$Owner.jobKillOnClose -or
        -not [bool]$Owner.jobAssignmentVerified
    ) {
        throw "The Qwen3.8 owner record does not attest verified kill-on-close assignment."
    }
    if (-not (Test-Path -LiteralPath $runtimeScript -PathType Leaf)) {
        throw "The Qwen3.8 owner record points to a missing deployed runtime."
    }
    $runtimeHash = (Get-FileHash -LiteralPath $runtimeScript -Algorithm SHA256).Hash.ToLowerInvariant()
    if ([string]$Owner.runtimeScriptSha256 -ne $runtimeHash) {
        throw "The Qwen3.8 owner record runtime hash does not match the deployed script."
    }
    if ([string]$Task.State -ne "Running") {
        throw "The Qwen3.8 owner record is live but the Scheduled Task is not Running."
    }

    return [pscustomobject]@{
        owner = $Owner
        wrapper = $wrapper
        server = $server
        runtimeSha256 = $runtimeHash
    }
}

function Wait-ServiceAbsent {
    param(
        [int[]]$ProcessIds = @(),
        [int]$TimeoutSeconds = 30,
        [switch]$RequireTaskNotRunning
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $listenerRows = @(Get-PortListenerRows)
        $liveProcessIds = @()
        foreach ($candidateId in @($ProcessIds | Select-Object -Unique)) {
            if ($candidateId -le 0) {
                continue
            }
            $native = Get-Process -Id $candidateId -ErrorAction SilentlyContinue
            if ($null -ne $native) {
                $liveProcessIds += $candidateId
                $native.Dispose()
            }
        }
        $task = Get-ExistingTask
        $taskRunning = $null -ne $task -and [string]$task.State -eq "Running"
        if (
            $listenerRows.Count -eq 0 -and
            $liveProcessIds.Count -eq 0 -and
            (-not $RequireTaskNotRunning -or -not $taskRunning)
        ) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Qwen3.8 teardown did not complete within $TimeoutSeconds seconds (listeners=$($listenerRows.Count), livePids=$($liveProcessIds -join ','), taskRunning=$taskRunning)."
}

function Remove-ProvenStaleOwnerRecord {
    $owner = Read-OwnerRecord
    if ($null -eq $owner) {
        return
    }
    if ([int]$owner.schemaVersion -ne 2) {
        throw "Refusing to remove a Qwen3.8 owner record with an unknown schema."
    }
    foreach ($prefix in @("wrapper", "server")) {
        $pidProperty = "${prefix}Pid"
        $startProperty = "${prefix}StartTimeUtc"
        $native = Get-Process -Id ([int]$owner.$pidProperty) -ErrorAction SilentlyContinue
        if ($null -eq $native) {
            continue
        }
        $native.Dispose()
        $identity = Get-ProcessIdentity -ProcessId ([int]$owner.$pidProperty)
        if (Test-SameUtcTimestamp $identity.startTimeUtc $owner.$startProperty) {
            throw "Refusing to remove an owner record whose $prefix process is still live."
        }
    }
    Remove-Item -LiteralPath $ownerPath -Force
}

function Wait-ManagedServiceReady {
    param(
        [int]$TimeoutSeconds = 120
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastError = $null
    do {
        Start-Sleep -Milliseconds 500
        try {
            $task = Get-ExistingTask
            if ($null -eq $task) {
                throw "The Qwen3.8 task is absent."
            }
            Assert-OwnedTaskDefinition -Task $task
            $listener = Get-ExactListener
            if ($null -eq $listener) {
                throw "The Qwen3.8 loopback listener is not ready."
            }
            $owner = Read-OwnerRecord
            if ($null -eq $owner) {
                throw "The Qwen3.8 owner record is not ready."
            }
            $ownership = Assert-OwnerMatchesLiveService -Owner $owner -Listener $listener -Task $task
            $health = Invoke-RestMethod -Uri "$endpoint/health" -TimeoutSec 3
            if ([string]$health.status -ne "ok") {
                throw "The Qwen3.8 health endpoint is not ready."
            }
            return $ownership
        } catch {
            $lastError = $_
        }
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "The managed Qwen3.8 service did not reach readiness within $TimeoutSeconds seconds: $($lastError.Exception.Message)"
}

Assert-ScriptParses -Path $PSCommandPath
Assert-ScriptParses -Path $sourceRuntime
$validationOutput = & $windowsPowerShell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $sourceRuntime -Profile $Profile -ValidateOnly 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "The Qwen3.8 runtime failed static and Job Object validation: $($validationOutput -join ' ')"
}

if ($ValidateOnly) {
    [pscustomobject]@{
        status = "valid"
        installer = $PSCommandPath
        runtime = $sourceRuntime
        runtimeValidation = ($validationOutput -join " ")
        profileName = $Profile
        profile = [string]$selectedProfile.profileId
        task = $taskName
        stateRoot = $stateRoot
        modelAlias = [string]$selectedProfile.modelAlias
        contextTokens = [int]$selectedProfile.contextTokens
        cacheType = [string]$selectedProfile.cacheType
        mtp = [bool]$selectedProfile.mtp
        reasoningBudget = [int]$selectedProfile.reasoningBudget
        onDemand = $true
        triggerCount = 0
        endpoint = $endpoint
    } | ConvertTo-Json -Compress
    exit 0
}

New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
New-Item -ItemType Directory -Path $rollbackRoot -Force | Out-Null

$sourceHash = (Get-FileHash -LiteralPath $sourceRuntime -Algorithm SHA256).Hash.ToLowerInvariant()
$stagePath = Join-Path $stateRoot (".Start-ExcaliburQwen38.{0}.stage.ps1" -f [Guid]::NewGuid().ToString("N"))
Copy-Item -LiteralPath $sourceRuntime -Destination $stagePath
if ((Get-FileHash -LiteralPath $stagePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sourceHash) {
    throw "The staged Qwen3.8 runtime hash does not match its parsed source."
}
Assert-ScriptParses -Path $stagePath
$stageValidation = & $windowsPowerShell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $stagePath -Profile $Profile -ValidateOnly 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "The staged Qwen3.8 runtime failed validation: $($stageValidation -join ' ')"
}

$existingTask = Get-ExistingTask
if ($null -ne $existingTask) {
    Assert-OwnedTaskDefinition -Task $existingTask
}

$otherProfileOwnership = Get-OtherProfileLiveOwnership
$existingListener = if ($null -eq $otherProfileOwnership) {
    Get-ExactListener -AllowProtectedMetadata
} else {
    $null
}
$owner = Read-OwnerRecord
$ownershipKind = if ($null -eq $otherProfileOwnership) { "none" } else { "other-profile" }
$managedOwnership = $null

if ($null -ne $existingListener) {
    if ($null -ne $owner) {
        if ($null -eq $existingTask) {
            throw "A live Qwen3.8 owner record exists without the owned Scheduled Task."
        }
        $managedOwnership = Assert-OwnerMatchesLiveService -Owner $owner -Listener $existingListener -Task $existingTask
        $ownershipKind = "managed"
    } else {
        if ($ExpectedUnmanagedServerPid -eq 0) {
            throw "Port 8818 has no owner record; provide the exact audited unmanaged PID and start time."
        }
        if ([int]$existingListener.owningProcess -ne $ExpectedUnmanagedServerPid) {
            throw "The live unmanaged listener PID does not match the audited tuple."
        }
        $observedStart = [DateTimeOffset]::Parse([string]$existingListener.process.startTimeUtc).ToUniversalTime()
        if ($observedStart.UtcTicks -ne $expectedUnmanagedStart.UtcTicks) {
            throw "The live unmanaged listener start time does not match the audited tuple."
        }
        $ownershipKind = "unmanaged"
    }
} elseif ($ExpectedUnmanagedServerPid -ne 0) {
    throw "The audited unmanaged listener disappeared before reconciliation; refusing a stale PID tuple."
} elseif ($null -ne $owner) {
    Remove-ProvenStaleOwnerRecord
    $owner = $null
}

if (
    $null -ne $existingTask -and
    [string]$existingTask.State -eq "Running" -and
    $ownershipKind -ne "managed"
) {
    throw "A Running Qwen3.8 task may be stopped only when its schema-v2 owner record validates."
}

$timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffffffZ")
$taskRollbackPath = $null
$taskXml = $null
$hadExistingTask = $null -ne $existingTask
$priorTaskRunning = $hadExistingTask -and [string]$existingTask.State -eq "Running"
if ($hadExistingTask) {
    $taskRollbackPath = Join-Path $rollbackRoot "$timestamp.task.xml"
    $taskXml = Export-ScheduledTask -TaskName $taskName
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($taskRollbackPath, $taskXml, $utf8NoBom)
}

$hadRuntime = Test-Path -LiteralPath $runtimeScript -PathType Leaf
$runtimeRollbackPath = $null
$runtimeDisplacedPath = $null
if ($hadRuntime) {
    $runtimeRollbackPath = Join-Path $rollbackRoot "$timestamp.runtime.ps1"
    $runtimeDisplacedPath = Join-Path $rollbackRoot "$timestamp.displaced-runtime.ps1"
    Copy-Item -LiteralPath $runtimeScript -Destination $runtimeRollbackPath
    if (
        (Get-FileHash -LiteralPath $runtimeRollbackPath -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $runtimeScript -Algorithm SHA256).Hash
    ) {
        throw "The Qwen3.8 runtime rollback copy failed hash verification."
    }
    Assert-ScriptParses -Path $runtimeRollbackPath
}

$priorProcessIds = @()
if ($null -ne $existingListener) {
    $priorProcessIds += [int]$existingListener.owningProcess
}
if ($ownershipKind -eq "managed") {
    $priorProcessIds += [int]$managedOwnership.owner.wrapperPid
}

$serviceChangeStarted = $false
$runtimeMutated = $false
$taskMutated = $false
$unmanagedStopped = $false

try {
    if ($priorTaskRunning) {
        $serviceChangeStarted = $true
        Stop-ScheduledTask -TaskName $taskName
        Wait-ServiceAbsent -ProcessIds $priorProcessIds -TimeoutSeconds 30 -RequireTaskNotRunning
        Remove-ProvenStaleOwnerRecord
    }

    if (Test-Path -LiteralPath $runtimeScript -PathType Leaf) {
        [System.IO.File]::Replace($stagePath, $runtimeScript, $runtimeDisplacedPath, $true)
    } else {
        Move-Item -LiteralPath $stagePath -Destination $runtimeScript
    }
    $runtimeMutated = $true
    $stagePath = $null
    if ((Get-FileHash -LiteralPath $runtimeScript -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sourceHash) {
        throw "The deployed Qwen3.8 runtime hash does not match the validated source."
    }

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $windowsPowerShell -Argument $taskArguments
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Principal $principal `
        -Settings $settings `
        -Description ("On-demand, loopback-only Qwen3.8 Q6 {0} backend for EXCALIBUR." -f $Profile) `
        -Force | Out-Null
    $taskMutated = $true

    $installedTask = Get-ExistingTask
    if ($null -eq $installedTask) {
        throw "The Qwen3.8 Scheduled Task was not registered."
    }
    Assert-OwnedTaskDefinition -Task $installedTask
    if ([string]$installedTask.State -eq "Running") {
        throw "The on-demand Qwen3.8 task started unexpectedly during installation."
    }

    if ($ownershipKind -eq "unmanaged") {
        # This is deliberately the final mutation. All runtime/task validation
        # has passed, and only the exact audited PID/start/listener tuple can be
        # stopped. No broad process-name reconciliation is permitted.
        $liveIdentity = Get-ProcessIdentity -ProcessId $ExpectedUnmanagedServerPid
        $liveStart = [DateTimeOffset]::Parse([string]$liveIdentity.startTimeUtc).ToUniversalTime()
        if ($liveStart.UtcTicks -ne $expectedUnmanagedStart.UtcTicks) {
            throw "The unmanaged process identity changed immediately before reconciliation."
        }
        $liveListener = Get-ExactListener -AllowProtectedMetadata
        if ($null -eq $liveListener -or [int]$liveListener.owningProcess -ne $ExpectedUnmanagedServerPid) {
            throw "The unmanaged process no longer owns the exact loopback listener."
        }
        $serviceChangeStarted = $true
        Stop-Process -Id $ExpectedUnmanagedServerPid -Force -ErrorAction Stop
        $unmanagedStopped = $true
        Wait-ServiceAbsent -ProcessIds @($ExpectedUnmanagedServerPid) -TimeoutSeconds 30
    }

    if ($priorTaskRunning) {
        Start-ScheduledTask -TaskName $taskName
        Wait-ManagedServiceReady -TimeoutSeconds 120 | Out-Null
    }
} catch {
    $deploymentError = $_
    if (-not $serviceChangeStarted -and -not $runtimeMutated -and -not $taskMutated) {
        if ($null -ne $stagePath -and (Test-Path -LiteralPath $stagePath -PathType Leaf)) {
            Remove-Item -LiteralPath $stagePath -Force
        }
        throw
    }

    try {
        $currentTask = Get-ExistingTask
        if ($null -ne $currentTask -and [string]$currentTask.State -eq "Running") {
            Stop-ScheduledTask -TaskName $taskName
            Wait-ServiceAbsent -TimeoutSeconds 30 -RequireTaskNotRunning
        }
        Remove-ProvenStaleOwnerRecord

        if ($hadRuntime) {
            if (-not (Test-Path -LiteralPath $runtimeRollbackPath -PathType Leaf)) {
                throw "The saved Qwen3.8 runtime rollback artifact is missing."
            }
            $restoreStage = Join-Path $stateRoot (".Start-ExcaliburQwen38.{0}.restore.ps1" -f [Guid]::NewGuid().ToString("N"))
            $failedRuntimePath = Join-Path $rollbackRoot ("{0}.failed-runtime.ps1" -f [Guid]::NewGuid().ToString("N"))
            Copy-Item -LiteralPath $runtimeRollbackPath -Destination $restoreStage
            if (Test-Path -LiteralPath $runtimeScript -PathType Leaf) {
                [System.IO.File]::Replace($restoreStage, $runtimeScript, $failedRuntimePath, $true)
            } else {
                Move-Item -LiteralPath $restoreStage -Destination $runtimeScript
            }
            if (
                (Get-FileHash -LiteralPath $runtimeScript -Algorithm SHA256).Hash -ne
                (Get-FileHash -LiteralPath $runtimeRollbackPath -Algorithm SHA256).Hash
            ) {
                throw "The restored Qwen3.8 runtime does not match its rollback artifact."
            }
        } elseif (Test-Path -LiteralPath $runtimeScript -PathType Leaf) {
            Remove-Item -LiteralPath $runtimeScript -Force
        }

        $currentTask = Get-ExistingTask
        if ($hadExistingTask) {
            Register-ScheduledTask -TaskName $taskName -Xml $taskXml -Force | Out-Null
        } elseif ($null -ne $currentTask) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }

        if ($priorTaskRunning) {
            Start-ScheduledTask -TaskName $taskName
            Wait-ManagedServiceReady -TimeoutSeconds 120 | Out-Null
        }
    } catch {
        $rollbackError = $_
        throw "Qwen3.8 deployment failed: $($deploymentError.Exception.Message). Rollback also failed: $($rollbackError.Exception.Message)"
    }

    if ($unmanagedStopped) {
        throw "Qwen3.8 deployment failed after the exact unmanaged server was stopped; task/runtime rollback succeeded, but an unmanaged process was intentionally not recreated: $($deploymentError.Exception.Message)"
    }
    throw "Qwen3.8 deployment failed but the previous task/runtime state was restored: $($deploymentError.Exception.Message)"
} finally {
    if ($null -ne $stagePath -and (Test-Path -LiteralPath $stagePath -PathType Leaf)) {
        Remove-Item -LiteralPath $stagePath -Force -ErrorAction SilentlyContinue
    }
}

$finalTask = Get-ExistingTask
Assert-OwnedTaskDefinition -Task $finalTask
[pscustomobject]@{
    task = $taskName
    taskState = [string]$finalTask.State
    onDemand = $true
    triggerCount = Get-RegisteredTriggerCount
    runtime = $runtimeScript
    runtimeSha256 = $sourceHash
    endpoint = $endpoint
    profileName = $Profile
    profile = [string]$selectedProfile.profileId
    modelAlias = [string]$selectedProfile.modelAlias
    contextTokens = [int]$selectedProfile.contextTokens
    cacheType = [string]$selectedProfile.cacheType
    mtp = [bool]$selectedProfile.mtp
    reasoningBudget = [int]$selectedProfile.reasoningBudget
    priorOwnership = $ownershipKind
    unmanagedReconciled = $unmanagedStopped
    priorManagedStateRestored = $priorTaskRunning
    taskRollback = $taskRollbackPath
    runtimeRollback = $runtimeRollbackPath
    displacedRuntime = $runtimeDisplacedPath
} | ConvertTo-Json -Compress
