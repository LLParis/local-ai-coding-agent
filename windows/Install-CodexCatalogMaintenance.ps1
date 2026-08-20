[CmdletBinding()]
param(
    [string]$TaskName = 'Coding Intelligence - Codex Catalog Maintenance',
    [ValidateRange(1, 100)]
    [int]$ArchiveLimit = 25,
    [ValidateRange(15, 1440)]
    [int]$IntervalMinutes = 60,
    [string]$RuntimeRoot = (Join-Path $env:LOCALAPPDATA 'CodingIntelligence\CatalogMaintenance')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Resolve-PythonPath {
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $launcher) {
        throw 'The Python launcher py.exe is unavailable.'
    }
    $resolved = (& $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null |
        Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw 'Could not resolve a stable Python 3 executable.'
    }
    return (Resolve-Path -LiteralPath $resolved).Path
}

function Resolve-CodexPath {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\OpenAI\Codex\bin\codex.exe'),
        (Join-Path $env:USERPROFILE '.codex\packages\standalone\current\bin\codex.exe'),
        (Join-Path $env:USERPROFILE '.codex\packages\standalone\current\codex.exe')
    )
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            continue
        }
        $version = & $candidate --version 2>$null
        if ($LASTEXITCODE -eq 0 -and $version -match '^codex-cli\s+[0-9]') {
            return [PSCustomObject]@{ Path = $candidate; Version = [string]$version }
        }
    }
    throw 'No executable current-user standalone Codex CLI was found.'
}

function Test-PowerShellSource {
    param([string]$Path)
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

$sourceProgram = (Resolve-Path -LiteralPath (
    Join-Path $PSScriptRoot '..\ops\codex_catalog_maintenance.py'
)).Path
$sourceWrapper = (Resolve-Path -LiteralPath (
    Join-Path $PSScriptRoot 'Run-CodexCatalogMaintenance.ps1'
)).Path
$pythonPath = Resolve-PythonPath
$codex = Resolve-CodexPath
$codexHome = (Resolve-Path -LiteralPath (Join-Path $env:USERPROFILE '.codex')).Path

& $pythonPath -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' $sourceProgram
if ($LASTEXITCODE -ne 0) {
    throw 'Python parser rejected the shared catalog-maintenance program.'
}
Test-PowerShellSource -Path $sourceWrapper
Test-PowerShellSource -Path $PSCommandPath

$destinationProgram = Join-Path $RuntimeRoot 'codex_catalog_maintenance.py'
$destinationWrapper = Join-Path $RuntimeRoot 'Run-CodexCatalogMaintenance.ps1'
$destinationConfig = Join-Path $RuntimeRoot 'runtime-paths.json'
$powerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$currentUser = $currentIdentity.Name
$currentSid = $currentIdentity.User.Value
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$actionArguments = @(
    '-NoLogo',
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $destinationWrapper),
    '-Limit', [string]$ArchiveLimit
) -join ' '
$action = New-ScheduledTaskAction `
    -Execute $powerShellExe `
    -Argument $actionArguments `
    -WorkingDirectory $RuntimeRoot
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes($IntervalMinutes) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$existingTaskXml = if ($null -ne $existingTask) {
    Export-ScheduledTask -TaskName $TaskName
} else {
    $null
}

$backupRoot = Join-Path $env:TEMP ('coding-intelligence-catalog-install.' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $backupRoot | Out-Null
$existingFiles = @{}
foreach ($destination in @($destinationProgram, $destinationWrapper, $destinationConfig)) {
    if (Test-Path -LiteralPath $destination -PathType Leaf) {
        $backup = Join-Path $backupRoot ([IO.Path]::GetFileName($destination))
        Copy-Item -LiteralPath $destination -Destination $backup
        $existingFiles[$destination] = $backup
    }
}

try {
    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    Copy-Item -LiteralPath $sourceProgram -Destination $destinationProgram -Force
    Copy-Item -LiteralPath $sourceWrapper -Destination $destinationWrapper -Force
    $runtimeConfig = [ordered]@{
        schema = 'coding-intelligence.catalog-maintenance-runtime/v1'
        python_path = $pythonPath
        codex_path = $codex.Path
        codex_version = $codex.Version
        codex_home = $codexHome
        program_path = $destinationProgram
    }
    $configTemporary = "$destinationConfig.$PID.tmp"
    [IO.File]::WriteAllText(
        $configTemporary,
        ($runtimeConfig | ConvertTo-Json -Depth 4),
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $configTemporary -Destination $destinationConfig -Force

    Test-PowerShellSource -Path $destinationWrapper
    & $pythonPath $destinationProgram --self-check *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'Deployed catalog-maintenance self-check failed.'
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description 'Reversibly archive completed Codex subagents older than 24 hours.' `
        -Force | Out-Null

    $installed = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $installedSid = ([System.Security.Principal.NTAccount]::new(
        [string]$installed.Principal.UserId
    )).Translate([System.Security.Principal.SecurityIdentifier]).Value
    if ($installed.Principal.RunLevel -ne 'Limited' -or
        $installedSid -cne $currentSid -or
        $installed.Settings.MultipleInstances -ne 'IgnoreNew') {
        throw (
            'Installed Scheduled Task does not match the Limited single-instance contract. ' +
            "RunLevel=$($installed.Principal.RunLevel); " +
            "UserId=$($installed.Principal.UserId); UserSid=$installedSid; " +
            "ExpectedUser=$currentUser; ExpectedSid=$currentSid; " +
            "MultipleInstances=$($installed.Settings.MultipleInstances)"
        )
    }
} catch {
    if ($null -ne $existingTaskXml) {
        Register-ScheduledTask -TaskName $TaskName -Xml $existingTaskXml -Force | Out-Null
    } else {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }
    foreach ($destination in @($destinationProgram, $destinationWrapper, $destinationConfig)) {
        if ($existingFiles.ContainsKey($destination)) {
            Copy-Item -LiteralPath $existingFiles[$destination] -Destination $destination -Force
        } else {
            Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
        }
    }
    throw
} finally {
    foreach ($backup in $existingFiles.Values) {
        Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $backupRoot -Force -ErrorAction SilentlyContinue
}

$installed = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
[ordered]@{
    status = 'installed'
    task_name = $TaskName
    state = [string]$installed.State
    user_id = [string]$installed.Principal.UserId
    run_level = [string]$installed.Principal.RunLevel
    logon_type = [string]$installed.Principal.LogonType
    multiple_instances = [string]$installed.Settings.MultipleInstances
    execution_time_limit = [string]$installed.Settings.ExecutionTimeLimit
    interval_minutes = $IntervalMinutes
    archive_limit = $ArchiveLimit
    last_task_result = $info.LastTaskResult
    runtime_root = $RuntimeRoot
    python_path = $pythonPath
    codex_path = $codex.Path
    codex_version = $codex.Version
} | ConvertTo-Json -Depth 4 -Compress
