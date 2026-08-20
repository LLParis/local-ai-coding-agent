Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\..\src\Test-OwnedProcess.ps1"

$expected = [pscustomobject]@{
    ProcessId = 42
    ParentProcessId = 7
    StartTimeUtc = "2026-08-20T10:00:00.0000000Z"
    ExecutablePath = "C:\\Tools\\worker.exe"
    CommandLine = '"C:\\Tools\\worker.exe" serve'
}
$matching = [pscustomobject]@{
    ProcessId = 42
    ParentProcessId = 7
    StartTimeUtc = "2026-08-20T10:00:00.0000000Z"
    ExecutablePath = "C:\\Tools\\worker.exe"
    CommandLine = '"C:\\Tools\\worker.exe" serve'
}
$reusedPid = [pscustomobject]@{
    ProcessId = 42
    ParentProcessId = 99
    StartTimeUtc = "2026-08-20T11:00:00.0000000Z"
    ExecutablePath = "C:\\Tools\\worker.exe"
    CommandLine = '"C:\\Tools\\worker.exe" serve'
}

if (-not (Test-OwnedProcess -Expected $expected -Observed $matching)) {
    throw "An exact owner identity was rejected."
}
if (Test-OwnedProcess -Expected $expected -Observed $reusedPid) {
    throw "A reused PID/start/parent identity was accepted."
}
Write-Output "PASS exact process ownership"
