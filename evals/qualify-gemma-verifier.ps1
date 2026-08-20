Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$model = "hf.co/google/gemma-4-31B-it-qat-q4_0-gguf:latest"
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
        raw = $text
        seconds = [math]::Round($started.Elapsed.TotalSeconds, 3)
        modelCalls = 1
        automaticRetries = 0
    }
}

$acceptPrompt = @"
Objective: make task-scope paths portable across Windows and macOS while rejecting drive-qualified, UNC, rooted, and traversal forms before filesystem access.

Proposed patch:
- strip surrounding whitespace;
- reject strings matching a Windows drive prefix;
- reject UNC and rooted slash/backslash prefixes;
- replace remaining backslashes with forward slashes;
- parse with PurePosixPath and reject any '..' component.

Independent hidden tests passed for safe relative Windows paths plus drive, UNC, rooted, and mixed-separator traversal cases. Decide whether this patch satisfies the objective.
"@

$rejectPrompt = @"
Objective: make task-scope paths portable across Windows and macOS while rejecting drive-qualified, UNC, rooted, and traversal forms before filesystem access.

Proposed patch:
- replace backslashes with forward slashes;
- keep the existing PurePosixPath absolute and '..' checks;
- do not add any Windows drive-prefix or UNC-specific validation.

The patch has not passed the Windows drive and UNC hidden tests. Decide whether this patch satisfies the objective.
"@

$results = @(
    Invoke-Verdict -Case "known-good" -Prompt $acceptPrompt -Expected "accept"
    Invoke-Verdict -Case "known-incomplete" -Prompt $rejectPrompt -Expected "reject"
)
$report = [ordered]@{
    status = if (@($results | Where-Object { -not $_.passed }).Count -eq 0) { "qualified" } else { "failed" }
    model = $model
    results = $results
    capturedAtUtc = [DateTime]::UtcNow.ToString("o")
}
$output = Join-Path (Split-Path -Parent $PSScriptRoot) "runs\gemma4-verifier.json"
[IO.Directory]::CreateDirectory((Split-Path -Parent $output)) | Out-Null
[IO.File]::WriteAllText($output, ($report | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
$report | ConvertTo-Json -Depth 12
if ($report.status -ne "qualified") { exit 1 }
