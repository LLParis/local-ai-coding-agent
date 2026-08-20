param(
    [Parameter(Mandatory = $true)]
    [string]$Task,
    [switch]$PlanOnly,
    [Parameter(DontShow = $true)]
    [string]$SwitcherPath,
    [Parameter(DontShow = $true)]
    [string]$ContinuityPath,
    [Parameter(DontShow = $true)]
    [string]$VerifierUri = "http://127.0.0.1:11434/api/chat"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$switcher = if ([string]::IsNullOrWhiteSpace($SwitcherPath)) {
    Join-Path $root "windows\Switch-ExcaliburBackend.ps1"
} else {
    $SwitcherPath
}
$continuity = if ([string]::IsNullOrWhiteSpace($ContinuityPath)) {
    Join-Path $PSScriptRoot "continuity.cmd"
} else {
    $ContinuityPath
}
$taskPath = (Resolve-Path -LiteralPath $Task -ErrorAction Stop).Path
$plan = Get-Content -LiteralPath $taskPath -Raw | ConvertFrom-Json

function Limit-TestOutput([string]$Text, [int]$Limit = 32768) {
    if ($Text.Length -le $Limit) {
        return $Text
    }
    return $Text.Substring(0, $Limit) + "`n...[test output truncated at $Limit characters]"
}

function Invoke-IndependentVerifier(
    [string]$Objective,
    [string]$Diff,
    [object]$EditResult,
    [string]$Uri,
    [int]$Timeout
) {
    $parsedUri = [Uri]$Uri
    if (
        $parsedUri.Scheme -ne "http" -or
        -not $parsedUri.IsLoopback -or
        $parsedUri.AbsolutePath -ne "/api/chat" -or
        -not [string]::IsNullOrEmpty($parsedUri.Query) -or
        -not [string]::IsNullOrEmpty($parsedUri.Fragment)
    ) {
        throw "verifier endpoint must be a loopback HTTP /api/chat endpoint"
    }

    $testOutput = Limit-TestOutput -Text ([string]$EditResult.test_output)
    $testCommand = @($EditResult.test_command | ForEach-Object { [string]$_ }) |
        ConvertTo-Json -Compress
    $prompt = @"
Objective:
$Objective

Scoped staged diff:
$Diff

Authoritative test result:
command: $testCommand
exit: $([int]$EditResult.test_exit)
output:
$testOutput

Judge only whether this scoped diff satisfies the objective, preserves unrelated behavior, and is consistent with the authoritative passing test. Return accept or reject. The hidden verifier source is intentionally unavailable.
"@
    $schema = @{
        type = "object"
        additionalProperties = $false
        properties = @{
            verdict = @{ type = "string"; enum = @("accept", "reject") }
            # Keep the transport grammar minimal and enforce the exact string
            # bounds on the parsed response below.
            reason = @{ type = "string" }
            risks = @{
                type = "array"
                maxItems = 3
                items = @{ type = "string" }
            }
        }
        required = @("verdict", "reason", "risks")
    }
    $request = @{
        model = "devstral-small-2:24b"
        messages = @(
            @{
                role = "system"
                content = "Act as an independent code verifier. Return only the requested JSON verdict."
            }
            @{ role = "user"; content = $prompt }
        )
        stream = $false
        format = $schema
        options = @{
            temperature = 0
            num_predict = 1024
        }
    }
    $bodyText = $request | ConvertTo-Json -Depth 12 -Compress
    $body = [Text.Encoding]::UTF8.GetBytes($bodyText)
    $response = Invoke-RestMethod `
        -Method Post `
        -Uri $Uri `
        -ContentType "application/json" `
        -Body $body `
        -TimeoutSec $Timeout
    if ($null -eq $response.message) {
        throw "verifier returned an unexpected response envelope"
    }
    $content = [string]$response.message.content
    if ([string]::IsNullOrWhiteSpace($content)) {
        throw "verifier returned no verdict JSON"
    }
    try {
        $verdict = $content | ConvertFrom-Json
    } catch {
        throw "verifier returned invalid verdict JSON"
    }
    if ($null -eq $verdict -or $verdict -is [System.Array]) {
        throw "verifier verdict must be one JSON object"
    }
    $expectedProperties = @("verdict", "reason", "risks")
    $actualProperties = @($verdict.PSObject.Properties.Name)
    $unexpected = @($actualProperties | Where-Object { $_ -notin $expectedProperties })
    if ($actualProperties.Count -ne 3 -or $unexpected.Count) {
        throw "verifier verdict must contain only verdict, reason, and risks"
    }
    if (
        $verdict.verdict -isnot [string] -or
        [string]$verdict.verdict -notin @("accept", "reject")
    ) {
        throw "verifier verdict must be accept or reject"
    }
    if (
        $verdict.reason -isnot [string] -or
        [string]::IsNullOrWhiteSpace([string]$verdict.reason) -or
        ([string]$verdict.reason).Length -gt 2000
    ) {
        throw "verifier reason must be a non-empty string of at most 2000 characters"
    }
    if ($verdict.risks -isnot [System.Array]) {
        throw "verifier risks must be a JSON array"
    }
    $risks = @($verdict.risks)
    if ($risks.Count -gt 3) {
        throw "verifier risks may contain at most three items"
    }
    foreach ($risk in $risks) {
        if (
            $risk -isnot [string] -or
            [string]::IsNullOrWhiteSpace([string]$risk) -or
            ([string]$risk).Length -gt 1000
        ) {
            throw "each verifier risk must be a non-empty string of at most 1000 characters"
        }
    }
    return [ordered]@{
        status = if ([string]$verdict.verdict -eq "accept") { "accepted" } else { "rejected" }
        model = "devstral-small-2:24b"
        verdict = [string]$verdict.verdict
        reason = [string]$verdict.reason
        risks = $risks
        modelCalls = 1
        automaticRetries = 0
    }
}

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
        verifierModel = "devstral-small-2:24b"
        workspace = $workspace
        modelCalls = 0
        automaticRetries = 0
    } | ConvertTo-Json -Compress
    exit 0
}

$swap = $null
$verifierSwap = $null
$restore = $null
$edit = $null
$verifier = [ordered]@{
    status = "not_run"
    model = "devstral-small-2:24b"
    verdict = $null
    reason = "implementation did not reach verified test success"
    risks = @()
    modelCalls = 0
    automaticRetries = 0
}
$output = @()
$exitCode = 1
$resultStatus = "failed"
$resultExit = 1
$failure = $null
$implementationCalls = 0
$verifierCalls = 0
try {
    $swap = & $switcher -Backend ([string]$plan.backend) | ConvertFrom-Json
    $implementationCalls = 1
    $output = @(& $continuity @arguments 2>&1)
    $exitCode = $LASTEXITCODE
    if (-not $output.Count) {
        throw "local edit command returned no report"
    }
    $edit = $output[-1] | ConvertFrom-Json
    $editPassed = (
        $exitCode -eq 0 -and
        [string]$edit.status -eq "verified" -and
        [int]$edit.test_exit -eq 0
    )
    if (-not $editPassed) {
        $resultStatus = "failed"
        $resultExit = if ($exitCode -eq 0) { 1 } else { $exitCode }
    } else {
        if ([string]$plan.backend -ne "Ollama") {
            $verifierSwap = & $switcher -Backend "Ollama" | ConvertFrom-Json
        } else {
            $verifierSwap = $swap
        }
        $verifierCalls = 1
        $verifierStarted = [Diagnostics.Stopwatch]::StartNew()
        try {
            $verifier = Invoke-IndependentVerifier `
                -Objective ([string]$plan.objective) `
                -Diff ([string]$edit.diff) `
                -EditResult $edit `
                -Uri $VerifierUri `
                -Timeout $timeout
            $verifierStarted.Stop()
            $verifier.seconds = [math]::Round($verifierStarted.Elapsed.TotalSeconds, 3)
        } catch {
            $verifierStarted.Stop()
            $verifier = [ordered]@{
                status = "failed"
                model = "devstral-small-2:24b"
                verdict = $null
                reason = $_.Exception.Message
                risks = @()
                seconds = [math]::Round($verifierStarted.Elapsed.TotalSeconds, 3)
                modelCalls = 1
                automaticRetries = 0
            }
        }
        if ([string]$verifier.status -eq "accepted") {
            $resultStatus = "verified"
            $resultExit = 0
        } elseif ([string]$verifier.status -eq "rejected") {
            $resultStatus = "rejected"
            $resultExit = 1
        } else {
            $resultStatus = "failed"
            $resultExit = 1
        }
    }
} catch {
    $failure = $_.Exception.Message
    if ($null -eq $edit -and $output.Count) {
        try {
            $edit = $output[-1] | ConvertFrom-Json
        } catch {
            # Keep the original bounded failure and raw output.
        }
    }
    $resultStatus = "failed"
    $resultExit = if ($exitCode -gt 0) { $exitCode } else { 1 }
} finally {
    try {
        $restore = & $switcher -Backend "Qwen38" | ConvertFrom-Json
    } catch {
        $restore = [ordered]@{ status = "failed"; error = $_.Exception.Message }
        $resultStatus = "failed"
        $resultExit = 1
        if ($null -eq $failure) {
            $failure = "failed to restore Qwen38 idle backend: $($_.Exception.Message)"
        }
    }
}

[pscustomobject]@{
    status = $resultStatus
    task = $taskPath
    backend = [string]$plan.backend
    model = $model
    switch = $swap
    edit = $edit
    verifierSwitch = $verifierSwap
    verifier = $verifier
    restore = $restore
    commandExit = $exitCode
    processExit = $resultExit
    rawFailure = if ($null -ne $failure) {
        $failure
    } elseif ($null -eq $edit) {
        $output -join "`n"
    } else {
        $null
    }
    modelCalls = $implementationCalls + $verifierCalls
    automaticRetries = 0
} | ConvertTo-Json -Depth 12 -Compress

exit $resultExit
