param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Qwen38", "Ollama")]
    [string]$Backend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$qwenTask = "Coding Intelligence Excalibur Qwen3.8"
$ollamaTask = "AnimeFrontier Excalibur Ollama"

function Get-TaskState([string]$Name) {
    return [string](Get-ScheduledTask -TaskName $Name -ErrorAction Stop).State
}

function Wait-BackendStopped([string]$TaskName, [int]$Port) {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 250
        $state = Get-TaskState $TaskName
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    } while (($state -eq "Running" -or $listeners.Count) -and (Get-Date) -lt $deadline)
    if ($state -eq "Running" -or $listeners.Count) {
        throw "$TaskName did not stop within 30 seconds."
    }
}

function Wait-QwenReady {
    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 500
        $state = Get-TaskState $qwenTask
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8818 -ErrorAction SilentlyContinue)
        $ready = $false
        if ($state -eq "Running" -and $listeners.Count -eq 1) {
            try {
                $ready = [string](Invoke-RestMethod -Uri "http://127.0.0.1:8818/health" -TimeoutSec 3).status -eq "ok"
            } catch {
                $ready = $false
            }
        }
    } while (-not $ready -and (Get-Date) -lt $deadline)
    if (-not $ready) {
        throw "Qwen3.8 did not become ready within 60 seconds."
    }
}

function Wait-OllamaReady {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $state = Get-TaskState $ollamaTask
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue)
        $ready = $false
        if ($state -eq "Running" -and $listeners.Count -eq 1) {
            try {
                $ready = -not [string]::IsNullOrWhiteSpace(
                    [string](Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 3).version
                )
            } catch {
                $ready = $false
            }
        }
    } while (-not $ready -and (Get-Date) -lt $deadline)
    if (-not $ready) {
        throw "Ollama did not become ready within 30 seconds."
    }
}

$started = [Diagnostics.Stopwatch]::StartNew()
$previousQwen = Get-TaskState $qwenTask
$previousOllama = Get-TaskState $ollamaTask

try {
    if ($Backend -eq "Qwen38") {
        if ((Get-TaskState $ollamaTask) -eq "Running") {
            Stop-ScheduledTask -TaskName $ollamaTask
            Wait-BackendStopped $ollamaTask 11434
        }
        if ((Get-TaskState $qwenTask) -ne "Running") {
            Start-ScheduledTask -TaskName $qwenTask
        }
        Wait-QwenReady
        $port = 8818
    } else {
        if ((Get-TaskState $qwenTask) -eq "Running") {
            Stop-ScheduledTask -TaskName $qwenTask
            Wait-BackendStopped $qwenTask 8818
        }
        if ((Get-TaskState $ollamaTask) -ne "Running") {
            Start-ScheduledTask -TaskName $ollamaTask
        }
        Wait-OllamaReady
        $port = 11434
    }
} catch {
    if ($previousQwen -eq "Running" -and (Get-TaskState $qwenTask) -ne "Running") {
        Start-ScheduledTask -TaskName $qwenTask -ErrorAction SilentlyContinue
    } elseif ($previousOllama -eq "Running" -and (Get-TaskState $ollamaTask) -ne "Running") {
        Start-ScheduledTask -TaskName $ollamaTask -ErrorAction SilentlyContinue
    }
    throw
}

$started.Stop()
[pscustomobject]@{
    status = "ready"
    backend = $Backend
    port = $port
    swapSeconds = [math]::Round($started.Elapsed.TotalSeconds, 3)
    qwenTaskState = Get-TaskState $qwenTask
    ollamaTaskState = Get-TaskState $ollamaTask
    uacPrompt = $false
} | ConvertTo-Json -Compress
