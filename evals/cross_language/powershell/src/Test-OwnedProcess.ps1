function Test-OwnedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Expected,
        [Parameter(Mandatory = $true)]
        [object]$Observed
    )

    # Ownership records expose these exact fields on both objects:
    # ProcessId, ParentProcessId, StartTimeUtc, ExecutablePath, CommandLine.
    return (
        [string]$Expected.ExecutablePath -eq [string]$Observed.ExecutablePath -and
        [string]$Expected.CommandLine -eq [string]$Observed.CommandLine
    )
}
