param(
    [Parameter(Mandatory = $true)]
    [string]$Task,
    [switch]$PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$switcher = Join-Path $root "windows\Switch-ExcaliburBackend.ps1"
$continuity = Join-Path $PSScriptRoot "continuity.cmd"
$taskPath = (Resolve-Path -LiteralPath $Task -ErrorAction Stop).Path
$plan = Get-Content -LiteralPath $taskPath -Raw | ConvertFrom-Json

$allowed = @(
    "schema_version", "backend", "workspace", "objective", "mutable",
    "context", "verify_context", "test_command", "timeout"
)
$unknown = @($plan.PSObject.Properties.Name | Where-Object { $_ -notin $allowed })
if ($unknown.Count) {
    throw "Unknown task fields: $($unknown -join ', ')"
}
if ([int]$plan.schema_version -ne 1) {
    throw "Unsupported coding-task schema."
}
if ([string]$plan.backend -notin @("Qwen38", "Ollama")) {
    throw "backend must be Qwen38 or Ollama."
}
$workspace = (Resolve-Path -LiteralPath ([string]$plan.workspace) -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $workspace -PathType Container)) {
    throw "workspace must be an existing directory."
}
if ([string]::IsNullOrWhiteSpace([string]$plan.objective)) {
    throw "objective is required."
}
$mutable = @($plan.mutable)
$context = @($plan.context)
$verifyContext = @($plan.verify_context)
$testCommand = @($plan.test_command)
if (-not $mutable.Count -or -not $context.Count -or -not $verifyContext.Count -or -not $testCommand.Count) {
    throw "mutable, context, verify_context, and test_command must be non-empty arrays."
}
$timeout = if ($null -eq $plan.timeout) { 180 } else { [int]$plan.timeout }
if ($timeout -lt 10 -or $timeout -gt 1800) {
    throw "timeout must be between 10 and 1800 seconds."
}

if ([string]$plan.backend -eq "Qwen38") {
    $baseUrl = "http://127.0.0.1:8818/v1"
    $model = "arm-qwen38-q6-text"
} else {
    $baseUrl = "http://127.0.0.1:11434/v1"
    $model = "gpt-oss-20b:latest"
}

$arguments = @(
    "local-edit", "--workspace", $workspace,
    "--objective", [string]$plan.objective
)
foreach ($path in $mutable) {
    $arguments += @("--mutable", [string]$path)
}
foreach ($path in $context) {
    $arguments += @("--context", [string]$path)
}
foreach ($path in $verifyContext) {
    $arguments += @("--verify-context", [string]$path)
}
$arguments += @("--base-url", $baseUrl, "--model", $model, "--timeout", [string]$timeout, "--")
$arguments += @($testCommand | ForEach-Object { [string]$_ })

if ($PlanOnly) {
    [pscustomobject]@{
        status = "planned"
        task = $taskPath
        backend = [string]$plan.backend
        model = $model
        workspace = $workspace
        modelCalls = 0
        automaticRetries = 0
    } | ConvertTo-Json -Compress
    exit 0
}

$swap = & $switcher -Backend ([string]$plan.backend) | ConvertFrom-Json
$output = @(& $continuity @arguments 2>&1)
$exitCode = $LASTEXITCODE
$edit = $null
try {
    $edit = $output[-1] | ConvertFrom-Json
} catch {
    # Preserve the exact bounded failure without attempting another model call.
}

[pscustomobject]@{
    status = if ($null -ne $edit) { [string]$edit.status } else { "failed" }
    task = $taskPath
    backend = [string]$plan.backend
    model = $model
    switch = $swap
    edit = $edit
    commandExit = $exitCode
    rawFailure = if ($null -eq $edit) { $output -join "`n" } else { $null }
    modelCalls = 1
    automaticRetries = 0
} | ConvertTo-Json -Depth 12 -Compress

exit $exitCode
