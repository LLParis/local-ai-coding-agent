param(
    [Parameter(Mandatory = $true)]
    [int]$ExpectedPid,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedStartTimeUtc
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$listener = Get-NetTCPConnection -State Listen -LocalPort 8818 -ErrorAction Stop
if (@($listener).Count -ne 1 -or [int]$listener.OwningProcess -ne $ExpectedPid) {
    throw "Port 8818 is not owned by the expected process."
}

$process = Get-CimInstance Win32_Process -Filter "ProcessId = $ExpectedPid" -ErrorAction Stop
if (-not $process -or [string]$process.Name -ne "llama-server.exe") {
    throw "The expected Qwen llama-server process is absent or has changed identity."
}
$actualStart = ([DateTime]$process.CreationDate).ToUniversalTime().ToString("o")
if ($actualStart -ne $ExpectedStartTimeUtc) {
    throw "The Qwen process start identity changed; refusing termination."
}

Stop-Process -Id $ExpectedPid -Force -ErrorAction Stop
$deadline = (Get-Date).AddSeconds(15)
do {
    Start-Sleep -Milliseconds 250
    $alive = Get-CimInstance Win32_Process -Filter "ProcessId = $ExpectedPid" -ErrorAction SilentlyContinue
    $remaining = @(Get-NetTCPConnection -State Listen -LocalPort 8818 -ErrorAction SilentlyContinue)
} while (($alive -or $remaining.Count) -and (Get-Date) -lt $deadline)

if ($alive -or $remaining.Count) {
    throw "The exact Qwen process or listener remained after bounded termination."
}

[pscustomobject]@{
    status = "stopped"
    pid = $ExpectedPid
    startTimeUtc = $ExpectedStartTimeUtc
    listenerCount = 0
} | ConvertTo-Json -Compress
