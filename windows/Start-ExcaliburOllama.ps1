param(
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ollamaPath = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
$stateRoot = Join-Path $env:LOCALAPPDATA "AnimeFrontier\AgentContinuity"
$stdoutPath = Join-Path $stateRoot "ollama.stdout.log"
$stderrPath = Join-Path $stateRoot "ollama.stderr.log"
$lifecyclePath = Join-Path $stateRoot "service-lifecycle.log"
$ownerPath = Join-Path $stateRoot "service-owner.json"
$lockPath = Join-Path $stateRoot "service-wrapper.lock"
$taskName = "AnimeFrontier Excalibur Ollama"
$endpoint = "http://127.0.0.1:11434"

if (-not (Test-Path -LiteralPath $ollamaPath -PathType Leaf)) {
    throw "Ollama is not installed at the expected per-user path."
}

if (-not ("AnimeFrontier.KillOnCloseJob" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace AnimeFrontier {
    public static class KillOnCloseJob {
        private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
        private const int JobObjectExtendedLimitInformation = 9;

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            int informationClass,
            IntPtr information,
            uint informationLength
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool result);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        public static IntPtr Create(string name) {
            IntPtr job = CreateJobObject(IntPtr.Zero, name);
            if (job == IntPtr.Zero) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");
            }

            var limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            int size = Marshal.SizeOf(limits);
            IntPtr pointer = Marshal.AllocHGlobal(size);
            try {
                Marshal.StructureToPtr(limits, pointer, false);
                if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, pointer, (uint)size)) {
                    int error = Marshal.GetLastWin32Error();
                    CloseHandle(job);
                    throw new Win32Exception(error, "SetInformationJobObject failed");
                }
            } finally {
                Marshal.FreeHGlobal(pointer);
            }
            return job;
        }

        public static void AssignAndVerify(IntPtr job, IntPtr process) {
            if (!AssignProcessToJobObject(job, process)) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject failed");
            }
            bool assigned;
            if (!IsProcessInJob(process, job, out assigned)) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "IsProcessInJob failed");
            }
            if (!assigned) {
                throw new InvalidOperationException("The Ollama process was not assigned to the lifecycle job.");
            }
        }

        public static void Release(IntPtr job) {
            if (job != IntPtr.Zero && !CloseHandle(job)) {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "CloseHandle failed");
            }
        }
    }
}
"@
}

if ($ValidateOnly) {
    $validationJobName = "Local\AnimeFrontier.ExcaliburOllama.Validation.$PID.$([Guid]::NewGuid().ToString('N'))"
    $validationHandle = [IntPtr]::Zero
    try {
        $validationHandle = [AnimeFrontier.KillOnCloseJob]::Create($validationJobName)
    } finally {
        if ($validationHandle -ne [IntPtr]::Zero) {
            [AnimeFrontier.KillOnCloseJob]::Release($validationHandle)
        }
    }
    [pscustomobject]@{
        status = "valid"
        jobObjectInterop = $true
        killOnCloseConfigured = $true
        script = $PSCommandPath
    } | ConvertTo-Json -Compress
    exit 0
}

New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null

function Write-LifecycleEvent {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Event,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    try {
        $record = [ordered]@{
            timestampUtc = [DateTime]::UtcNow.ToString("o")
            event = $Event
            wrapperPid = $PID
            message = $Message
        }
        Add-Content -LiteralPath $lifecyclePath -Value ($record | ConvertTo-Json -Compress) -Encoding UTF8
    } catch {
        # Lifecycle logging must never mask the service's real failure.
    }
}

# Keep the inference service private. The Mac reaches it only through an SSH
# tunnel, so neither the LAN nor the Tailnet receives a raw model endpoint.
$env:OLLAMA_HOST = "127.0.0.1:11434"
$env:OLLAMA_KEEP_ALIVE = "30m"
$env:OLLAMA_MAX_LOADED_MODELS = "1"
$env:OLLAMA_NUM_PARALLEL = "1"

$lockStream = $null
$jobHandle = [IntPtr]::Zero
$process = $null
$ownerCreated = $false
$instanceId = [Guid]::NewGuid().ToString("D")
$exitCode = 1
$failure = $null

