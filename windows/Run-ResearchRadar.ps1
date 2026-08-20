[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$runtimeRoot = $PSScriptRoot
$runtimeConfigPath = Join-Path $runtimeRoot 'runtime-config.json'
$lastRunPath = Join-Path $runtimeRoot 'last-run.json'
$lockPath = Join-Path $runtimeRoot 'runner.lock'
$startedAt = [DateTimeOffset]::UtcNow
$lockStream = $null

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

function Write-AtomicUtf8Json {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][object]$Value
    )
    $temporary = "$Path.$PID.tmp"
    [IO.File]::WriteAllText(
        $temporary,
        ($Value | ConvertTo-Json -Depth 10 -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Get-OptionalProperty {
    param(
        [Parameter(Mandatory)][object]$Value,
        [Parameter(Mandatory)][string]$Name,
        [object]$Default = $null
    )
    $property = $Value.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $Default
    }
    return $property.Value
}

try {
    try {
        $lockStream = [IO.File]::Open(
            $lockPath,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    } catch [IO.IOException] {
        throw 'Another exact Research Radar runner already owns the runtime lock.'
    }

    if (-not (Test-Path -LiteralPath $runtimeConfigPath -PathType Leaf)) {
        throw "Missing Research Radar runtime configuration: $runtimeConfigPath"
    }
    $config = Get-Content -LiteralPath $runtimeConfigPath -Raw |
        ConvertFrom-Json -ErrorAction Stop
    if ($config.schema -cne 'coding-intelligence.research-radar-runtime/v2') {
        throw 'Unexpected Research Radar runtime configuration schema.'
    }

    $pythonPath = [string]$config.python_path
    $entryPath = [string]$config.entry_path
    $packageRoot = [string]$config.package_root
    $radarRoot = [string]$config.radar_root
    $syncConfig = [string]$config.sync_config
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "Configured Python runtime is unavailable: $pythonPath"
    }
    if (-not (Test-Path -LiteralPath $entryPath -PathType Leaf)) {
        throw "Configured deployed Radar entrypoint is unavailable: $entryPath"
    }
    if (-not (Test-Path -LiteralPath $packageRoot -PathType Container)) {
        throw "Configured deployed package root is unavailable: $packageRoot"
    }
    if (-not (Test-Path -LiteralPath $syncConfig -PathType Leaf)) {
        throw "Configured Research Radar sync config is unavailable: $syncConfig"
    }
    if ([string]::IsNullOrWhiteSpace($radarRoot)) {
        throw 'Configured Research Radar state root is empty.'
    }

    $manifest = @($config.file_manifest)
    if ($manifest.Count -lt 4 -or $manifest.Count -gt 64) {
        throw 'Deployed Research Radar file manifest count is outside its bound.'
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 5 -Compress
    $computedManifestSha256 = Get-BytesSha256 -Bytes (
        [Text.Encoding]::UTF8.GetBytes($manifestJson)
    )
    if ($computedManifestSha256 -cne [string]$config.manifest_sha256) {
        throw 'Deployed Research Radar manifest identity is invalid.'
    }
    $seenRelativePaths = @{}
    foreach ($entry in $manifest) {
        $relativePath = [string]$entry.relative_path
        $expectedSha256 = [string]$entry.sha256
        if ([string]::IsNullOrWhiteSpace($relativePath) -or
            [IO.Path]::IsPathRooted($relativePath) -or
            $relativePath.Contains('..') -or
            $relativePath.Contains('\')) {
            throw "Deployed Research Radar manifest path is invalid: $relativePath"
        }
        if ($seenRelativePaths.ContainsKey($relativePath)) {
            throw "Duplicate deployed Research Radar manifest path: $relativePath"
        }
        $seenRelativePaths[$relativePath] = $true
        $installedPath = Join-Path $runtimeRoot ($relativePath.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $installedPath -PathType Leaf)) {
            throw "Deployed Research Radar manifest file is missing: $relativePath"
        }
        if ((Get-Sha256 -Path $installedPath) -cne $expectedSha256) {
            throw "Deployed Research Radar manifest hash mismatch: $relativePath"
        }
    }
    if ((Get-Sha256 -Path $syncConfig) -cne [string]$config.sync_config_sha256) {
        throw 'Research Radar sync configuration hash changed after installation.'
    }

    $env:CODING_INTELLIGENCE_RADAR_LAUNCHER_PID = [string]$PID
    $env:PYTHONPATH = $packageRoot
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    $outputLines = @(
        & $pythonPath `
            $entryPath `
            --root $radarRoot `
            --config $syncConfig `
            --apply 2>&1 |
            ForEach-Object { [string]$_ }
    )
    $exitCode = $LASTEXITCODE
    $output = ($outputLines -join "`n")
    $outputBytes = [Text.Encoding]::UTF8.GetBytes($output)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $outputHashBytes = $sha256.ComputeHash($outputBytes)
    } finally {
        $sha256.Dispose()
    }
    $outputSha256 = 'sha256:' + (($outputHashBytes | ForEach-Object {
        $_.ToString('x2')
    }) -join '')
    $boundedOutput = if ($output.Length -le 8192) {
        $output
    } else {
        $output.Substring(0, 8192)
    }
    $summary = $null
    try {
        $parsedOutput = $output | ConvertFrom-Json -ErrorAction Stop
        $summary = [ordered]@{
            status = [string](Get-OptionalProperty $parsedOutput 'status')
            write_outcome = Get-OptionalProperty $parsedOutput 'write_outcome'
            mode = [string](Get-OptionalProperty $parsedOutput 'mode')
            run_id = [string](Get-OptionalProperty $parsedOutput 'run_id')
            daily_discovery_due = [bool](Get-OptionalProperty $parsedOutput 'daily_discovery_due')
            weekly_version_recheck_due = [bool](Get-OptionalProperty $parsedOutput 'weekly_version_recheck_due')
            discovery_page_complete = Get-OptionalProperty $parsedOutput 'discovery_page_complete'
            discovery_total_results = Get-OptionalProperty $parsedOutput 'discovery_total_results'
            discovery_next_start = Get-OptionalProperty $parsedOutput 'discovery_next_start'
            discovery_pagination_restarted = Get-OptionalProperty $parsedOutput 'discovery_pagination_restarted'
            currentness = Get-OptionalProperty $parsedOutput 'currentness'
            discovered_items = Get-OptionalProperty $parsedOutput 'discovered_items'
            rechecked_items = Get-OptionalProperty $parsedOutput 'rechecked_items'
            exact_versions = Get-OptionalProperty $parsedOutput 'exact_versions'
            artifact_verification_selected = Get-OptionalProperty $parsedOutput 'artifact_verification_selected'
            artifact_verification_deferred = Get-OptionalProperty $parsedOutput 'artifact_verification_deferred'
            artifact_manual_review_required = Get-OptionalProperty $parsedOutput 'artifact_manual_review_required'
            triage_records = Get-OptionalProperty $parsedOutput 'triage_records'
            network = Get-OptionalProperty $parsedOutput 'network'
            write_results = Get-OptionalProperty $parsedOutput 'write_results'
            authority = Get-OptionalProperty $parsedOutput 'authority'
        }
    } catch {
        $summary = $null
    }
    $record = [ordered]@{
        schema = 'coding-intelligence.research-radar-runner-result/v2'
        launcher_pid = $PID
        started_at = $startedAt.ToString('o')
        completed_at = [DateTimeOffset]::UtcNow.ToString('o')
        exit_code = $exitCode
        python_path = $pythonPath
        entry_path = $entryPath
        package_root = $packageRoot
        runtime_manifest_sha256 = [string]$config.manifest_sha256
        radar_root = $radarRoot
        sync_config_sha256 = [string]$config.sync_config_sha256
        output_sha256 = $outputSha256
        output_truncated = $output.Length -gt 8192
        output = $boundedOutput
        summary = $summary
    }
    Write-AtomicUtf8Json -Path $lastRunPath -Value $record
    exit $exitCode
} catch {
    $failure = [ordered]@{
        schema = 'coding-intelligence.research-radar-runner-result/v2'
        launcher_pid = $PID
        started_at = $startedAt.ToString('o')
        completed_at = [DateTimeOffset]::UtcNow.ToString('o')
        exit_code = 2
        error_type = $_.Exception.GetType().FullName
        error = $_.Exception.Message
    }
    Write-AtomicUtf8Json -Path $lastRunPath -Value $failure
    Write-Error $_
    exit 2
} finally {
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}
