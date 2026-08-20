param(
    [string]$Model = "gpt-oss:20b",
    [switch]$SkipPull,
    [ValidateRange(0, 2147483647)]
    [int]$ExpectedLegacyServerPid = 0,
    [string]$ExpectedLegacyServerStartTimeUtc = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ollamaPath = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
$stateRoot = Join-Path $env:LOCALAPPDATA "AnimeFrontier\AgentContinuity"
$runtimeScript = Join-Path $stateRoot "Start-ExcaliburOllama.ps1"
$ownerPath = Join-Path $stateRoot "service-owner.json"
$rollbackRoot = Join-Path $stateRoot "rollback"
$taskName = "AnimeFrontier Excalibur Ollama"
$endpoint = "http://127.0.0.1:11434"
$sourceRuntime = Join-Path $PSScriptRoot "Start-ExcaliburOllama.ps1"
$windowsPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$taskArguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $runtimeScript

if (($ExpectedLegacyServerPid -eq 0) -ne ([string]::IsNullOrWhiteSpace($ExpectedLegacyServerStartTimeUtc))) {
    throw "Legacy recovery requires both expected PID and exact start time, or neither."
}
if (-not (Test-Path -LiteralPath $ollamaPath -PathType Leaf)) {
    throw "Install Ollama before configuring the EXCALIBUR continuity service."
}
if (-not (Test-Path -LiteralPath $sourceRuntime -PathType Leaf)) {
    throw "The companion runtime script is missing."
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
        return [pscustomobject]@{
            processId = $ProcessId
            parentProcessId = [int]$cim.ParentProcessId
            executablePath = [System.IO.Path]::GetFullPath([string]$cim.ExecutablePath)
            commandLine = [string]$cim.CommandLine
            startTimeUtc = $native.StartTime.ToUniversalTime().ToString("o")
        }
    } finally {
        $native.Dispose()
    }
}

function Assert-OllamaServeIdentity {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Identity
    )

    $expectedPath = [System.IO.Path]::GetFullPath($ollamaPath)
    if (-not [string]::Equals(
        [string]$Identity.executablePath,
        $expectedPath,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Port 11434 is owned by an unexpected executable."
    }
    $commandPattern = '^\s*"?' + [regex]::Escape($expectedPath) + '"?\s+serve\s*$'
    if ([string]$Identity.commandLine -notmatch $commandPattern) {
        throw "The resolved Ollama process does not have the exact serve command line."
    }
}

function Get-PortListenerRows {
    try {
        return @(
            Get-NetTCPConnection -State Listen -ErrorAction Stop |
                Where-Object { [int]$_.LocalPort -eq 11434 }
        )
    } catch {
        throw "Unable to inspect port 11434; listener discovery failed."
    }
}

function Get-ExactListener {
    $listeners = @(Get-PortListenerRows)
    if ($listeners.Count -eq 0) {
        return $null
    }
    if ($listeners.Count -ne 1) {
        throw "Expected zero or one listener on port 11434; found $($listeners.Count)."
    }
    $listener = $listeners[0]
    if ([string]$listener.LocalAddress -ne "127.0.0.1") {
        throw "Port 11434 is exposed outside the exact IPv4 loopback address."
    }
    $identity = Get-ProcessIdentity -ProcessId ([int]$listener.OwningProcess)
    Assert-OllamaServeIdentity -Identity $identity
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
        throw "The owned task identity is not unique."
    }
    if ($matches.Count -eq 0) {
        return $null
    }
    return $matches[0]
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

    throw "Service teardown did not complete within $TimeoutSeconds seconds (listeners=$($listenerRows.Count), livePids=$($liveProcessIds -join ','), taskRunning=$taskRunning)."
}

