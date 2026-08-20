[CmdletBinding()]
param(
    [string]$TaskName = 'Coding Intelligence - Memory Maintenance',
    [Parameter(Mandatory)]
    [string]$MemoryRoot,
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'CodingIntelligence\MemoryMaintenance'),
    [ValidateRange(15, 1440)]
    [int]$IntervalMinutes = 60,
    [ValidateRange(1, 100)]
    [int]$MaxItems = 25,
    [ValidateRange(1, 1099511627776)]
    [long]$MaxBytes = 1073741824,
    [ValidateRange(5, 300)]
    [int]$MaxWallSeconds = 30,
    [ValidateRange(1, 109951162777600)]
    [long]$HotBlobBudgetBytes = 17179869184,
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Resolve-PythonPath {
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $launcher) {
        throw 'The current-user Python launcher py.exe is unavailable.'
    }
    $resolved = (& $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null |
        Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw 'Could not resolve a stable current-user Python 3 executable.'
    }
    return (Resolve-Path -LiteralPath $resolved).Path
}

function Test-PowerShellSource {
    param([Parameter(Mandatory)][string]$Path)
    $tokens = $null
    $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile(
        $Path,
        [ref]$tokens,
        [ref]$errors
    )
    if ($errors.Count -ne 0) {
        throw "PowerShell parser rejected $Path`: $($errors[0].Message)"
    }
}

