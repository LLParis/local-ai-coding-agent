[CmdletBinding()]
param(
    [ValidateRange(1, 100)]
    [int]$Limit = 25
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$runtimeRoot = $PSScriptRoot
$configPath = Join-Path $runtimeRoot 'runtime-paths.json'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Missing catalog-maintenance runtime configuration: $configPath"
}

$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json -ErrorAction Stop
if ($config.schema -cne 'coding-intelligence.catalog-maintenance-runtime/v1') {
    throw 'Unexpected catalog-maintenance runtime configuration schema.'
}

$pythonPath = [string]$config.python_path
$codexPath = [string]$config.codex_path
$programPath = [string]$config.program_path
$codexHome = [string]$config.codex_home
foreach ($requiredPath in @($pythonPath, $codexPath, $programPath)) {
    if ([string]::IsNullOrWhiteSpace($requiredPath) -or
        -not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Configured runtime file is unavailable: $requiredPath"
    }
}
if ([string]::IsNullOrWhiteSpace($codexHome) -or
    -not (Test-Path -LiteralPath $codexHome -PathType Container)) {
    throw "Configured CODEX_HOME is unavailable: $codexHome"
}

$env:CODEX_HOME = $codexHome
$env:CODEX_BIN = $codexPath
$env:CODEX_CATALOG_LAUNCHER_PID = [string]$PID
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:NO_COLOR = '1'
$env:TERM = 'xterm-256color'

& $pythonPath $programPath --limit $Limit
exit $LASTEXITCODE
