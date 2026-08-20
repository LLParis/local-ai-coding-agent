Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

& "$PSScriptRoot\..\windows\Switch-ExcaliburBackend.ps1" -Backend Ollama | Out-Null
$model = "devstral-small-2:24b"
$endpoint = "http://127.0.0.1:11434/v1/chat/completions"
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

function Invoke-Verdict([string]$Case, [string]$Prompt, [string]$Expected) {
    $body = @{
        model = $model
        messages = @(
            @{ role = "system"; content = "Act as an independent code verifier. Return only the requested JSON verdict." }
            @{ role = "user"; content = $Prompt }
        )
        max_tokens = 1024
        stream = $false
        temperature = 0
        response_format = @{
            type = "json_schema"
            json_schema = @{ name = "verifier"; strict = $true; schema = $schema }
        }
    } | ConvertTo-Json -Depth 12 -Compress
    $started = [Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-RestMethod -Method Post -Uri $endpoint -ContentType "application/json" -Body $body -TimeoutSec 180
    $started.Stop()
    $text = [string]$response.choices[0].message.content
    $parsed = if ([string]::IsNullOrWhiteSpace($text)) { $null } else { $text | ConvertFrom-Json }
    return [ordered]@{
        case = $Case
        expected = $Expected
        observed = if ($null -eq $parsed) { $null } else { [string]$parsed.verdict }
        passed = $null -ne $parsed -and [string]$parsed.verdict -eq $Expected
        response = $parsed
        seconds = [math]::Round($started.Elapsed.TotalSeconds, 3)
        modelCalls = 1
        automaticRetries = 0
    }
}

$goodPath = @"
Objective: canonicalize safe relative paths and reject Windows drive, UNC, rooted, and traversal forms.
Patch: reject drive prefixes and rooted/UNC prefixes, replace remaining backslashes with slashes, parse with PurePosixPath, reject '..'.
Hidden assertions for safe normalization and every forbidden form passed.
"@
$badPath = @"
Objective: canonicalize safe relative paths and reject Windows drive, UNC, rooted, and traversal forms.
Patch: only replace backslashes with slashes; add no drive-prefix or UNC checks.
Windows drive and UNC assertions fail.
"@
$typescript = @"
Objective: backend readiness requires task Running, IPv4 loopback, health ok, and listenerPid equal to ownerPid.
Patch: add `state.listenerPid === state.ownerPid` to the existing conjunction.
The hidden test asserts matching PIDs return true and mismatched PIDs return false. Both assertions passed.
"@

$results = @(
    Invoke-Verdict -Case "known-good-path" -Prompt $goodPath -Expected "accept"
    Invoke-Verdict -Case "known-bad-path" -Prompt $badPath -Expected "reject"
    Invoke-Verdict -Case "real-qwen-typescript" -Prompt $typescript -Expected "accept"
)
$report = [ordered]@{
    status = if (@($results | Where-Object { -not $_.passed }).Count -eq 0) { "qualified" } else { "failed" }
    model = $model
    results = $results
    capturedAtUtc = [DateTime]::UtcNow.ToString("o")
}
$output = "$PSScriptRoot\..\runs\devstral-verifier.json"
[IO.File]::WriteAllText($output, ($report | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
$report | ConvertTo-Json -Depth 12
if ($report.status -ne "qualified") { exit 1 }