function Get-Sha256 {
    param([Parameter(Mandatory)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha256.ComputeHash($stream)
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
    return 'sha256:' + (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Assert-NoReparseChain {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Boundary,
        [Parameter(Mandatory)][string]$Label
    )
    $full = [IO.Path]::GetFullPath($Path)
    $basePath = [IO.Path]::GetFullPath($Boundary)
    $base = $basePath.TrimEnd('\')
    if ($full.TrimEnd('\') -cne $base -and
        -not $full.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label escaped its allowed boundary."
    }
    $relative = $full.Substring($base.Length).TrimStart('\')
    $cursor = $basePath
    $parts = if ([string]::IsNullOrEmpty($relative)) { @() } else { $relative.Split('\') }
    $components = @('') + @($parts)
    foreach ($part in $components) {
        if (-not [string]::IsNullOrEmpty($part)) {
            $cursor = Join-Path $cursor $part
        }
        if (-not (Test-Path -LiteralPath $cursor)) {
            break
        }
        $item = Get-Item -LiteralPath $cursor -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a symlink, junction, or reparse point: $cursor"
        }
    }
    return $full
}

$resolvedMemoryRoot = [IO.Path]::GetFullPath($MemoryRoot)
$resolvedRuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$allowedRuntimeBase = [IO.Path]::GetFullPath((
    Join-Path $env:LOCALAPPDATA 'CodingIntelligence'
)).TrimEnd('\')
$allowedTempBase = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
if (-not (
        $resolvedRuntimeRoot.StartsWith(
            $allowedRuntimeBase + '\',
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        $resolvedRuntimeRoot.StartsWith(
            $allowedTempBase + '\',
            [StringComparison]::OrdinalIgnoreCase
        )
    )) {
    throw 'RuntimeRoot must remain below current-user LOCALAPPDATA or TEMP.'
}
$runtimeBoundary = if ($resolvedRuntimeRoot.StartsWith(
        $allowedTempBase + '\',
        [StringComparison]::OrdinalIgnoreCase
    )) {
    $allowedTempBase
} else {
    [IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\')
}
[void](Assert-NoReparseChain `
    -Path $resolvedRuntimeRoot `
    -Boundary $runtimeBoundary `
    -Label 'RuntimeRoot')
[void](Assert-NoReparseChain `
    -Path $resolvedMemoryRoot `
    -Boundary ([IO.Path]::GetPathRoot($resolvedMemoryRoot)) `
    -Label 'MemoryRoot')
$runtimeDriveRoot = [IO.Path]::GetPathRoot($resolvedRuntimeRoot).TrimEnd('\')
if ($resolvedRuntimeRoot.TrimEnd('\') -ceq $runtimeDriveRoot -or
    $resolvedMemoryRoot.TrimEnd('\') -ceq [IO.Path]::GetPathRoot(
        $resolvedMemoryRoot
    ).TrimEnd('\')) {
    throw 'Memory Maintenance roots cannot be a filesystem root.'
}
if ($resolvedMemoryRoot.StartsWith(
        $resolvedRuntimeRoot.TrimEnd('\') + '\',
        [StringComparison]::OrdinalIgnoreCase
    ) -or $resolvedMemoryRoot -ceq $resolvedRuntimeRoot) {
    throw 'RuntimeRoot cannot equal or contain the MemoryRoot.'
}
$sourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\src')).Path
$sourcePackage = Join-Path $sourceRoot 'agent_continuity'
$sourceRunner = (Resolve-Path -LiteralPath (
    Join-Path $PSScriptRoot 'Run-MemoryMaintenance.ps1'
)).Path
$sourceVerifier = (Resolve-Path -LiteralPath (
    Join-Path $PSScriptRoot 'Test-MemoryMaintenanceTask.ps1'
)).Path
$moduleNames = @(
    '__init__.py',
    'compaction.py',
    'memory_maintenance.py',
    'memory_store.py',
    'retention.py'
)
foreach ($moduleName in $moduleNames) {
    $sourceModule = Join-Path $sourcePackage $moduleName
    if (-not (Test-Path -LiteralPath $sourceModule -PathType Leaf)) {
        throw "Required Memory Maintenance source module is unavailable: $sourceModule"
    }
}
Test-PowerShellSource -Path $sourceRunner
Test-PowerShellSource -Path $sourceVerifier
Test-PowerShellSource -Path $PSCommandPath
$pythonPath = Resolve-PythonPath
foreach ($moduleName in $moduleNames) {
    $sourceModule = Join-Path $sourcePackage $moduleName
    & $pythonPath -c 'import ast,sys;ast.parse(open(sys.argv[1],mode=chr(114)+chr(98)).read())' `
        $sourceModule
    if ($LASTEXITCODE -ne 0) {
        throw "Python parser rejected $sourceModule"
    }
}

$powerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$destinationRunner = Join-Path $resolvedRuntimeRoot 'Run-MemoryMaintenance.ps1'
$destinationVerifier = Join-Path $resolvedRuntimeRoot 'Test-MemoryMaintenanceTask.ps1'
$destinationModuleRoot = Join-Path $resolvedRuntimeRoot 'python'
$destinationPackage = Join-Path $destinationModuleRoot 'agent_continuity'
$runtimeConfigPath = Join-Path $resolvedRuntimeRoot 'runtime-config.json'
$actionArguments = @(
    '-NoLogo',
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $destinationRunner),
    '-RuntimeConfig', ('"{0}"' -f $runtimeConfigPath)
) -join ' '
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentUser = $identity.Name
$currentSid = $identity.User.Value
$plan = [ordered]@{
    schema = 'coding-intelligence.memory-maintenance-install-plan/v1'
    status = if ($Apply) { 'apply_requested' } else { 'planned' }
    mode = if ($Apply) { 'explicit_apply' } else { 'dry_run_no_writes' }
    task_name = $TaskName
    task_user = $currentUser
    task_user_sid = $currentSid
    run_level = 'Limited'
    logon_type = 'Interactive'
    multiple_instances = 'IgnoreNew'
    interval_minutes = $IntervalMinutes
    execution_time_limit_minutes = 5
    max_items = $MaxItems
    max_bytes = $MaxBytes
    max_wall_seconds = $MaxWallSeconds
    hot_blob_budget_bytes = $HotBlobBudgetBytes
    memory_root = $resolvedMemoryRoot
    runtime_root = $resolvedRuntimeRoot
    python_path = $pythonPath
    action_execute = $powerShellExe
    action_arguments = $actionArguments
}
if (-not $Apply) {
    $plan | ConvertTo-Json -Depth 5 -Compress
    exit 0
}

$principalContext = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principalContext.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Install Memory Maintenance from the normal non-elevated current-user session.'
}
if (-not (Test-Path -LiteralPath $resolvedMemoryRoot -PathType Container)) {
    throw "Memory v1 root must already exist before installation: $resolvedMemoryRoot"
}

$action = New-ScheduledTaskAction `
    -Execute $powerShellExe `
    -Argument $actionArguments `
    -WorkingDirectory $resolvedRuntimeRoot
$scheduledAt = (Get-Date).AddMinutes($IntervalMinutes)
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $scheduledAt `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$existingTaskXml = if ($null -ne $existingTask) {
    Export-ScheduledTask -TaskName $TaskName
} else {
    $null
}
$tempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
$backupRoot = [IO.Path]::GetFullPath((Join-Path $tempRoot (
    'coding-intelligence-memory-maintenance-install.' + [guid]::NewGuid().ToString('N')
)))
if (-not $backupRoot.StartsWith($tempRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Resolved Memory Maintenance backup root escaped current-user TEMP.'
}
New-Item -ItemType Directory -Path $backupRoot | Out-Null
$runtimeExisted = Test-Path -LiteralPath $resolvedRuntimeRoot -PathType Container
if ($runtimeExisted) {
    Copy-Item -LiteralPath $resolvedRuntimeRoot -Destination (
        Join-Path $backupRoot 'runtime'
    ) -Recurse
}

try {
    [void](Assert-NoReparseChain `
        -Path $resolvedRuntimeRoot `
        -Boundary $runtimeBoundary `
        -Label 'RuntimeRoot before create')
    New-Item -ItemType Directory -Force -Path $destinationPackage | Out-Null
    [void](Assert-NoReparseChain `
        -Path $destinationPackage `
        -Boundary $runtimeBoundary `
        -Label 'deployed package')
    [void](Assert-NoReparseChain `
        -Path $destinationRunner `
        -Boundary $runtimeBoundary `
        -Label 'deployed runner')
    Copy-Item -LiteralPath $sourceRunner -Destination $destinationRunner -Force
    [void](Assert-NoReparseChain `
        -Path $destinationVerifier `
        -Boundary $runtimeBoundary `
        -Label 'deployed task verifier')
    Copy-Item -LiteralPath $sourceVerifier -Destination $destinationVerifier -Force
    $runnerSha256 = Get-Sha256 -Path $sourceRunner
    $verifierSha256 = Get-Sha256 -Path $sourceVerifier
    if ((Get-Sha256 -Path $destinationRunner) -cne $runnerSha256 -or
        (Get-Sha256 -Path $destinationVerifier) -cne $verifierSha256) {
        throw 'Deployed Memory Maintenance PowerShell source digest differs.'
    }
    foreach ($moduleName in $moduleNames) {
        Copy-Item `
            -LiteralPath (Join-Path $sourcePackage $moduleName) `
            -Destination (Join-Path $destinationPackage $moduleName) `
            -Force
        [void](Assert-NoReparseChain `
            -Path (Join-Path $destinationPackage $moduleName) `
            -Boundary $runtimeBoundary `
            -Label "deployed module $moduleName")
    }
    $moduleSha256 = [ordered]@{}
    foreach ($moduleName in $moduleNames) {
        $sourceModule = Join-Path $sourcePackage $moduleName
        $deployedModule = Join-Path $destinationPackage $moduleName
        $sourceHash = Get-Sha256 -Path $sourceModule
        if ((Get-Sha256 -Path $deployedModule) -cne $sourceHash) {
            throw "Deployed Memory Maintenance module digest differs: $moduleName"
        }
        $moduleSha256[$moduleName] = $sourceHash
    }
    $runtimeConfig = [ordered]@{
        schema = 'coding-intelligence.memory-maintenance-runtime/v1'
        python_path = $pythonPath
        module_root = $destinationModuleRoot
        memory_root = $resolvedMemoryRoot
        runner_root = (Join-Path $resolvedRuntimeRoot 'runner')
        runtime_root = $resolvedRuntimeRoot
        runner_path = $destinationRunner
        verifier_path = $destinationVerifier
        runner_sha256 = $runnerSha256
        verifier_sha256 = $verifierSha256
        task_name = $TaskName
        task_user_sid = $currentSid
        interval_minutes = $IntervalMinutes
        execution_limit_minutes = 5
        trigger_start_boundary = ([DateTimeOffset]$scheduledAt).ToString('o')
        action_execute = $powerShellExe
        action_arguments = $actionArguments
        max_items = $MaxItems
        max_bytes = $MaxBytes
        max_wall_seconds = $MaxWallSeconds
        hot_blob_budget_bytes = $HotBlobBudgetBytes
        module_sha256 = $moduleSha256
    }
    $temporaryConfig = "$runtimeConfigPath.$PID.tmp"
    [void](Assert-NoReparseChain `
        -Path $runtimeConfigPath `
        -Boundary $runtimeBoundary `
        -Label 'runtime config')
    [void](Assert-NoReparseChain `
        -Path $temporaryConfig `
        -Boundary $runtimeBoundary `
        -Label 'runtime config temporary')
    [IO.File]::WriteAllText(
        $temporaryConfig,
        ($runtimeConfig | ConvertTo-Json -Depth 4 -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporaryConfig -Destination $runtimeConfigPath -Force
    Test-PowerShellSource -Path $destinationRunner
    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $destinationModuleRoot
        & $pythonPath -m agent_continuity.memory_maintenance `
            --root $resolvedMemoryRoot `
            --max-items $MaxItems `
            --max-bytes $MaxBytes `
            --max-wall-seconds $MaxWallSeconds `
            --hot-blob-budget-bytes $HotBlobBudgetBytes *> $null
        if ($LASTEXITCODE -ne 0) {
            throw 'Deployed Memory Maintenance dry-run self-check failed.'
        }
    } finally {
        $env:PYTHONPATH = $previousPythonPath
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description 'Bounded pressure-triggered sovereign Memory v1 retention maintenance.' `
        -Force | Out-Null

    $installed = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $installedSid = ([Security.Principal.NTAccount]::new(
        [string]$installed.Principal.UserId
    )).Translate([Security.Principal.SecurityIdentifier]).Value
    $installedAction = @($installed.Actions)
    $installedTriggers = @($installed.Triggers)
    $installedInterval = if ($installedTriggers.Count -eq 1) {
        [Xml.XmlConvert]::ToTimeSpan([string]$installedTriggers[0].Repetition.Interval)
    } else {
        [TimeSpan]::Zero
    }
    $installedExecutionLimit = [Xml.XmlConvert]::ToTimeSpan(
        [string]$installed.Settings.ExecutionTimeLimit
    )
    $installedStart = if ($installedTriggers.Count -eq 1) {
        [DateTimeOffset]::Parse([string]$installedTriggers[0].StartBoundary)
    } else {
        [DateTimeOffset]::MinValue
    }
    $expectedStart = [DateTimeOffset]$scheduledAt
    if ($installed.Principal.RunLevel -ne 'Limited' -or
        $installed.Principal.LogonType -ne 'Interactive' -or
        $installedSid -cne $currentSid -or
        $installed.Settings.MultipleInstances -ne 'IgnoreNew' -or
        $installedAction.Count -ne 1 -or
        [string]$installedAction[0].Execute -cne $powerShellExe -or
        [string]$installedAction[0].Arguments -cne $actionArguments -or
        [string]$installedAction[0].WorkingDirectory -cne $resolvedRuntimeRoot -or
        $installedTriggers.Count -ne 1 -or
        -not [bool]$installedTriggers[0].Enabled -or
        $installedInterval -ne (New-TimeSpan -Minutes $IntervalMinutes) -or
        [Math]::Abs(($installedStart - $expectedStart).TotalSeconds) -gt 1 -or
        -not [bool]$installed.Settings.Enabled -or
        $installedExecutionLimit -ne (New-TimeSpan -Minutes 5) -or
        $installed.State -eq 'Disabled') {
        throw 'Installed Memory Maintenance task violates its exact Limited ownership contract.'
    }
    $validationText = (& $destinationVerifier `
        -RuntimeConfig $runtimeConfigPath 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Installed Memory Maintenance task verifier failed: $validationText"
    }
    $validation = $validationText | ConvertFrom-Json -ErrorAction Stop
    if ($validation.status -cne 'valid') {
        throw 'Installed Memory Maintenance task verifier returned an invalid status.'
    }
} catch {
    if ($null -ne $existingTaskXml) {
        Register-ScheduledTask -TaskName $TaskName -Xml $existingTaskXml -Force | Out-Null
    } else {
        Unregister-ScheduledTask `
            -TaskName $TaskName `
            -Confirm:$false `
            -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $resolvedRuntimeRoot -PathType Container) {
        [void](Assert-NoReparseChain `
            -Path $resolvedRuntimeRoot `
            -Boundary $runtimeBoundary `
            -Label 'RuntimeRoot rollback removal')
        Remove-Item -LiteralPath $resolvedRuntimeRoot -Recurse -Force
    }
    $backupRuntime = Join-Path $backupRoot 'runtime'
    if ($runtimeExisted -and (Test-Path -LiteralPath $backupRuntime -PathType Container)) {
        Copy-Item -LiteralPath $backupRuntime -Destination $resolvedRuntimeRoot -Recurse
    }
    throw
} finally {
    [void](Assert-NoReparseChain `
        -Path $backupRoot `
        -Boundary $tempRoot `
        -Label 'installer backup cleanup')
    Remove-Item -LiteralPath $backupRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$installed = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$finalTriggers = @($installed.Triggers)
$plan.status = 'installed'
$plan.task_state = [string]$installed.State
$plan.last_task_result = $info.LastTaskResult
$plan.trigger_count = $finalTriggers.Count
$plan.trigger_enabled = [bool]$finalTriggers[0].Enabled
$plan.trigger_start_boundary = [string]$finalTriggers[0].StartBoundary
$plan.trigger_repetition_interval = [string]$finalTriggers[0].Repetition.Interval
$plan | ConvertTo-Json -Depth 5 -Compress
