[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'research-radar.config.json'),
    [string]$TaskName = 'Coding Intelligence - Research Radar',
    [string]$RadarRoot = (Join-Path $env:LOCALAPPDATA 'CodingIntelligence\ResearchRadar'),
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'CodingIntelligence\ResearchRadarRuntime'),
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$DailyAt = '06:15',
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Get-BytesSha256 {
    param([Parameter(Mandatory)][byte[]]$Bytes)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha256.ComputeHash($Bytes)
    } finally {
        $sha256.Dispose()
    }
    return 'sha256:' + (($hashBytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-Sha256 {
    param([Parameter(Mandatory)][string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha256.ComputeHash($stream)
    } finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
    return 'sha256:' + (($hashBytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Resolve-PythonPath {
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $launcher) {
        throw 'The current-user Python launcher py.exe is unavailable.'
    }
    $resolved = (& $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null |
        Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw 'Could not resolve a stable Python 3 executable.'
    }
    return (Resolve-Path -LiteralPath $resolved).Path
}

function Test-PowerShellSource {
    param([Parameter(Mandatory)][string]$Path)
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $Path,
        [ref]$tokens,
        [ref]$errors
    )
    if ($errors.Count -ne 0) {
        throw "PowerShell parser rejected $Path`: $($errors[0].Message)"
    }
}

function Assert-SiblingPath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Parent,
        [Parameter(Mandatory)][string]$Prefix
    )
    $resolved = [IO.Path]::GetFullPath($Path)
    $resolvedParent = [IO.Path]::GetFullPath($Parent).TrimEnd('\')
    if (-not $resolved.StartsWith(
        $resolvedParent + '\' + $Prefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Resolved transactional path escaped its intended parent: $resolved"
    }
    return $resolved
}

$sourceRunner = (Resolve-Path -LiteralPath (
    Join-Path $PSScriptRoot 'Run-ResearchRadar.ps1'
)).Path
$sourceRuntimeEntry = (Resolve-Path -LiteralPath (
    Join-Path $PSScriptRoot 'research_radar_runtime_entry.py'
)).Path
$sourceConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$sourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$sourcePackageRoot = (Resolve-Path -LiteralPath (
    Join-Path $sourceRoot 'src\agent_continuity'
)).Path
$continuityCmd = (Resolve-Path -LiteralPath (
    Join-Path $sourceRoot 'bin\continuity.cmd'
)).Path
$sourceModules = @(
    (Get-Item -LiteralPath (Join-Path $sourcePackageRoot '__init__.py'))
    (Get-Item -LiteralPath (Join-Path $sourcePackageRoot 'research_radar.py'))
)
if ($sourceModules.Count -ne 2) {
    throw 'Research Radar minimal package must contain exactly two Python modules.'
}
$pythonPath = Resolve-PythonPath
$resolvedRadarRoot = [IO.Path]::GetFullPath($RadarRoot)
$resolvedRuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$allowedCurrentUserRoot = [IO.Path]::GetFullPath((
    Join-Path $env:LOCALAPPDATA 'CodingIntelligence'
)).TrimEnd('\')
foreach ($ownedRoot in @($resolvedRadarRoot, $resolvedRuntimeRoot)) {
    if (-not $ownedRoot.StartsWith(
        $allowedCurrentUserRoot + '\',
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Research Radar owned root escaped $allowedCurrentUserRoot`: $ownedRoot"
    }
}
$runtimeParent = Split-Path -Parent $resolvedRuntimeRoot
Test-PowerShellSource -Path $sourceRunner
Test-PowerShellSource -Path $PSCommandPath
foreach ($module in @($sourceModules) + @(Get-Item -LiteralPath $sourceRuntimeEntry)) {
    & $pythonPath -c "import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read())" `
        $module.FullName
    if ($LASTEXITCODE -ne 0) {
        throw "Python parser rejected $($module.FullName)"
    }
}

$validationText = (& $continuityCmd `
    radar-config-validate `
    --config $sourceConfig 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Research Radar sync configuration validation failed: $validationText"
}
$validatedConfig = $validationText | ConvertFrom-Json -ErrorAction Stop
if ($validatedConfig.status -cne 'valid' -or
    $validatedConfig.mode -cne 'read_only_no_network_no_writes') {
    throw 'Research Radar configuration validator returned an unexpected contract.'
}

$sourceConfigSha256 = Get-Sha256 -Path $sourceConfig
$sourceRunnerSha256 = Get-Sha256 -Path $sourceRunner
$sourceRuntimeEntrySha256 = Get-Sha256 -Path $sourceRuntimeEntry
$sourceModuleManifest = @($sourceModules | ForEach-Object {
    [ordered]@{
        name = $_.Name
        sha256 = Get-Sha256 -Path $_.FullName
    }
})
$sourceManifestJson = $sourceModuleManifest | ConvertTo-Json -Depth 4 -Compress
$sourceManifestSha256 = Get-BytesSha256 -Bytes (
    [Text.Encoding]::UTF8.GetBytes($sourceManifestJson)
)

$powerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$destinationRunner = Join-Path $resolvedRuntimeRoot 'Run-ResearchRadar.ps1'
$destinationRuntimeEntry = Join-Path $resolvedRuntimeRoot 'research_radar_runtime_entry.py'
$destinationConfig = Join-Path $resolvedRuntimeRoot 'sync-config.json'
$destinationPackageRoot = Join-Path $resolvedRuntimeRoot 'package'
$destinationModuleRoot = Join-Path $destinationPackageRoot 'agent_continuity'
$runtimeConfigPath = Join-Path $resolvedRuntimeRoot 'runtime-config.json'
$actionArguments = @(
    '-NoLogo',
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $destinationRunner)
) -join ' '
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentUser = $currentIdentity.Name
$currentSid = $currentIdentity.User.Value

$plan = [ordered]@{
    status = if ($Apply) { 'apply_requested' } else { 'planned' }
    mode = if ($Apply) { 'explicit_apply' } else { 'dry_run_no_writes' }
    task_name = $TaskName
    task_user = $currentUser
    task_user_sid = $currentSid
    run_level = 'Limited'
    logon_type = 'Interactive'
    multiple_instances = 'IgnoreNew'
    daily_at = $DailyAt
    runtime_root = $resolvedRuntimeRoot
    radar_root = $resolvedRadarRoot
    python_path = $pythonPath
    deployed_module_count = $sourceModules.Count
    source_module_manifest_sha256 = $sourceManifestSha256
    source_runner_sha256 = $sourceRunnerSha256
    source_runtime_entry_sha256 = $sourceRuntimeEntrySha256
    sync_config_source = $sourceConfig
    sync_config_sha256 = $sourceConfigSha256
    validated_config_contract_sha256 = [string]$validatedConfig.config_sha256
    action_execute = $powerShellExe
    action_arguments = $actionArguments
}
if (-not $Apply) {
    $plan | ConvertTo-Json -Depth 6 -Compress
    exit 0
}

$principalContext = [Security.Principal.WindowsPrincipal]::new($currentIdentity)
if ($principalContext.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Install Research Radar from the normal non-elevated current-user session.'
}
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask -and [string]$existingTask.State -eq 'Running') {
    throw 'Research Radar task is Running; wait for its exact owned run to finish.'
}
$existingTaskXml = if ($null -ne $existingTask) {
    Export-ScheduledTask -TaskName $TaskName
} else {
    $null
}

New-Item -ItemType Directory -Force -Path $runtimeParent | Out-Null
$transactionId = [guid]::NewGuid().ToString('N')
$stageRoot = Assert-SiblingPath `
    -Path ($resolvedRuntimeRoot + '.stage.' + $transactionId) `
    -Parent $runtimeParent `
    -Prefix ((Split-Path -Leaf $resolvedRuntimeRoot) + '.stage.')
$rollbackRoot = Assert-SiblingPath `
    -Path ($resolvedRuntimeRoot + '.rollback.' + $transactionId) `
    -Parent $runtimeParent `
    -Prefix ((Split-Path -Leaf $resolvedRuntimeRoot) + '.rollback.')
$oldRuntimeMoved = $false
$newRuntimeInstalled = $false

$scheduledTime = [DateTime]::Today.Add([TimeSpan]::ParseExact($DailyAt, 'hh\:mm', $null))
$action = New-ScheduledTaskAction `
    -Execute $powerShellExe `
    -Argument $actionArguments `
    -WorkingDirectory $resolvedRuntimeRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $scheduledTime
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

try {
    $stageModuleRoot = Join-Path $stageRoot 'package\agent_continuity'
    New-Item -ItemType Directory -Path $stageModuleRoot -Force | Out-Null
    Copy-Item -LiteralPath $sourceRunner -Destination (
        Join-Path $stageRoot 'Run-ResearchRadar.ps1'
    )
    Copy-Item -LiteralPath $sourceConfig -Destination (
        Join-Path $stageRoot 'sync-config.json'
    )
    Copy-Item -LiteralPath $sourceRuntimeEntry -Destination (
        Join-Path $stageRoot 'research_radar_runtime_entry.py'
    )
    foreach ($module in $sourceModules) {
        Copy-Item -LiteralPath $module.FullName -Destination (
            Join-Path $stageModuleRoot $module.Name
        )
    }
    $oldLastRun = Join-Path $resolvedRuntimeRoot 'last-run.json'
    if (Test-Path -LiteralPath $oldLastRun -PathType Leaf) {
        Copy-Item -LiteralPath $oldLastRun -Destination (
            Join-Path $stageRoot 'last-run.json'
        )
    }

    $fileManifest = @(
        [ordered]@{
            relative_path = 'Run-ResearchRadar.ps1'
            sha256 = Get-Sha256 -Path (Join-Path $stageRoot 'Run-ResearchRadar.ps1')
        },
        [ordered]@{
            relative_path = 'sync-config.json'
            sha256 = Get-Sha256 -Path (Join-Path $stageRoot 'sync-config.json')
        },
        [ordered]@{
            relative_path = 'research_radar_runtime_entry.py'
            sha256 = Get-Sha256 -Path (
                Join-Path $stageRoot 'research_radar_runtime_entry.py'
            )
        }
    )
    $fileManifest += @($sourceModules | ForEach-Object {
        $relative = 'package/agent_continuity/' + $_.Name
        [ordered]@{
            relative_path = $relative
            sha256 = Get-Sha256 -Path (
                Join-Path $stageRoot ($relative.Replace('/', '\'))
            )
        }
    })
    $fileManifest = @($fileManifest | Sort-Object relative_path)
    $manifestJson = $fileManifest | ConvertTo-Json -Depth 5 -Compress
    $manifestSha256 = Get-BytesSha256 -Bytes (
        [Text.Encoding]::UTF8.GetBytes($manifestJson)
    )
    $runtimeConfig = [ordered]@{
        schema = 'coding-intelligence.research-radar-runtime/v2'
        python_path = $pythonPath
        entry_path = $destinationRuntimeEntry
        package_root = $destinationPackageRoot
        radar_root = $resolvedRadarRoot
        sync_config = $destinationConfig
        sync_config_sha256 = Get-Sha256 -Path (Join-Path $stageRoot 'sync-config.json')
        manifest_sha256 = $manifestSha256
        file_manifest = $fileManifest
    }
    [IO.File]::WriteAllText(
        (Join-Path $stageRoot 'runtime-config.json'),
        ($runtimeConfig | ConvertTo-Json -Depth 8 -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    Test-PowerShellSource -Path (Join-Path $stageRoot 'Run-ResearchRadar.ps1')

    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = (Join-Path $stageRoot 'package')
        & $pythonPath (Join-Path $stageRoot 'research_radar_runtime_entry.py') `
            --validate-config `
            --config (Join-Path $stageRoot 'sync-config.json') *> $null
        if ($LASTEXITCODE -ne 0) {
            throw 'Staged deployed Research Radar package self-check failed.'
        }
    } finally {
        $env:PYTHONPATH = $previousPythonPath
    }

    if (Test-Path -LiteralPath $resolvedRuntimeRoot -PathType Container) {
        Move-Item -LiteralPath $resolvedRuntimeRoot -Destination $rollbackRoot
        $oldRuntimeMoved = $true
    }
    Move-Item -LiteralPath $stageRoot -Destination $resolvedRuntimeRoot
    $newRuntimeInstalled = $true
    New-Item -ItemType Directory -Force -Path $resolvedRadarRoot | Out-Null

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description 'Pinned bounded Research Radar runtime; never auto-adopts.' `
        -Force | Out-Null

    $installed = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $installedSid = ([Security.Principal.NTAccount]::new(
        [string]$installed.Principal.UserId
    )).Translate([Security.Principal.SecurityIdentifier]).Value
    $installedAction = @($installed.Actions)
    $installedTriggers = @($installed.Triggers)
    if ($installed.Principal.RunLevel -ne 'Limited' -or
        $installed.Principal.LogonType -ne 'Interactive' -or
        $installedSid -cne $currentSid -or
        $installed.Settings.MultipleInstances -ne 'IgnoreNew' -or
        $installedAction.Count -ne 1 -or
        [string]$installedAction[0].Execute -cne $powerShellExe -or
        [string]$installedAction[0].Arguments -cne $actionArguments -or
        [string]$installedAction[0].WorkingDirectory -cne $resolvedRuntimeRoot -or
        $installedTriggers.Count -ne 1 -or
        [int]$installedTriggers[0].DaysInterval -ne 1) {
        throw 'Installed Research Radar task does not match its exact ownership/trigger contract.'
    }

    $deployedConfig = Get-Content -LiteralPath $runtimeConfigPath -Raw |
        ConvertFrom-Json -ErrorAction Stop
    foreach ($entry in @($deployedConfig.file_manifest)) {
        $installedPath = Join-Path $resolvedRuntimeRoot (
            ([string]$entry.relative_path).Replace('/', '\')
        )
        if ((Get-Sha256 -Path $installedPath) -cne [string]$entry.sha256) {
            throw "Post-install deployed hash mismatch: $($entry.relative_path)"
        }
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
    if ($newRuntimeInstalled -and
        (Test-Path -LiteralPath $resolvedRuntimeRoot -PathType Container)) {
        Remove-Item -LiteralPath $resolvedRuntimeRoot -Recurse -Force
    }
    if ($oldRuntimeMoved -and
        (Test-Path -LiteralPath $rollbackRoot -PathType Container)) {
        Move-Item -LiteralPath $rollbackRoot -Destination $resolvedRuntimeRoot
    }
    if (Test-Path -LiteralPath $stageRoot -PathType Container) {
        Remove-Item -LiteralPath $stageRoot -Recurse -Force
    }
    throw
}

if (Test-Path -LiteralPath $rollbackRoot -PathType Container) {
    Remove-Item -LiteralPath $rollbackRoot -Recurse -Force
}
$installed = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$deployedConfig = Get-Content -LiteralPath $runtimeConfigPath -Raw |
    ConvertFrom-Json -ErrorAction Stop
$plan.status = 'installed'
$plan.task_state = [string]$installed.State
$plan.last_task_result = $info.LastTaskResult
$plan.deployed_manifest_sha256 = [string]$deployedConfig.manifest_sha256
$plan.deployed_runner_sha256 = Get-Sha256 -Path $destinationRunner
$plan.deployed_config_sha256 = Get-Sha256 -Path $destinationConfig
$plan.deployed_package_root = $destinationPackageRoot
$plan.deployed_entry_path = $destinationRuntimeEntry
$plan | ConvertTo-Json -Depth 8 -Compress