try {
    try {
        $lockStream = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch {
        throw "Another Ollama service wrapper owns the singleton lock: $lockPath"
    }

    $lockBytes = [System.Text.Encoding]::UTF8.GetBytes("wrapperPid=$PID`ninstanceId=$instanceId`n")
    $lockStream.SetLength(0)
    $lockStream.Write($lockBytes, 0, $lockBytes.Length)
    $lockStream.Flush($true)

    try {
        $existingListeners = @(
            Get-NetTCPConnection -State Listen -ErrorAction Stop |
                Where-Object { [int]$_.LocalPort -eq 11434 }
        )
    } catch {
        throw "Unable to prove that port 11434 is free; listener discovery failed."
    }
    if ($existingListeners.Count -ne 0) {
        throw "Port 11434 already has a listener; refusing to overwrite its ownership record."
    }

    # A force-terminated Scheduled Task cannot run its finally block. Holding
    # the singleton lock and observing an empty port make the old record stale.
    if (Test-Path -LiteralPath $ownerPath -PathType Leaf) {
        Remove-Item -LiteralPath $ownerPath -Force
    }

    $jobName = "Local\AnimeFrontier.ExcaliburOllama.$PID.$($instanceId.Replace('-', ''))"
    $jobHandle = [AnimeFrontier.KillOnCloseJob]::Create($jobName)
    $startParameters = @{
        FilePath = $ollamaPath
        ArgumentList = "serve"
        WindowStyle = "Hidden"
        RedirectStandardOutput = $stdoutPath
        RedirectStandardError = $stderrPath
        PassThru = $true
    }
    $process = Start-Process @startParameters

    try {
        [AnimeFrontier.KillOnCloseJob]::AssignAndVerify($jobHandle, $process.Handle)
    } catch {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw
    }

    $process.Refresh()
    $serverCim = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)" -ErrorAction Stop
    if (-not $serverCim) {
        throw "Ollama exited before its process identity could be verified."
    }

    $observedPath = [System.IO.Path]::GetFullPath([string]$serverCim.ExecutablePath)
    $expectedPath = [System.IO.Path]::GetFullPath($ollamaPath)
    if (-not [string]::Equals($observedPath, $expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The started server executable does not match the configured Ollama path."
    }
    $commandPattern = '^\s*"?' + [regex]::Escape($expectedPath) + '"?\s+serve\s*$'
    if ([string]$serverCim.CommandLine -notmatch $commandPattern) {
        throw "The started Ollama process does not have the exact serve command line."
    }
    if ([int]$serverCim.ParentProcessId -ne $PID) {
        throw "The started Ollama process is not an exact child of this wrapper."
    }

    $wrapperProcess = Get-Process -Id $PID -ErrorAction Stop
    $wrapperCim = Get-CimInstance Win32_Process -Filter "ProcessId = $PID" -ErrorAction Stop
    $runtimeHash = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $owner = [ordered]@{
        schemaVersion = 2
        instanceId = $instanceId
        taskName = $taskName
        endpoint = $endpoint
        localAddress = "127.0.0.1"
        localPort = 11434
        wrapperPid = $PID
        wrapperExecutablePath = [string]$wrapperCim.ExecutablePath
        wrapperCommandLine = [string]$wrapperCim.CommandLine
        wrapperStartTimeUtc = $wrapperProcess.StartTime.ToUniversalTime().ToString("o")
        serverPid = $process.Id
        serverParentPid = [int]$serverCim.ParentProcessId
        serverExecutablePath = $observedPath
        serverCommandLine = [string]$serverCim.CommandLine
        serverStartTimeUtc = $process.StartTime.ToUniversalTime().ToString("o")
        runtimeScriptPath = $PSCommandPath
        runtimeScriptSha256 = $runtimeHash
        jobObjectName = $jobName
        jobKillOnClose = $true
        jobAssignmentVerified = $true
        createdAtUtc = [DateTime]::UtcNow.ToString("o")
    }
    $ownerTemp = "$ownerPath.$PID.$($instanceId.Replace('-', '')).tmp"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $ownerTemp,
        ($owner | ConvertTo-Json -Compress),
        $utf8NoBom
    )
    Move-Item -LiteralPath $ownerTemp -Destination $ownerPath -Force
    $ownerCreated = $true

    Write-LifecycleEvent -Event "started" -Message "Assigned server PID $($process.Id) to $jobName."
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    Write-LifecycleEvent -Event "server-exited" -Message "Server PID $($process.Id) exited with code $exitCode."
} catch {
    $failure = $_
    Write-LifecycleEvent -Event "failed" -Message $_.Exception.Message
} finally {
    if ($jobHandle -ne [IntPtr]::Zero) {
        try {
            [AnimeFrontier.KillOnCloseJob]::Release($jobHandle)
        } catch {
            if ($null -eq $failure) {
                $failure = $_
            }
            Write-LifecycleEvent -Event "job-release-failed" -Message $_.Exception.Message
        }
    }

    if ($ownerCreated -and (Test-Path -LiteralPath $ownerPath -PathType Leaf)) {
        try {
            $currentOwner = Get-Content -LiteralPath $ownerPath -Raw | ConvertFrom-Json
            if (
                [string]$currentOwner.instanceId -eq $instanceId -and
                [int]$currentOwner.wrapperPid -eq $PID
            ) {
                Remove-Item -LiteralPath $ownerPath -Force
            }
        } catch {
            Write-LifecycleEvent -Event "owner-cleanup-failed" -Message $_.Exception.Message
        }
    }

    if ($null -ne $process) {
        $process.Dispose()
    }
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}

if ($null -ne $failure) {
    throw $failure
}

exit $exitCode
