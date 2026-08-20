param(
    [ValidateSet("Bounded", "Native")]
    [string]$Profile = "Bounded",
    [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$profiles = @{
    Bounded = [ordered]@{
        taskName = "Coding Intelligence Excalibur Qwen3.8"
        otherQwenTaskName = "Coding Intelligence Excalibur Qwen3.8 Native"
        stateName = "Qwen38"
        profileId = "q6-text/medium/q8_0/32768/mtp3"
        modelAlias = "arm-qwen38-q6-text"
        contextTokens = 32768
        cacheType = "q8_0"
        reasoningBudget = 2048
        mtp = $true
        jobNamespace = "Qwen38"
        backendId = "qwen38-bounded"
    }
    Native = [ordered]@{
        taskName = "Coding Intelligence Excalibur Qwen3.8 Native"
        otherQwenTaskName = "Coding Intelligence Excalibur Qwen3.8"
        stateName = "Qwen38Native"
        profileId = "q6-text/medium/q4_0/262144/mtp-off"
        modelAlias = "arm-qwen38-q6-native-262k"
        contextTokens = 262144
        cacheType = "q4_0"
        reasoningBudget = 16384
        mtp = $false
        jobNamespace = "Qwen38Native"
        backendId = "qwen38-native"
    }
}
$selectedProfile = $profiles[$Profile]
$taskName = [string]$selectedProfile.taskName
$otherQwenTaskName = [string]$selectedProfile.otherQwenTaskName
$ollamaTaskName = "AnimeFrontier Excalibur Ollama"
$projectRoot = "D:\11_CS\00_REPOS\AI Research Mastery"
$runtimePath = Join-Path $projectRoot "artifacts\runtime\llama.cpp\b10435\bin\llama-server.exe"
$modelPath = Join-Path $projectRoot "models\Qwen3.8-27B-GGUF\Qwen3.8-27B-Q6_K.gguf"
$sourceManifestPath = Join-Path $projectRoot "research\source_manifest.json"
$runtimeManifestPath = Join-Path $projectRoot "research\runtime_manifest.json"
$expectedRuntimeSha256 = "9c554aac54df3b2ceaa68fa96401928d794f2ba87f345bf4c04af1f1310f8e89"
$expectedModelSha256 = "562fbf760503008f118e5df38de5b3e97992d1f693f475815631198547486727"
$expectedModelBytes = 22884408288
$stateRoot = Join-Path $env:LOCALAPPDATA ("CodingIntelligence\{0}" -f $selectedProfile.stateName)
$coordinationRoot = Split-Path -Parent $stateRoot
$stdoutPath = Join-Path $stateRoot "qwen38.stdout.log"
$stderrPath = Join-Path $stateRoot "qwen38.stderr.log"
$lifecyclePath = Join-Path $stateRoot "qwen38-lifecycle.log"
$ownerPath = Join-Path $stateRoot "qwen38-owner.json"
$lockPath = Join-Path $stateRoot "qwen38-wrapper.lock"
$backendLockPath = Join-Path $coordinationRoot "active-backend.lock"
$endpoint = "http://127.0.0.1:8818"
$localAddress = "127.0.0.1"
$localPort = 8818

$serverArguments = @(
    "--model", $modelPath,
    "--alias", ([string]$selectedProfile.modelAlias),
    "--ctx-size", ([string]$selectedProfile.contextTokens),
    "--parallel", "1",
    "--gpu-layers", "999",
    "--flash-attn", "on",
    "--cache-type-k", ([string]$selectedProfile.cacheType),
    "--cache-type-v", ([string]$selectedProfile.cacheType),
    "--fit", "off",
    "--jinja",
    "--reasoning-format", "deepseek",
    "--host", $localAddress,
    "--port", ([string]$localPort),
    "--cors-origins", "localhost",
    "--no-cors-credentials",
    "--no-ui",
    "--metrics",
    "--no-context-shift",
    "--reasoning", "on",
    "--reasoning-effort", "medium",
    "--reasoning-budget", ([string]$selectedProfile.reasoningBudget),
    "--reasoning-preserve"
)
if ([bool]$selectedProfile.mtp) {
    $serverArguments += @("--spec-type", "draft-mtp", "--spec-draft-n-max", "3")
} else {
    $serverArguments += @("--spec-type", "none")
}

function ConvertTo-SafeCommandLineArgument {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Value
    )

    if ($Value.IndexOf('"') -ge 0 -or $Value.IndexOf("`r") -ge 0 -or $Value.IndexOf("`n") -ge 0) {
        throw "A fixed llama-server argument contains an unsupported quote or newline."
    }
    if ($Value.Length -eq 0) {
        return '""'
    }
    if ($Value -match '\s') {
        return '"' + $Value + '"'
    }
    return $Value
}

function Get-ServerArgumentLine {
    return (@($serverArguments | ForEach-Object {
        ConvertTo-SafeCommandLineArgument -Value ([string]$_)
    }) -join " ")
}

function Assert-StaticConfiguration {
    if (-not (Test-Path -LiteralPath $runtimePath -PathType Leaf)) {
        throw "Pinned llama.cpp runtime is missing: $runtimePath"
    }
    if (-not (Test-Path -LiteralPath $modelPath -PathType Leaf)) {
        throw "Pinned Qwen3.8 Q6 model is missing: $modelPath"
    }
    if (-not (Test-Path -LiteralPath $sourceManifestPath -PathType Leaf)) {
        throw "Qwen source manifest is missing: $sourceManifestPath"
    }
    if (-not (Test-Path -LiteralPath $runtimeManifestPath -PathType Leaf)) {
        throw "llama.cpp runtime manifest is missing: $runtimeManifestPath"
    }

    $runtimeHash = (Get-FileHash -LiteralPath $runtimePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($runtimeHash -ne $expectedRuntimeSha256) {
        throw "The llama-server executable hash does not match the pinned runtime."
    }

    $modelFile = Get-Item -LiteralPath $modelPath
    if ([long]$modelFile.Length -ne $expectedModelBytes) {
        throw "The Qwen3.8 Q6 model size does not match the verified artifact."
    }

    $sourceManifest = Get-Content -LiteralPath $sourceManifestPath -Raw | ConvertFrom-Json
    $manifestModels = @(
        $sourceManifest.artifacts |
            ForEach-Object {
                if ($_.PSObject.Properties.Name -contains "files") {
                    @($_.files)
                }
            } |
            Where-Object { [string]$_.name -eq "Qwen3.8-27B-Q6_K.gguf" }
    )
    if ($manifestModels.Count -ne 1) {
        throw "The source manifest does not contain one exact Qwen3.8 Q6 artifact."
    }
    if (
        [string]$manifestModels[0].sha256 -ne $expectedModelSha256 -or
        [long]$manifestModels[0].bytes -ne $expectedModelBytes -or
        -not [bool]$manifestModels[0].local_verified
    ) {
        throw "The source manifest does not attest the pinned Qwen3.8 Q6 artifact."
    }

    $runtimeManifest = Get-Content -LiteralPath $runtimeManifestPath -Raw | ConvertFrom-Json
    if (
        [string]$runtimeManifest.runtime -ne "llama.cpp" -or
        [string]$runtimeManifest.release -ne "b10435" -or
        [string]$runtimeManifest.commit -ne "9e40df63b"
    ) {
        throw "The runtime manifest does not attest llama.cpp b10435 / 9e40df63b."
    }

    $argumentLine = Get-ServerArgumentLine
    if ([string]::IsNullOrWhiteSpace($argumentLine)) {
        throw "The fixed llama-server argument line is empty."
    }

    return [pscustomobject]@{
        runtimeSha256 = $runtimeHash
        modelBytes = [long]$modelFile.Length
        sourceManifestSha256 = (Get-FileHash -LiteralPath $sourceManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        runtimeManifestSha256 = (Get-FileHash -LiteralPath $runtimeManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        argumentLine = $argumentLine
    }
}

if (-not ("CodingIntelligence.Qwen38KillOnCloseJob" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace CodingIntelligence {
    public static class Qwen38KillOnCloseJob {
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
                throw new InvalidOperationException("The Qwen3.8 server was not assigned to the lifecycle job.");
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

$staticConfiguration = Assert-StaticConfiguration

if ($ValidateOnly) {
    $validationJobName = "Local\CodingIntelligence.Qwen38.Validation.$PID.$([Guid]::NewGuid().ToString('N'))"
    $validationHandle = [IntPtr]::Zero
    try {
        $validationHandle = [CodingIntelligence.Qwen38KillOnCloseJob]::Create($validationJobName)
    } finally {
        if ($validationHandle -ne [IntPtr]::Zero) {
            [CodingIntelligence.Qwen38KillOnCloseJob]::Release($validationHandle)
        }
    }
    [pscustomobject]@{
        status = "valid"
        profileName = $Profile
        profile = [string]$selectedProfile.profileId
        task = $taskName
        stateRoot = $stateRoot
        modelAlias = [string]$selectedProfile.modelAlias
        contextTokens = [int]$selectedProfile.contextTokens
        cacheType = [string]$selectedProfile.cacheType
        mtp = [bool]$selectedProfile.mtp
        reasoningEffort = "medium"
        reasoningBudget = [int]$selectedProfile.reasoningBudget
        endpoint = $endpoint
        runtimeSha256 = [string]$staticConfiguration.runtimeSha256
        modelSha256Expected = $expectedModelSha256
        modelBytesObserved = [long]$staticConfiguration.modelBytes
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
        # Lifecycle logging must never mask the server's real failure.
    }
}

function Get-PortListenerRows {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    try {
        return @(
            Get-NetTCPConnection -State Listen -ErrorAction Stop |
                Where-Object { [int]$_.LocalPort -eq $Port }
        )
    } catch {
        throw "Unable to inspect port $Port; listener discovery failed."
    }
}

function Assert-OtherBackendsInactive {
    $otherQwenTask = Get-ScheduledTask -TaskName $otherQwenTaskName -ErrorAction SilentlyContinue
    if ($null -ne $otherQwenTask -and [string]$otherQwenTask.State -eq "Running") {
        throw "$otherQwenTaskName is Running; refusing concurrent Qwen3.8 profiles."
    }
    $ollamaTask = Get-ScheduledTask -TaskName $ollamaTaskName -ErrorAction SilentlyContinue
    if ($null -ne $ollamaTask -and [string]$ollamaTask.State -eq "Running") {
        throw "Ollama Scheduled Task is Running; refusing concurrent GPU backends."
    }
    if (@(Get-PortListenerRows -Port 11434).Count -ne 0) {
        throw "Port 11434 is listening; refusing to load Qwen3.8 beside Ollama."
    }
}

function Get-ProcessIdentity {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    $native = Get-Process -Id $ProcessId -ErrorAction Stop
    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if (-not $cim) {
            throw "Process $ProcessId has no Win32_Process identity."
        }
        return [pscustomobject]@{
            processId = $ProcessId
            parentProcessId = [int]$cim.ParentProcessId
            executablePath = [System.IO.Path]::GetFullPath([string]$cim.ExecutablePath)
            commandLine = [string]$cim.CommandLine
            startTimeUtc = $native.StartTime.ToUniversalTime().ToString("o")
        }
    } finally {
        $native.Dispose()
    }
}

function Assert-ServerIdentity {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Identity,
        [Parameter(Mandatory = $true)]
        [string]$ArgumentLine
    )

    $expectedPath = [System.IO.Path]::GetFullPath($runtimePath)
    if (-not [string]::Equals(
        [string]$Identity.executablePath,
        $expectedPath,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "The started server executable does not match the pinned llama.cpp runtime."
    }
    if ([int]$Identity.parentProcessId -ne $PID) {
        throw "The started Qwen3.8 server is not an exact child of this wrapper."
    }
    $commandPattern = '^\s*"?' + [regex]::Escape($expectedPath) + '"?\s+' + [regex]::Escape($ArgumentLine) + '\s*$'
    if ([string]$Identity.commandLine -notmatch $commandPattern) {
        throw "The started Qwen3.8 server command line does not match the fixed profile."
    }
}

$singletonLock = $null
$backendLock = $null
$jobHandle = [IntPtr]::Zero
$process = $null
$ownerCreated = $false
$instanceId = [Guid]::NewGuid().ToString("D")
$exitCode = 1
$failure = $null

try {
    try {
        $singletonLock = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch {
        throw "Another Qwen3.8 wrapper owns the singleton lock: $lockPath"
    }

    try {
        $backendLock = [System.IO.File]::Open(
            $backendLockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch {
        throw "Another managed inference backend owns the selection lock: $backendLockPath"
    }

    $lockBytes = [System.Text.Encoding]::UTF8.GetBytes(
        "backend=$($selectedProfile.backendId)`nwrapperPid=$PID`ninstanceId=$instanceId`n"
    )
    $backendLock.SetLength(0)
    $backendLock.Write($lockBytes, 0, $lockBytes.Length)
    $backendLock.Flush($true)

    Assert-OtherBackendsInactive
    if (@(Get-PortListenerRows -Port $localPort).Count -ne 0) {
        throw "Port $localPort already has a listener; refusing to overwrite its ownership record."
    }

    # A force-terminated Scheduled Task cannot run its finally block. Holding
    # both locks and observing an empty port make an old owner record stale.
    if (Test-Path -LiteralPath $ownerPath -PathType Leaf) {
        Remove-Item -LiteralPath $ownerPath -Force
    }

    $jobName = "Local\CodingIntelligence.$($selectedProfile.jobNamespace).$PID.$($instanceId.Replace('-', ''))"
    $jobHandle = [CodingIntelligence.Qwen38KillOnCloseJob]::Create($jobName)
    $startParameters = @{
        FilePath = $runtimePath
        ArgumentList = [string]$staticConfiguration.argumentLine
        WorkingDirectory = $projectRoot
        WindowStyle = "Hidden"
        RedirectStandardOutput = $stdoutPath
        RedirectStandardError = $stderrPath
        PassThru = $true
    }
    $process = Start-Process @startParameters

    try {
        [CodingIntelligence.Qwen38KillOnCloseJob]::AssignAndVerify($jobHandle, $process.Handle)
    } catch {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw
    }

    $serverIdentity = Get-ProcessIdentity -ProcessId $process.Id
    Assert-ServerIdentity -Identity $serverIdentity -ArgumentLine ([string]$staticConfiguration.argumentLine)

    $readyDeadline = [DateTime]::UtcNow.AddSeconds(120)
    $lastReadyError = $null
    $readyListener = $null
    do {
        if ($process.HasExited) {
            throw "Qwen3.8 exited before reaching readiness with code $($process.ExitCode)."
        }
        try {
            Assert-OtherBackendsInactive
            $listeners = @(Get-PortListenerRows -Port $localPort)
            if ($listeners.Count -ne 1) {
                throw "Expected one Qwen3.8 listener; found $($listeners.Count)."
            }
            if (
                [string]$listeners[0].LocalAddress -ne $localAddress -or
                [int]$listeners[0].OwningProcess -ne $process.Id
            ) {
                throw "The Qwen3.8 listener is not the exact child on IPv4 loopback."
            }
            $health = Invoke-RestMethod -Uri "$endpoint/health" -TimeoutSec 3
            if ([string]$health.status -ne "ok") {
                throw "Qwen3.8 health status is not ok."
            }
            $readyListener = $listeners[0]
        } catch {
            $lastReadyError = $_
            $readyListener = $null
            Start-Sleep -Milliseconds 250
        }
    } while ($null -eq $readyListener -and [DateTime]::UtcNow -lt $readyDeadline)

    if ($null -eq $readyListener) {
        throw "Qwen3.8 did not reach exact owned readiness within 120 seconds: $($lastReadyError.Exception.Message)"
    }

    $wrapperProcess = Get-Process -Id $PID -ErrorAction Stop
    try {
        $wrapperCim = Get-CimInstance Win32_Process -Filter "ProcessId = $PID" -ErrorAction Stop
        if (-not $wrapperCim) {
            throw "The Qwen3.8 wrapper has no Win32_Process identity."
        }
        $runtimeScriptHash = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
        $owner = [ordered]@{
            schemaVersion = 2
            instanceId = $instanceId
            taskName = $taskName
            endpoint = $endpoint
            localAddress = $localAddress
            localPort = $localPort
            profileName = $Profile
            profile = [string]$selectedProfile.profileId
            modelAlias = [string]$selectedProfile.modelAlias
            contextTokens = [int]$selectedProfile.contextTokens
            cacheType = [string]$selectedProfile.cacheType
            mtp = [bool]$selectedProfile.mtp
            reasoningEffort = "medium"
            reasoningBudget = [int]$selectedProfile.reasoningBudget
            modelPath = $modelPath
            modelBytes = [long]$staticConfiguration.modelBytes
            modelSha256Expected = $expectedModelSha256
            sourceManifestPath = $sourceManifestPath
            sourceManifestSha256 = [string]$staticConfiguration.sourceManifestSha256
            serverRuntimePath = $runtimePath
            serverRuntimeSha256 = [string]$staticConfiguration.runtimeSha256
            runtimeManifestPath = $runtimeManifestPath
            runtimeManifestSha256 = [string]$staticConfiguration.runtimeManifestSha256
            wrapperPid = $PID
            wrapperExecutablePath = [string]$wrapperCim.ExecutablePath
            wrapperCommandLine = [string]$wrapperCim.CommandLine
            wrapperStartTimeUtc = $wrapperProcess.StartTime.ToUniversalTime().ToString("o")
            serverPid = $process.Id
            serverParentPid = [int]$serverIdentity.parentProcessId
            serverExecutablePath = [string]$serverIdentity.executablePath
            serverCommandLine = [string]$serverIdentity.commandLine
            serverStartTimeUtc = [string]$serverIdentity.startTimeUtc
            runtimeScriptPath = $PSCommandPath
            runtimeScriptSha256 = $runtimeScriptHash
            jobObjectName = $jobName
            jobKillOnClose = $true
            jobAssignmentVerified = $true
            backendLockPath = $backendLockPath
            ollamaBackendAbsentAtReady = $true
            createdAtUtc = [DateTime]::UtcNow.ToString("o")
        }
    } finally {
        $wrapperProcess.Dispose()
    }

    $ownerTemp = "$ownerPath.$PID.$($instanceId.Replace('-', '')).tmp"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($ownerTemp, ($owner | ConvertTo-Json -Compress), $utf8NoBom)
    Move-Item -LiteralPath $ownerTemp -Destination $ownerPath -Force
    $ownerCreated = $true
    Write-LifecycleEvent -Event "started" -Message "Assigned server PID $($process.Id) to $jobName."

    while (-not $process.WaitForExit(1000)) {
        # The existing Ollama wrapper predates the shared backend lock. Until it
        # participates, fail closed if that endpoint is started while Qwen runs.
        Assert-OtherBackendsInactive
    }
    $exitCode = $process.ExitCode
    Write-LifecycleEvent -Event "server-exited" -Message "Server PID $($process.Id) exited with code $exitCode."
} catch {
    $failure = $_
    Write-LifecycleEvent -Event "failed" -Message $_.Exception.Message
} finally {
    if ($jobHandle -ne [IntPtr]::Zero) {
        try {
            [CodingIntelligence.Qwen38KillOnCloseJob]::Release($jobHandle)
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
    if ($null -ne $backendLock) {
        $backendLock.Dispose()
    }
    if ($null -ne $singletonLock) {
        $singletonLock.Dispose()
    }
}

if ($null -ne $failure) {
    throw $failure
}

exit $exitCode
