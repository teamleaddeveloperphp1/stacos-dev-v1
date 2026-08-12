<#
.SYNOPSIS
    Stops the STACOS development stack started by start.ps1.

.DESCRIPTION
    Reads `.run/<name>.json`, verifies each recorded PID still belongs to the
    process we started (PID *and* start time must match), then terminates it
    together with its children -- every service is `uv run <something>`, so the
    process doing the work is a child of the one we launched.

    Stale entries, from a reboot or a manually killed process, are cleaned up
    silently.

.PARAMETER Only
    Stop just these services, e.g. -Only worker,beat.

.PARAMETER Force
    Skip the graceful request and kill immediately.

.PARAMETER Grace
    Seconds to wait for a graceful exit before forcing. Default 8.

.PARAMETER Ports
    Also report anything still holding 8000/8001/5555/3002 afterwards. Use this
    when a previous run was killed by hand and left something behind.

.PARAMETER Infra
    Also stop the PostgreSQL and Memurai Windows services. Off by default: they
    are shared machine services, not part of this project's process tree.

.EXAMPLE
    .\stop.ps1
    .\stop.ps1 -Only worker,beat
    .\stop.ps1 -Force -Ports
#>
[CmdletBinding()]
param(
    [string[]]$Only,
    [switch]$Force,
    [int]$Grace = 8,
    [switch]$Ports,
    [switch]$Infra
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
. (Join-Path $PSScriptRoot 'scripts\_stack.ps1')

Write-Step 'Stopping STACOS stack'

$script:StopFailures = 0
$names = Get-TrackedServiceNames
if ($Only) {
    $unknown = $Only | Where-Object { $names -notcontains $_ }
    foreach ($u in $unknown) { Write-Info "$u is not tracked as running" }
    $names = $Only | Where-Object { $names -contains $_ }
}

if (-not $names) {
    Write-Info 'Nothing to stop.'
} else {
    # Reverse order: producers before the things they depend on, so beat is not
    # queueing work into a broker whose workers have already gone.
    $order = @('mobile', 'assets', 'flower', 'beat')
    $sorted = @($names | Where-Object { $order -contains $_ } | Sort-Object { $order.IndexOf($_) })
    $sorted += @($names | Where-Object { $order -notcontains $_ } | Sort-Object)

    foreach ($name in $sorted) {
        $result = Stop-StackService -Name $name -GraceSeconds $Grace -Force:$Force
        if ($result -eq 'failed') { $script:StopFailures++ }
    }
}

# ---------------------------------------------------------------------------
# Leftovers
# ---------------------------------------------------------------------------
if ($Ports) {
    Write-Step 'Port check'
    foreach ($port in 8000, 8001, 5555, 3002) {
        if (-not (Test-PortListening -Port $port)) {
            Write-Ok "$port free"
            continue
        }
        $owner = $null
        try {
            $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop | Select-Object -First 1
            $owner = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        } catch { }
        if ($owner) {
            Write-Warn "$port still held by $($owner.ProcessName) (pid $($owner.Id)) -- Stop-Process -Id $($owner.Id) -Force"
        } else {
            Write-Warn "$port still in use by an unidentified process"
        }
    }
}

if ($Infra) {
    Write-Step 'Infrastructure services'
    foreach ($pattern in 'Memurai*', 'postgresql*') {
        foreach ($winsvc in @(Get-Service -Name $pattern -ErrorAction SilentlyContinue)) {
            if ($winsvc.Status -eq 'Running') {
                try {
                    Stop-Service $winsvc.Name -Force
                    Write-Ok "$($winsvc.Name) stopped"
                } catch {
                    Write-Warn "Could not stop $($winsvc.Name) (try an elevated shell): $($_.Exception.Message)"
                }
            } else {
                Write-Info "$($winsvc.Name) already $($winsvc.Status)"
            }
        }
    }
}

Write-Host ''
Write-Host '  Logs from the stopped run are still in .run\logs\' -ForegroundColor Gray

# Explicit, so the exit code is ours and not whatever taskkill last reported
# (128 = "no such process", which is a normal outcome when killing a tree).
if ($script:StopFailures -gt 0) { exit 1 }
exit 0
