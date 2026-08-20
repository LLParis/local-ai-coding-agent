[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RuntimeConfig
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

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
    foreach ($part in (@('') + @($parts))) {
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

$configured = [IO.Path]::GetFullPath($RuntimeConfig)
$runtimeRoot = [IO.Path]::GetDirectoryName($configured)
[void](Assert-NoReparseChain `
    -Path $configured `
    -Boundary $runtimeRoot `
    -Label 'runtime config')
$config = Get-Content -LiteralPath $configured -Raw |
    ConvertFrom-Json -ErrorAction Stop
if ($config.schema -cne 'coding-intelligence.memory-maintenance-runtime/v1') {
    throw 'Unexpected Memory Maintenance runtime configuration schema.'
}
if ((Get-Sha256 -Path $PSCommandPath) -cne [string]$config.verifier_sha256) {
    throw 'Deployed Memory Maintenance verifier digest differs from runtime config.'
}

$taskName = [string]$config.task_name
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
$info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
$actions = @($task.Actions)
$triggers = @($task.Triggers)
$installedSid = ([Security.Principal.NTAccount]::new(
    [string]$task.Principal.UserId
)).Translate([Security.Principal.SecurityIdentifier]).Value
$interval = if ($triggers.Count -eq 1) {
    [Xml.XmlConvert]::ToTimeSpan([string]$triggers[0].Repetition.Interval)
} else {
    [TimeSpan]::Zero
}
$executionLimit = [Xml.XmlConvert]::ToTimeSpan(
    [string]$task.Settings.ExecutionTimeLimit
)
$startBoundary = if ($triggers.Count -eq 1) {
    [DateTimeOffset]::Parse([string]$triggers[0].StartBoundary)
} else {
    [DateTimeOffset]::MinValue
}
$expectedStart = [DateTimeOffset]::Parse([string]$config.trigger_start_boundary)

if ($task.Principal.RunLevel -ne 'Limited' -or
    $task.Principal.LogonType -ne 'Interactive' -or
    $installedSid -cne [string]$config.task_user_sid -or
    $task.Settings.MultipleInstances -ne 'IgnoreNew' -or
    -not [bool]$task.Settings.Enabled -or
    $executionLimit -ne (New-TimeSpan -Minutes ([int]$config.execution_limit_minutes)) -or
    $task.State -eq 'Disabled' -or
    $actions.Count -ne 1 -or
    [string]$actions[0].Execute -cne [string]$config.action_execute -or
    [string]$actions[0].Arguments -cne [string]$config.action_arguments -or
    [string]$actions[0].WorkingDirectory -cne [string]$config.runtime_root -or
    $triggers.Count -ne 1 -or
    -not [bool]$triggers[0].Enabled -or
    $interval -ne (New-TimeSpan -Minutes ([int]$config.interval_minutes)) -or
    [Math]::Abs(($startBoundary - $expectedStart).TotalSeconds) -gt 1) {
    throw (
        'Memory Maintenance Scheduled Task violates its exact ownership contract. ' +
        "RunLevel=$($task.Principal.RunLevel); LogonType=$($task.Principal.LogonType); " +
        "Sid=$installedSid; ExpectedSid=$($config.task_user_sid); " +
        "MultipleInstances=$($task.Settings.MultipleInstances); " +
        "SettingsEnabled=$($task.Settings.Enabled); State=$($task.State); " +
        "ExecutionLimit=$($task.Settings.ExecutionTimeLimit); " +
        "ExpectedExecutionMinutes=$($config.execution_limit_minutes); " +
        "Actions=$($actions.Count); Triggers=$($triggers.Count); " +
        "TriggerEnabled=$($triggers[0].Enabled); " +
        "Interval=$($triggers[0].Repetition.Interval); " +
        "ExpectedIntervalMinutes=$($config.interval_minutes); " +
        "Start=$($triggers[0].StartBoundary); ExpectedStart=$($config.trigger_start_boundary)"
    )
}

[ordered]@{
    schema = 'coding-intelligence.memory-maintenance-task-validation/v1'
    status = 'valid'
    task_name = $taskName
    state = [string]$task.State
    user_sid = $installedSid
    run_level = [string]$task.Principal.RunLevel
    logon_type = [string]$task.Principal.LogonType
    action_count = $actions.Count
    trigger_count = $triggers.Count
    trigger_enabled = [bool]$triggers[0].Enabled
    trigger_start_boundary = [string]$triggers[0].StartBoundary
    repetition_interval = [string]$triggers[0].Repetition.Interval
    multiple_instances = [string]$task.Settings.MultipleInstances
    execution_time_limit = [string]$task.Settings.ExecutionTimeLimit
    last_task_result = $info.LastTaskResult
} | ConvertTo-Json -Depth 4 -Compress
