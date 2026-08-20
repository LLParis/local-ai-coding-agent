Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

& "$PSScriptRoot\..\windows\Switch-ExcaliburBackend.ps1" -Backend Ollama | Out-Null
$model = "hf.co/google/gemma-4-31B-it-qat-q4_0-gguf:latest"
$prompt = @"
Act as an independent verifier.

Objective: backend readiness must require the loopback listener PID to match the recorded owner PID while preserving task-state, loopback-address, and health checks.

Qwen proposed adding this condition to the existing conjunction:
    state.listenerPid === state.ownerPid

The retained TypeScript stage passed its hidden Node test for both a matching PID and a mismatched listener PID. Return an accept or reject verdict for this patch.
"@
$schema = @{
    type = "object"
    additionalProperties = $false
    properties = @{
        verdict = @{ type = "string"; enum = @("accept", "reject") }
        reason = @{ type = "string" }
        risks = @{ type = "array"; maxItems = 3; items = @{ type = "string" } }
    }
    required = @("verdict", "reason", "risks")
}
$body = @{
    model = $model
    messages = @(
        @{ role = "system"; content = "Return only the requested JSON verifier result." }
        @{ role = "user"; content = $prompt }
    )
    max_tokens = 768
    stream = $false
    temperature = 0
    response_format = @{
        type = "json_schema"
        json_schema = @{ name = "verifier"; strict = $true; schema = $schema }
    }
} | ConvertTo-Json -Depth 12 -Compress
$started = [Diagnostics.Stopwatch]::StartNew()
$response = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:11434/v1/chat/completions" -ContentType "application/json" -Body $body -TimeoutSec 180
$started.Stop()
$raw = [string]$response.choices[0].message.content
$result = if ([string]::IsNullOrWhiteSpace($raw)) { $null } else { $raw | ConvertFrom-Json }
$report = [ordered]@{
    status = if ($null -ne $result -and [string]$result.verdict -eq "accept") { "verified" } else { "failed" }
    implementer = "arm-qwen38-q6-text"
    verifier = $model
    language = "typescript"
    result = $result
    raw = $raw
    seconds = [math]::Round($started.Elapsed.TotalSeconds, 3)
    modelCalls = 1
    automaticRetries = 0
}
$output = "$PSScriptRoot\..\runs\gemma-verify-typescript.json"
[IO.File]::WriteAllText($output, ($report | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
$report | ConvertTo-Json -Depth 10
if ($report.status -ne "verified") { exit 1 }
