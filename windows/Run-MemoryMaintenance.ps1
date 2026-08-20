[CmdletBinding()]
param(
    [string]$RuntimeConfig = (Join-Path $PSScriptRoot 'runtime-config.json')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function ConvertTo-WindowsArgument {
    param([Parameter(Mandatory)][string]$Value)
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + (($Value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
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

$configuredConfig = [IO.Path]::GetFullPath($RuntimeConfig)
$configBoundary = [IO.Path]::GetDirectoryName($configuredConfig)
[void](Assert-NoReparseChain `
    -Path $configuredConfig `
    -Boundary $configBoundary `
    -Label 'runtime config')
$resolvedConfig = (Resolve-Path -LiteralPath $configuredConfig -ErrorAction Stop).Path
$config = Get-Content -LiteralPath $resolvedConfig -Raw |
    ConvertFrom-Json -ErrorAction Stop
if ($config.schema -cne 'coding-intelligence.memory-maintenance-runtime/v1') {
    throw 'Unexpected Memory Maintenance runtime configuration schema.'
}
if ((Get-Sha256 -Path $PSCommandPath) -cne [string]$config.runner_sha256) {
    throw 'Deployed Memory Maintenance runner digest differs from runtime config.'
}

$pythonPath = [string]$config.python_path
$moduleRoot = [string]$config.module_root
$memoryRoot = [string]$config.memory_root
$configDirectory = [IO.Path]::GetDirectoryName($resolvedConfig)
$runnerRoot = [IO.Path]::GetFullPath([string]$config.runner_root)
if (-not $runnerRoot.StartsWith(
        $configDirectory.TrimEnd('\') + '\',
        [StringComparison]::OrdinalIgnoreCase
    )) {
    throw 'Configured runner root must remain below the deployed runtime directory.'
}
[void](Assert-NoReparseChain `
    -Path $moduleRoot `
    -Boundary $configDirectory `
    -Label 'module root')
[void](Assert-NoReparseChain `
    -Path $runnerRoot `
    -Boundary $configDirectory `
    -Label 'runner root')
[void](Assert-NoReparseChain `
    -Path $memoryRoot `
    -Boundary ([IO.Path]::GetPathRoot($memoryRoot)) `
    -Label 'memory root')
$maxItems = [int]$config.max_items
$maxBytes = [long]$config.max_bytes
$maxWallSeconds = [int]$config.max_wall_seconds
$hotBlobBudgetBytes = [long]$config.hot_blob_budget_bytes
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Configured Python executable is unavailable: $pythonPath"
}
if (-not (Test-Path -LiteralPath $moduleRoot -PathType Container)) {
    throw "Configured module root is unavailable: $moduleRoot"
}
if (-not (Test-Path -LiteralPath $memoryRoot -PathType Container)) {
    throw "Configured Memory v1 root is unavailable: $memoryRoot"
}
foreach ($module in @(
        '__init__.py',
        'memory_maintenance.py',
        'memory_store.py',
        'retention.py',
        'compaction.py'
    )) {
    $modulePath = Join-Path $moduleRoot ('agent_continuity\' + $module)
    if (-not (Test-Path -LiteralPath $modulePath -PathType Leaf)) {
        throw "Deployed Memory Maintenance module is unavailable: $modulePath"
    }
    [void](Assert-NoReparseChain `
        -Path $modulePath `
        -Boundary $configDirectory `
        -Label "deployed module $module")
    $hashProperty = $config.module_sha256.PSObject.Properties[$module]
    if ($null -eq $hashProperty -or
        (Get-Sha256 -Path $modulePath) -cne [string]$hashProperty.Value) {
        throw "Deployed Memory Maintenance module digest differs: $modulePath"
    }
}
if ($maxItems -lt 1 -or $maxItems -gt 100 -or
    $maxBytes -lt 1 -or $maxBytes -gt 1099511627776 -or
    $maxWallSeconds -lt 5 -or $maxWallSeconds -gt 300 -or
    $hotBlobBudgetBytes -lt 1) {
    throw 'Memory Maintenance runtime limits are outside their bounded contract.'
}

New-Item -ItemType Directory -Force -Path $runnerRoot | Out-Null
[void](Assert-NoReparseChain `
    -Path $runnerRoot `
    -Boundary $configDirectory `
    -Label 'runner root after create')
$ownedLogs = @(
    Get-ChildItem -LiteralPath $runnerRoot -File -ErrorAction Stop |
        Where-Object {
            $_.Name -match '^[0-9a-f-]{36}\.(stdout\.json|stderr\.txt)$'
        } |
        Sort-Object LastWriteTimeUtc, Name
)
$excess = [Math]::Max(0, $ownedLogs.Count - 200)
for ($index = 0; $index -lt $excess; $index++) {
    [void](Assert-NoReparseChain `
        -Path $ownedLogs[$index].FullName `
        -Boundary $configDirectory `
        -Label 'runner log cleanup')
    Remove-Item -LiteralPath $ownedLogs[$index].FullName -Force
}
$runId = [guid]::NewGuid().ToString('D')
$stdoutPath = Join-Path $runnerRoot ($runId + '.stdout.json')
$stderrPath = Join-Path $runnerRoot ($runId + '.stderr.txt')
[void](Assert-NoReparseChain `
    -Path $stdoutPath `
    -Boundary $configDirectory `
    -Label 'runner stdout')
[void](Assert-NoReparseChain `
    -Path $stderrPath `
    -Boundary $configDirectory `
    -Label 'runner stderr')
$arguments = @(
    '-m',
    'agent_continuity.memory_maintenance',
    '--root', $memoryRoot,
    '--apply',
    '--max-items', [string]$maxItems,
    '--max-bytes', [string]$maxBytes,
    '--max-wall-seconds', [string]$maxWallSeconds,
    '--hot-blob-budget-bytes', [string]$hotBlobBudgetBytes
)
$argumentText = ($arguments | ForEach-Object {
    ConvertTo-WindowsArgument -Value ([string]$_)
}) -join ' '

$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $moduleRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:NO_COLOR = '1'
try {
    $child = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList $argumentText `
        -WorkingDirectory $moduleRoot `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -WindowStyle Hidden `
        -PassThru
    $completed = $child.WaitForExit(($maxWallSeconds + 30) * 1000)
    if (-not $completed) {
        $ownedPid = $child.Id
        $child.Kill()
        [void]$child.WaitForExit(10000)
        throw "Exact Memory Maintenance child PID $ownedPid exceeded its wall limit."
    }
    $child.WaitForExit()
    $child.Refresh()
    [int]$exitCode = $child.ExitCode
} finally {
    $env:PYTHONPATH = $previousPythonPath
}

$stdout = if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) {
    $rawStdout = Get-Content -LiteralPath $stdoutPath -Raw
    if ($null -eq $rawStdout) { '' } else { $rawStdout.Trim() }
} else {
    ''
}
$stderr = if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
    $rawStderr = Get-Content -LiteralPath $stderrPath -Raw
    if ($null -eq $rawStderr) { '' } else { $rawStderr.Trim() }
} else {
    ''
}
if ($exitCode -ne 0) {
    throw "Memory Maintenance child failed with exit $exitCode`: $stderr $stdout"
}
$report = $stdout | ConvertFrom-Json -ErrorAction Stop
if ([string]$report.schema -cne 'coding-intelligence-memory-maintenance-report/v1' -or
    [string]$report.status -eq 'failed') {
    throw 'Memory Maintenance child returned an invalid or failed report.'
}

[ordered]@{
    schema = 'coding-intelligence.memory-maintenance-runner-result/v1'
    status = 'completed'
    task_owner_pid = $PID
    exact_child_pid = $child.Id
    child_exit_code = $exitCode
    maintenance_status = [string]$report.status
    report_sha256 = [string]$report.report_sha256
    evidence_path = [string]$report.evidence_path
    stdout_path = $stdoutPath
    stderr_path = $stderrPath
} | ConvertTo-Json -Depth 4 -Compress
