param(
    [Parameter(Mandatory = $true)]
    [string]$Task,
    [switch]$PlanOnly,
    [Parameter(DontShow = $true)]
    [string]$SwitcherPath,
    [Parameter(DontShow = $true)]
    [string]$ContinuityPath,
    [Parameter(DontShow = $true)]
    [string]$MemoryContinuityPath,
    [Parameter(DontShow = $true)]
    [string]$MemoryRoot,
    [Parameter(DontShow = $true)]
    [string]$RunTaskId,
    [Parameter(DontShow = $true)]
    [string]$RunSessionId,
    [Parameter(DontShow = $true)]
    [string]$MacVerifierPath,
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
$memoryContinuity = if ([string]::IsNullOrWhiteSpace($MemoryContinuityPath)) {
    Join-Path $PSScriptRoot "continuity.cmd"
} else {
    $MemoryContinuityPath
}
$macVerifier = if ([string]::IsNullOrWhiteSpace($MacVerifierPath)) {
    Join-Path $PSScriptRoot "mac-swift-verifier.py"
} else {
    $MacVerifierPath
}
$taskPath = (Resolve-Path -LiteralPath $Task -ErrorAction Stop).Path
$plan = Get-Content -LiteralPath $taskPath -Raw | ConvertFrom-Json

function Limit-TestOutput([string]$Text, [int]$Limit = 32768) {
    if ($Text.Length -le $Limit) {
        return $Text
    }
    return $Text.Substring(0, $Limit) + "`n...[test output truncated at $Limit characters]"
}

function Get-TextSha256([AllowNull()][string]$Text) {
    if ($null -eq $Text) {
        $Text = ""
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes($Text)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return "sha256:" + ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    } finally {
        $hasher.Dispose()
    }
}

function Get-FileSha256OrEmpty([AllowNull()][string]$Path) {
    if (-not [string]::IsNullOrWhiteSpace($Path) -and (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $stream = [IO.File]::OpenRead($Path)
        $hasher = [Security.Cryptography.SHA256]::Create()
        try {
            return "sha256:" + ([BitConverter]::ToString(
                $hasher.ComputeHash($stream)
            )).Replace("-", "").ToLowerInvariant()
        } finally {
            $hasher.Dispose()
            $stream.Dispose()
        }
    }
    return Get-TextSha256 -Text ""
}

function Get-PropertyOrDefault([AllowNull()][object]$Value, [string]$Name, [object]$Default) {
    if ($null -ne $Value -and $null -ne $Value.PSObject.Properties[$Name]) {
        $candidate = $Value.PSObject.Properties[$Name].Value
        if ($null -ne $candidate) {
            return $candidate
        }
    }
    return $Default
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
    "context", "verify_context", "test_command", "timeout", "execution_verifier"
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
$executionVerifier = [string](Get-PropertyOrDefault `
    -Value $plan -Name "execution_verifier" -Default "")
if ($executionVerifier -notin @("", "mac-swift")) {
    throw "execution_verifier must be omitted or mac-swift."
}
$taskSha256 = Get-FileSha256OrEmpty -Path $taskPath
$macSourceSnapshot = $null
$macVerifierCommandPrefix = @()
$macRemoteTimeout = $null
if ($executionVerifier -eq "mac-swift") {
    if ($timeout -lt 60) {
        throw "mac-swift timeout must be at least 60 seconds."
    }
    if (
        $testCommand.Count -lt 2 -or
        [string]$testCommand[0] -ne "swift" -or
        [string]$testCommand[1] -ne "test"
    ) {
        throw "mac-swift test_command must begin with separate swift and test argv."
    }
    $macVerifier = (Resolve-Path -LiteralPath $macVerifier -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $macVerifier -PathType Leaf)) {
        throw "mac-swift verifier helper is unavailable."
    }
    if (-not [string]::IsNullOrWhiteSpace($env:CODING_INTELLIGENCE_PYTHON)) {
        $pythonExecutable = (Resolve-Path -LiteralPath $env:CODING_INTELLIGENCE_PYTHON `
            -ErrorAction Stop).Path
        $macVerifierCommandPrefix = @($pythonExecutable)
    } elseif ($null -ne (Get-Command "py" -ErrorAction SilentlyContinue)) {
        $macVerifierCommandPrefix = @("py", "-3")
    } elseif ($null -ne (Get-Command "python" -ErrorAction SilentlyContinue)) {
        $macVerifierCommandPrefix = @("python")
    } else {
        throw "mac-swift verifier requires the configured Python runtime."
    }
    $pythonExecutable = $macVerifierCommandPrefix[0]
    $pythonArguments = @($macVerifierCommandPrefix | Select-Object -Skip 1)
    $priorErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $macSnapshotOutput = @(& $pythonExecutable @pythonArguments `
            $macVerifier "snapshot" `
            "--task" $taskPath `
            "--expected-task-sha256" $taskSha256 `
            "--root" $workspace 2>&1)
        $macSnapshotExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $priorErrorAction
    }
    if ($macSnapshotExit -ne 0 -or -not $macSnapshotOutput.Count) {
        throw "mac-swift source checkpoint failed: $($macSnapshotOutput -join "`n")"
    }
    try {
        $macSourceSnapshot = $macSnapshotOutput[-1] | ConvertFrom-Json
    } catch {
        throw "mac-swift source checkpoint returned invalid JSON."
    }
    if (
        [string]$macSourceSnapshot.schema -ne `
            "coding-intelligence.mac-swift-source-snapshot/v1" -or
        [string]$macSourceSnapshot.status -ne "verified" -or
        [string]$macSourceSnapshot.task_sha256 -ne $taskSha256 -or
        [string]$macSourceSnapshot.manifest_sha256 -notmatch '^sha256:[0-9a-f]{64}$'
    ) {
        throw "mac-swift source checkpoint failed its schema or hash contract."
    }
    $macRemoteTimeout = $timeout - 30
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
$arguments += @("--base-url", $baseUrl, "--model", $model, "--timeout", [string]$timeout)

if ($PlanOnly) {
    [pscustomobject]@{
        status = "planned"
        task = $taskPath
        backend = [string]$plan.backend
        model = $model
        verifierModel = "devstral-small-2:24b"
        executionVerifier = if ($executionVerifier) { $executionVerifier } else { $null }
        sourceSnapshot = $macSourceSnapshot
        workspace = $workspace
        modelCalls = 0
        automaticRetries = 0
    } | ConvertTo-Json -Compress
    exit 0
}

$runTask = if ([string]::IsNullOrWhiteSpace($RunTaskId)) {
    [guid]::NewGuid().ToString()
} else {
    ([guid]::Parse($RunTaskId)).ToString()
}
$runSession = if ([string]::IsNullOrWhiteSpace($RunSessionId)) {
    [guid]::NewGuid().ToString()
} else {
    ([guid]::Parse($RunSessionId)).ToString()
}
$operationalMemoryRoot = if (-not [string]::IsNullOrWhiteSpace($MemoryRoot)) {
    [IO.Path]::GetFullPath($MemoryRoot)
} elseif (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Join-Path $env:LOCALAPPDATA "CodingIntelligence\MemoryV1"
} else {
    throw "LOCALAPPDATA is required for the default Coding Intelligence memory root."
}

$priorErrorAction = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    $memoryStartOutput = @(& $memoryContinuity `
        "run-memory-start" `
        "--root" $operationalMemoryRoot `
        "--task-id" $runTask `
        "--session-id" $runSession `
        "--workspace" $workspace `
        "--objective" ([string]$plan.objective) `
        "--model" $model `
        "--manifest" $taskPath 2>&1)
    $memoryStartExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $priorErrorAction
}
if ($memoryStartExit -ne 0 -or -not $memoryStartOutput.Count) {
    [pscustomobject]@{
        status = "failed"
        task = $taskPath
        taskId = $runTask
        sessionId = $runSession
        backend = [string]$plan.backend
        model = $model
        switch = $null
        edit = $null
        verifierSwitch = $null
        verifier = $null
        restore = $null
        commandExit = $memoryStartExit
        processExit = 1
        rawFailure = "pre-model memory checkpoint failed: $($memoryStartOutput -join "`n")"
        modelCalls = 0
        automaticRetries = 0
        memory = [ordered]@{
            status = "failed"
            root = $operationalMemoryRoot
            taskId = $runTask
            sessionId = $runSession
        }
    } | ConvertTo-Json -Depth 12 -Compress
    exit 1
}
$memoryStart = $memoryStartOutput[-1] | ConvertFrom-Json
$arguments += @(
    "--memory-root", $operationalMemoryRoot,
    "--task-id", $runTask,
    "--session-id", $runSession
)
$effectiveTestCommand = @($testCommand | ForEach-Object { [string]$_ })
if ($executionVerifier -eq "mac-swift") {
    $effectiveTestCommand = @(
        $macVerifierCommandPrefix +
        @(
            $macVerifier,
            "verify",
            "--task", $taskPath,
            "--expected-task-sha256", $taskSha256,
            "--stage", ".",
            "--expected-source-snapshot-sha256", `
                [string]$macSourceSnapshot.manifest_sha256,
            "--timeout", [string]$macRemoteTimeout
        )
    )
}
$arguments += "--"
$arguments += $effectiveTestCommand

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
$macExecutionVerifierReport = $null
try {
    $swap = & $switcher -Backend ([string]$plan.backend) | ConvertFrom-Json
    $output = @(& $continuity @arguments 2>&1)
    $exitCode = $LASTEXITCODE
    if (-not $output.Count) {
        throw "local edit command returned no report"
    }
    $edit = $output[-1] | ConvertFrom-Json
    $implementationCalls = [int](Get-PropertyOrDefault -Value $edit -Name "model_calls" -Default 0)
    $macExecutionVerifierPassed = $true
    if ($executionVerifier -eq "mac-swift") {
        $macExecutionVerifierPassed = $false
        try {
            $macExecutionVerifierReport = ([string]$edit.test_output).Trim() | ConvertFrom-Json
            $macCheckpoint = $macExecutionVerifierReport.checkpoint
            $macTransport = $macExecutionVerifierReport.transport
            $macRemote = $macExecutionVerifierReport.result
            if (
                [string]$macExecutionVerifierReport.schema -ne `
                    "coding-intelligence.mac-swift-verifier-result/v1" -or
                [string]$macExecutionVerifierReport.status -ne "verified" -or
                [int]$macExecutionVerifierReport.model_calls -ne 0 -or
                [int]$macExecutionVerifierReport.automatic_retries -ne 0 -or
                [string]$macCheckpoint.task_sha256 -ne $taskSha256 -or
                [string]$macCheckpoint.source_snapshot_before_sha256 -ne `
                    [string]$macSourceSnapshot.manifest_sha256 -or
                [string]$macCheckpoint.source_snapshot_after_sha256 -ne `
                    [string]$macSourceSnapshot.manifest_sha256 -or
                [string]$macCheckpoint.stage_snapshot_after_sha256 -ne `
                    [string]$macCheckpoint.stage_manifest_sha256 -or
                [bool]$macCheckpoint.source_workspace_mutated -or
                [int]$macTransport.ssh_sessions -ne 1 -or
                [int]$macTransport.connection_attempts -ne 1 -or
                [int]$macTransport.automatic_retries -ne 0 -or
                [int]$macTransport.exit_code -ne 0 -or
                [string]$macRemote.status -ne "verified" -or
                -not [bool]$macRemote.identity.matched -or
                [int]$macRemote.test.exit_code -ne 0 -or
                [bool]$macRemote.test.timed_out -or
                [bool]$macRemote.test.output_limit_exceeded -or
                [bool]$macRemote.test.owned_process_group_residual -or
                -not [bool]$macRemote.cleanup.succeeded -or
                [bool]$macRemote.cleanup.post_exists
            ) {
                throw "mac-swift verifier report failed its identity, lifecycle, or evidence contract"
            }
            $macExecutionVerifierPassed = $true
            $edit | Add-Member -NotePropertyName "mac_verifier" `
                -NotePropertyValue $macExecutionVerifierReport -Force
        } catch {
            $macExecutionVerifierPassed = $false
            $failure = "mac-swift verifier failed closed: $($_.Exception.Message)"
            if ($null -ne $edit) {
                $edit.status = "failed"
            }
        }
    }
    $editPassed = (
        $exitCode -eq 0 -and
        [string]$edit.status -eq "verified" -and
        [int]$edit.test_exit -eq 0 -and
        $macExecutionVerifierPassed
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

$emptyHash = Get-TextSha256 -Text ""
$stageLocator = [string](Get-PropertyOrDefault -Value $edit -Name "stage" -Default "unavailable")
$trajectoryLocator = [string](Get-PropertyOrDefault -Value $edit -Name "trajectory" -Default "unavailable")
$diffText = [string](Get-PropertyOrDefault -Value $edit -Name "diff" -Default "")
$testOutputText = [string](Get-PropertyOrDefault -Value $edit -Name "test_output" -Default "")
$testCommandJson = $effectiveTestCommand | ConvertTo-Json -Compress
$editFailure = [string](Get-PropertyOrDefault -Value $edit -Name "memory_failure" -Default "")
$failureText = if (-not [string]::IsNullOrWhiteSpace($failure)) {
    [string]$failure
} elseif (-not [string]::IsNullOrWhiteSpace($editFailure)) {
    $editFailure
} else {
    ""
}
$restoreError = [string](Get-PropertyOrDefault -Value $restore -Name "error" -Default "")
$verifierReason = [string](Get-PropertyOrDefault -Value $verifier -Name "reason" -Default "")
$verifierRisks = @(Get-PropertyOrDefault -Value $verifier -Name "risks" -Default @())
$verifierRisksJson = $verifierRisks | ConvertTo-Json -Compress
$finalization = [ordered]@{
    schema = "coding-intelligence-run-finalization/v1"
    status = $resultStatus
    process_exit = $resultExit
    command_exit = $exitCode
    model_calls = $implementationCalls + $verifierCalls
    automatic_retries = 0
    failure_sha256 = if ([string]::IsNullOrWhiteSpace($failureText)) {
        $null
    } else {
        Get-TextSha256 -Text $failureText
    }
    implementation = [ordered]@{
        status = [string](Get-PropertyOrDefault -Value $edit -Name "status" -Default "failed")
        stage_locator = $stageLocator
        trajectory_locator = $trajectoryLocator
        trajectory_sha256 = [string](Get-PropertyOrDefault `
            -Value $edit -Name "trajectory_sha256" -Default (Get-FileSha256OrEmpty $trajectoryLocator))
        request_sha256 = [string](Get-PropertyOrDefault -Value $edit -Name "request_sha256" -Default $emptyHash)
        prompt_sha256 = [string](Get-PropertyOrDefault -Value $edit -Name "prompt_sha256" -Default $emptyHash)
        response_sha256 = [string](Get-PropertyOrDefault -Value $edit -Name "response_sha256" -Default $emptyHash)
        response_observed = [bool](Get-PropertyOrDefault -Value $edit -Name "response_observed" -Default $false)
        response_bytes = [int](Get-PropertyOrDefault -Value $edit -Name "response_bytes" -Default 0)
        http_status = [int](Get-PropertyOrDefault -Value $edit -Name "http_status" -Default 0)
        inference_seconds = [double](Get-PropertyOrDefault -Value $edit -Name "inference_seconds" -Default 0)
        files = @(Get-PropertyOrDefault -Value $edit -Name "edited_files" -Default @())
        diff_sha256 = [string](Get-PropertyOrDefault -Value $edit -Name "diff_sha256" -Default (Get-TextSha256 $diffText))
        test_command_sha256 = [string](Get-PropertyOrDefault -Value $edit -Name "test_command_sha256" -Default (Get-TextSha256 $testCommandJson))
        test_exit = [int](Get-PropertyOrDefault -Value $edit -Name "test_exit" -Default -1)
        test_output_sha256 = [string](Get-PropertyOrDefault -Value $edit -Name "test_output_sha256" -Default (Get-TextSha256 $testOutputText))
    }
    verifier = [ordered]@{
        status = [string](Get-PropertyOrDefault -Value $verifier -Name "status" -Default "not_run")
        model = [string](Get-PropertyOrDefault -Value $verifier -Name "model" -Default "devstral-small-2:24b")
        verdict = Get-PropertyOrDefault -Value $verifier -Name "verdict" -Default $null
        reason_sha256 = if ([string]::IsNullOrWhiteSpace($verifierReason)) {
            $null
        } else {
            Get-TextSha256 -Text $verifierReason
        }
        risks_sha256 = Get-TextSha256 -Text $verifierRisksJson
        model_calls = [int](Get-PropertyOrDefault -Value $verifier -Name "modelCalls" -Default $verifierCalls)
    }
    restore = [ordered]@{
        status = [string](Get-PropertyOrDefault -Value $restore -Name "status" -Default "failed")
        backend = [string](Get-PropertyOrDefault -Value $restore -Name "backend" -Default "Qwen38")
        error_sha256 = if ([string]::IsNullOrWhiteSpace($restoreError)) {
            $null
        } else {
            Get-TextSha256 -Text $restoreError
        }
    }
}
$memoryFinal = $null
$finalizationPath = Join-Path ([IO.Path]::GetTempPath()) ("coding-memory-" + [guid]::NewGuid() + ".json")
try {
    [IO.File]::WriteAllText(
        $finalizationPath,
        ($finalization | ConvertTo-Json -Depth 12 -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    $priorErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $memoryFinalOutput = @(& $memoryContinuity `
            "run-memory-finalize" `
            "--root" $operationalMemoryRoot `
            "--task-id" $runTask `
            "--session-id" $runSession `
            "--workspace" $workspace `
            "--objective" ([string]$plan.objective) `
            "--model" $model `
            "--input" $finalizationPath 2>&1)
        $memoryFinalExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $priorErrorAction
    }
    if ($memoryFinalExit -ne 0 -or -not $memoryFinalOutput.Count) {
        throw "terminal memory finalization failed: $($memoryFinalOutput -join "`n")"
    }
    $memoryFinal = $memoryFinalOutput[-1] | ConvertFrom-Json
} catch {
    $memoryFinal = [ordered]@{
        status = "failed"
        root = $operationalMemoryRoot
        task_id = $runTask
        session_id = $runSession
        error = $_.Exception.Message
    }
    $resultStatus = "failed"
    $resultExit = 1
    if ($null -eq $failure) {
        $failure = $_.Exception.Message
    }
} finally {
    Remove-Item -LiteralPath $finalizationPath -Force -ErrorAction SilentlyContinue
}

[pscustomobject]@{
    status = $resultStatus
    task = $taskPath
    taskId = $runTask
    sessionId = $runSession
    backend = [string]$plan.backend
    model = $model
    switch = $swap
    edit = $edit
    verifierSwitch = $verifierSwap
    verifier = $verifier
    executionVerifier = if ($executionVerifier) { $executionVerifier } else { $null }
    macVerifier = $macExecutionVerifierReport
    sourceSnapshot = $macSourceSnapshot
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
    memory = $memoryFinal
} | ConvertTo-Json -Depth 12 -Compress

exit $resultExit