function Assert-OwnedTaskDefinition {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Task
    )

    if ([string]$Task.TaskPath -ne "\") {
        throw "The existing task name resolves outside the expected root task folder."
    }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        throw "The existing task does not have exactly one action."
    }
    $execute = [Environment]::ExpandEnvironmentVariables([string]$actions[0].Execute)
    if (
        [string]$execute -ne "powershell.exe" -and
        -not [string]::Equals($execute, $windowsPowerShell, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "The existing task action does not use the owned Windows PowerShell executable."
    }
    if ([string]$actions[0].Arguments -ne $taskArguments) {
        throw "The existing task action does not target the owned runtime script."
    }
}

function Read-OwnerRecord {
    if (-not (Test-Path -LiteralPath $ownerPath -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $ownerPath -Raw | ConvertFrom-Json
    } catch {
        throw "The service owner record is unreadable; refusing reconciliation."
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
        throw "The live service owner record does not use schema version 2."
    }
    $parsedInstanceId = [Guid]::Empty
    if (-not [Guid]::TryParse([string]$Owner.instanceId, [ref]$parsedInstanceId)) {
        throw "The live service owner record has an invalid instance ID."
    }
    if (
        [string]$Owner.taskName -ne $taskName -or
        [string]$Owner.endpoint -ne $endpoint -or
        [string]$Owner.localAddress -ne "127.0.0.1" -or
        [int]$Owner.localPort -ne 11434
    ) {
        throw "The live service owner record has the wrong task or endpoint contract."
    }
    if ([int]$Owner.serverPid -ne [int]$Listener.owningProcess) {
        throw "The owner record server PID does not own the listener."
    }

    $server = $Listener.process
    if (
        [string]$Owner.serverStartTimeUtc -ne [string]$server.startTimeUtc -or
        -not [string]::Equals(
            [string]$Owner.serverExecutablePath,
            [string]$server.executablePath,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$Owner.serverCommandLine -ne [string]$server.commandLine -or
        [int]$Owner.serverParentPid -ne [int]$server.parentProcessId
    ) {
        throw "The owner record does not match the live Ollama process identity."
    }

    $wrapper = Get-ProcessIdentity -ProcessId ([int]$Owner.wrapperPid)
    if (
        [string]$Owner.wrapperStartTimeUtc -ne [string]$wrapper.startTimeUtc -or
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
        throw "The live wrapper command line does not target the owned runtime."
    }
    if (-not [string]::Equals(
        [System.IO.Path]::GetFullPath([string]$Owner.runtimeScriptPath),
        [System.IO.Path]::GetFullPath($runtimeScript),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "The owner record points to a different runtime path."
    }
    $expectedJobPrefix = "Local\AnimeFrontier.ExcaliburOllama.$($wrapper.processId)."
    if (
        -not ([string]$Owner.jobObjectName).StartsWith(
            $expectedJobPrefix,
            [StringComparison]::Ordinal
        ) -or
        -not [bool]$Owner.jobKillOnClose -or
        -not [bool]$Owner.jobAssignmentVerified
    ) {
        throw "The owner record does not attest verified kill-on-close assignment."
    }
    if (-not (Test-Path -LiteralPath $runtimeScript -PathType Leaf)) {
        throw "The owner record points to a missing deployed runtime."
    }
    $runtimeHash = (Get-FileHash -LiteralPath $runtimeScript -Algorithm SHA256).Hash.ToLowerInvariant()
    if ([string]$Owner.runtimeScriptSha256 -ne $runtimeHash) {
        throw "The owner record runtime hash does not match the deployed script."
    }
    if ([string]$Task.State -ne "Running") {
        throw "The owner record is live but the Scheduled Task is not Running."
    }
    return [pscustomobject]@{
        owner = $Owner
        wrapper = $wrapper
        server = $server
        runtimeSha256 = $runtimeHash
    }
}

function Get-ModelInventory {
    $tags = Invoke-RestMethod -Uri "$endpoint/api/tags" -TimeoutSec 15
    return @($tags.models | Sort-Object name | ForEach-Object {
        [pscustomobject]@{
            name = [string]$_.name
            digest = [string]$_.digest
            size = [long]$_.size
        }
    })
}

function Assert-InventoryPreserved {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Before,
        [Parameter(Mandatory = $true)]
        [object[]]$After
    )

    foreach ($expected in $Before) {
        $actual = @($After | Where-Object { [string]$_.name -eq [string]$expected.name })
        if ($actual.Count -ne 1) {
            throw "Model inventory changed: $($expected.name) is no longer unique and present."
        }
        if (
            [string]$actual[0].digest -ne [string]$expected.digest -or
            [long]$actual[0].size -ne [long]$expected.size
        ) {
            throw "Model artifact identity changed during service installation: $($expected.name)."
        }
    }
}

function Remove-ProvenStaleOwnerRecord {
    $owner = Read-OwnerRecord
    if ($null -eq $owner) {
        return
    }
    if ([int]$owner.schemaVersion -ne 2) {
        throw "Refusing to remove an owner record with an unknown schema."
    }
    foreach ($prefix in @("wrapper", "server")) {
        $processIdProperty = "${prefix}Pid"
        $startTimeProperty = "${prefix}StartTimeUtc"
        $native = Get-Process -Id ([int]$owner.$processIdProperty) -ErrorAction SilentlyContinue
        if ($null -eq $native) {
            continue
        }
        $native.Dispose()
        $identity = Get-ProcessIdentity -ProcessId ([int]$owner.$processIdProperty)
        if ([string]$identity.startTimeUtc -eq [string]$owner.$startTimeProperty) {
            throw "Refusing to remove an owner record whose $prefix process is still live."
        }
    }
    Remove-Item -LiteralPath $ownerPath -Force
}

Assert-ScriptParses -Path $PSCommandPath
Assert-ScriptParses -Path $sourceRuntime

New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
New-Item -ItemType Directory -Path $rollbackRoot -Force | Out-Null

$sourceHash = (Get-FileHash -LiteralPath $sourceRuntime -Algorithm SHA256).Hash.ToLowerInvariant()
$stagePath = Join-Path $stateRoot (".Start-ExcaliburOllama.{0}.stage.ps1" -f [Guid]::NewGuid().ToString("N"))
Copy-Item -LiteralPath $sourceRuntime -Destination $stagePath
if ((Get-FileHash -LiteralPath $stagePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sourceHash) {
    throw "The staged runtime hash does not match its parsed source."
}
Assert-ScriptParses -Path $stagePath
$validationOutput = & $windowsPowerShell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $stagePath -ValidateOnly 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "The staged runtime failed Job Object interop validation: $($validationOutput -join ' ')"
}

$existingTask = Get-ExistingTask
if ($null -ne $existingTask) {
    Assert-OwnedTaskDefinition -Task $existingTask
}

$existingListener = Get-ExactListener
$owner = Read-OwnerRecord
$ownershipKind = "none"
$beforeInventory = @()

if ($null -ne $existingListener) {
    $beforeInventory = @(Get-ModelInventory)
    if ($null -ne $owner) {
        if ($null -eq $existingTask) {
            throw "A service owner record exists without the owned Scheduled Task."
        }
        Assert-OwnerMatchesLiveService -Owner $owner -Listener $existingListener -Task $existingTask | Out-Null
        $ownershipKind = "recorded"
    } else {
        if ($ExpectedLegacyServerPid -eq 0) {
            throw "The loopback listener has no owner record; provide the exact audited legacy PID and start time."
        }
        if (
            [int]$existingListener.owningProcess -ne $ExpectedLegacyServerPid -or
            [string]$existingListener.process.startTimeUtc -ne $ExpectedLegacyServerStartTimeUtc
        ) {
            throw "The live ownerless listener does not match the audited legacy process tuple."
        }
        if ($null -eq $existingTask) {
            throw "Legacy recovery requires the owned Scheduled Task definition."
        }
        $parentNative = Get-Process -Id ([int]$existingListener.process.parentProcessId) -ErrorAction SilentlyContinue
        if ($null -ne $parentNative) {
            $parentNative.Dispose()
            $parent = Get-ProcessIdentity -ProcessId ([int]$existingListener.process.parentProcessId)
            if ([DateTime]::Parse($parent.startTimeUtc).ToUniversalTime() -le [DateTime]::Parse($existingListener.process.startTimeUtc).ToUniversalTime()) {
                throw "The audited legacy Ollama process still has a live original parent."
            }
        }
        $ownershipKind = "legacy"
    }
} elseif ($ExpectedLegacyServerPid -ne 0) {
    throw "The audited legacy listener disappeared before reconciliation; refusing a stale PID tuple."
}

$timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffffffZ")
$taskRollbackPath = $null
$taskXml = $null
$hadExistingTask = $null -ne $existingTask
$priorTaskState = if ($hadExistingTask) { [string]$existingTask.State } else { "Absent" }
if ($null -ne $existingTask) {
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
        throw "The runtime rollback copy failed hash verification."
    }
    Assert-ScriptParses -Path $runtimeRollbackPath
}

$priorProcessIds = @()
if ($null -ne $existingListener) {
    $priorProcessIds += [int]$existingListener.owningProcess
}
if ($ownershipKind -eq "recorded") {
    $priorProcessIds += [int]$owner.wrapperPid
}
$shouldRestoreEndpoint = $null -ne $existingListener
$shouldRestartPreviousTask = $shouldRestoreEndpoint -or $priorTaskState -eq "Running"
$serviceChangeStarted = $false
$runtimeMutated = $false
$taskMutated = $false
$readyReport = $null

try {
    if ($null -ne $existingTask -and [string]$existingTask.State -eq "Running") {
        if ($ownershipKind -ne "recorded") {
            throw "A Running task may be stopped only when its schema-v2 owner record validates."
        }
        $serviceChangeStarted = $true
        Stop-ScheduledTask -TaskName $taskName
        Wait-ServiceAbsent -ProcessIds $priorProcessIds -TimeoutSeconds 30 -RequireTaskNotRunning
    } elseif ($ownershipKind -eq "recorded") {
        throw "A validated live owner record exists while the Scheduled Task is not Running."
    } elseif ($ownershipKind -eq "legacy") {
        $serviceChangeStarted = $true
        Stop-Process -Id $ExpectedLegacyServerPid -Force -ErrorAction Stop
        Wait-ServiceAbsent -ProcessIds @($ExpectedLegacyServerPid) -TimeoutSeconds 30
    }

    if (@(Get-PortListenerRows).Count -ne 0) {
        throw "Port 11434 was not free after exact ownership reconciliation."
    }
    Remove-ProvenStaleOwnerRecord

    if (Test-Path -LiteralPath $runtimeScript -PathType Leaf) {
        [System.IO.File]::Replace($stagePath, $runtimeScript, $runtimeDisplacedPath, $true)
    } else {
        Move-Item -LiteralPath $stagePath -Destination $runtimeScript
    }
    $runtimeMutated = $true
    $stagePath = $null
    if (
        (Get-FileHash -LiteralPath $runtimeScript -Algorithm SHA256).Hash.ToLowerInvariant() -ne
        $sourceHash
    ) {
        throw "The deployed runtime hash does not match the validated source."
    }
    if ($hadRuntime) {
        if (-not (Test-Path -LiteralPath $runtimeDisplacedPath -PathType Leaf)) {
            throw "Atomic runtime replacement did not preserve the displaced file."
        }
        if (
            (Get-FileHash -LiteralPath $runtimeDisplacedPath -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $runtimeRollbackPath -Algorithm SHA256).Hash
        ) {
            throw "The atomically displaced runtime does not match the rollback copy."
        }
    }

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $actionParameters = @{
        Execute = $windowsPowerShell
        Argument = $taskArguments
    }
    $action = New-ScheduledTaskAction @actionParameters
    $triggerParameters = @{
        AtLogOn = $true
        User = $currentUser
    }
    $trigger = New-ScheduledTaskTrigger @triggerParameters
    $principalParameters = @{
        UserId = $currentUser
        LogonType = "Interactive"
        RunLevel = "Limited"
    }
    $principal = New-ScheduledTaskPrincipal @principalParameters
    $settingsParameters = @{
        AllowStartIfOnBatteries = $true
        DontStopIfGoingOnBatteries = $true
        ExecutionTimeLimit = [TimeSpan]::Zero
        MultipleInstances = "IgnoreNew"
        RestartCount = 3
        RestartInterval = (New-TimeSpan -Minutes 1)
        StartWhenAvailable = $true
    }
    $settings = New-ScheduledTaskSettingsSet @settingsParameters
    $register = @{
        TaskName = $taskName
        Action = $action
        Trigger = $trigger
        Principal = $principal
        Settings = $settings
        Description = "Loopback-only Ollama for the EXCALIBUR coding-intelligence continuity lane."
        Force = $true
    }
    Register-ScheduledTask @register | Out-Null
    $taskMutated = $true
    Start-ScheduledTask -TaskName $taskName

    $readyDeadline = [DateTime]::UtcNow.AddSeconds(60)
    $lastReadyError = $null
    do {
        Start-Sleep -Milliseconds 500
        try {
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
            Assert-OwnedTaskDefinition -Task $task
            $listener = Get-ExactListener
            if ($null -eq $listener) {
                throw "The loopback listener is not ready."
            }
            $liveOwner = Read-OwnerRecord
            if ($null -eq $liveOwner) {
                throw "The schema-v2 owner record is not ready."
            }
            $ownership = Assert-OwnerMatchesLiveService -Owner $liveOwner -Listener $listener -Task $task
            $version = Invoke-RestMethod -Uri "$endpoint/api/version" -TimeoutSec 3
            $readyReport = [pscustomobject]@{
                task = $task
                listener = $listener
                ownership = $ownership
                version = [string]$version.version
            }
        } catch {
            $lastReadyError = $_
            $readyReport = $null
        }
    } while ($null -eq $readyReport -and [DateTime]::UtcNow -lt $readyDeadline)

    if ($null -eq $readyReport) {
        throw "The installed service did not reach exact owned readiness within 60 seconds: $($lastReadyError.Exception.Message)"
    }

    $afterInventory = @(Get-ModelInventory)
    $modelNames = @($afterInventory | ForEach-Object { [string]$_.name })
    if ($Model -notin $modelNames -and -not $SkipPull) {
        $env:OLLAMA_HOST = "127.0.0.1:11434"
        & $ollamaPath pull $Model
        if ($LASTEXITCODE -ne 0) {
            throw "Ollama failed to pull the missing model $Model."
        }
        $afterInventory = @(Get-ModelInventory)
        $modelNames = @($afterInventory | ForEach-Object { [string]$_.name })
    }
    if ($Model -notin $modelNames) {
        throw "Expected model $Model is not installed."
    }
    if ($beforeInventory.Count -ne 0) {
        Assert-InventoryPreserved -Before $beforeInventory -After $afterInventory
    }
} catch {
    $deploymentError = $_
    if (-not $serviceChangeStarted -and -not $runtimeMutated -and -not $taskMutated) {
        throw
    }

    try {
        $rollbackProcessIds = @($priorProcessIds)
        try {
            $failedOwner = Read-OwnerRecord
            if ($null -ne $failedOwner) {
                $rollbackProcessIds += [int]$failedOwner.wrapperPid
                $rollbackProcessIds += [int]$failedOwner.serverPid
            }
        } catch {
            # Teardown still proves task and listener absence; unreadable owner
            # metadata is handled by the fail-closed stale-record cleanup below.
        }

        $failedTask = Get-ExistingTask
        if ($null -ne $failedTask) {
            if ([string]$failedTask.State -eq "Running") {
                Stop-ScheduledTask -TaskName $taskName
            }
            Disable-ScheduledTask -TaskName $taskName | Out-Null
        }
        Wait-ServiceAbsent -ProcessIds $rollbackProcessIds -TimeoutSeconds 30 -RequireTaskNotRunning
        Remove-ProvenStaleOwnerRecord

        if ($hadRuntime) {
            if (-not (Test-Path -LiteralPath $runtimeRollbackPath -PathType Leaf)) {
                throw "The saved runtime rollback artifact is missing."
            }
            Assert-ScriptParses -Path $runtimeRollbackPath
            $restoreStage = Join-Path $stateRoot (".Start-ExcaliburOllama.{0}.restore.ps1" -f [Guid]::NewGuid().ToString("N"))
            $failedRuntimePath = Join-Path $rollbackRoot ("{0}.failed-runtime.ps1" -f [Guid]::NewGuid().ToString("N"))
            Copy-Item -LiteralPath $runtimeRollbackPath -Destination $restoreStage
            $runtimeWasPresentDuringRollback = Test-Path -LiteralPath $runtimeScript -PathType Leaf
            if ($runtimeWasPresentDuringRollback) {
                [System.IO.File]::Replace($restoreStage, $runtimeScript, $failedRuntimePath, $true)
            } else {
                Move-Item -LiteralPath $restoreStage -Destination $runtimeScript
            }
            if (
                (Get-FileHash -LiteralPath $runtimeScript -Algorithm SHA256).Hash -ne
                (Get-FileHash -LiteralPath $runtimeRollbackPath -Algorithm SHA256).Hash
            ) {
                throw "The restored runtime does not match the saved rollback artifact."
            }
            if (
                $runtimeWasPresentDuringRollback -and
                -not (Test-Path -LiteralPath $failedRuntimePath -PathType Leaf)
            ) {
                throw "Rollback did not preserve the displaced failed runtime."
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

        if ($shouldRestartPreviousTask) {
            Start-ScheduledTask -TaskName $taskName
            $restoreDeadline = [DateTime]::UtcNow.AddSeconds(60)
            $restored = $false
            $restoreError = $null
            do {
                Start-Sleep -Milliseconds 500
                try {
                    $restoredTask = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
                    $restoredListener = Get-ExactListener
                    if (
                        [string]$restoredTask.State -ne "Running" -or
                        $null -eq $restoredListener
                    ) {
                        throw "The prior task and endpoint are not both running."
                    }
                    if ($ownershipKind -eq "recorded") {
                        $restoredOwner = Read-OwnerRecord
                        if ($null -eq $restoredOwner) {
                            throw "The restored schema-v2 service has no owner record."
                        }
                        Assert-OwnerMatchesLiveService -Owner $restoredOwner -Listener $restoredListener -Task $restoredTask | Out-Null
                    }
                    Invoke-RestMethod -Uri "$endpoint/api/version" -TimeoutSec 3 | Out-Null
                    $restored = $true
                } catch {
                    $restoreError = $_
                    $restored = $false
                }
            } while (-not $restored -and [DateTime]::UtcNow -lt $restoreDeadline)
            if (-not $restored) {
                throw "The previous service could not be restored within 60 seconds: $($restoreError.Exception.Message)"
            }
        }
    } catch {
        $rollbackError = $_
        throw "Deployment failed: $($deploymentError.Exception.Message). Rollback also failed: $($rollbackError.Exception.Message)"
    }
    throw "Deployment failed but the previous service was restored: $($deploymentError.Exception.Message)"
}

[pscustomobject]@{
    task = $taskName
    taskState = [string]$readyReport.task.State
    runtime = $runtimeScript
    runtimeSha256 = [string]$readyReport.ownership.runtimeSha256
    endpoint = $endpoint
    version = [string]$readyReport.version
    model = $Model
    loopbackOnly = $true
    listenerCount = 1
    wrapperPid = [int]$readyReport.ownership.owner.wrapperPid
    serverPid = [int]$readyReport.ownership.owner.serverPid
    instanceId = [string]$readyReport.ownership.owner.instanceId
    jobKillOnClose = [bool]$readyReport.ownership.owner.jobKillOnClose
    modelInventoryPreserved = ($beforeInventory.Count -ne 0)
    taskRollback = $taskRollbackPath
    runtimeRollback = $runtimeRollbackPath
    displacedRuntime = $runtimeDisplacedPath
} | ConvertTo-Json -Compress
