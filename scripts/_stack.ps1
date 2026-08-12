<#
.SYNOPSIS
    Shared process-supervision helpers for start.ps1 and stop.ps1.

.DESCRIPTION
    STACOS in development is six or seven long-running processes, not one. This
    file holds the parts that start.ps1 and stop.ps1 must agree on exactly: where
    state lives, what the services are, and how a process is proved to be alive.

    State is one JSON file per service under `.run/`, holding the PID *and* the
    process start time. The start time is what makes `stop` safe: Windows reuses
    PIDs aggressively, and killing whatever happens to hold PID 8124 today
    because it was a Celery worker yesterday is a genuinely bad afternoon.
#>

$script:StackRoot = Split-Path $PSScriptRoot -Parent
$script:RunDir    = Join-Path $script:StackRoot '.run'
$script:LogDir    = Join-Path $script:RunDir 'logs'

function Write-Step { param([string]$Message) Write-Host "`n=== $Message ===" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "  [ok]   $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "  [warn] $Message" -ForegroundColor Yellow }
function Write-Info { param([string]$Message) Write-Host "  [..]   $Message" -ForegroundColor Gray }
function Write-Err  { param([string]$Message) Write-Host "  [fail] $Message" -ForegroundColor Red }

function Initialize-StackDirs {
    if (-not (Test-Path $script:RunDir)) { New-Item -ItemType Directory -Path $script:RunDir | Out-Null }
    if (-not (Test-Path $script:LogDir)) { New-Item -ItemType Directory -Path $script:LogDir | Out-Null }
}

# ---------------------------------------------------------------------------
# .env -> process environment. Child processes inherit it, which is the whole
# mechanism by which the workers get DATABASE_URL and friends.
# ---------------------------------------------------------------------------
function Import-DotEnv {
    Push-Location $script:StackRoot
    try {
        if (-not (Test-Path '.env')) {
            Write-Warn 'No .env found; copying .env.example'
            Copy-Item '.env.example' '.env'
        }
        Get-Content '.env' | ForEach-Object {
            if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
                $name  = $Matches[1]
                $value = $Matches[2].Trim().Trim('"').Trim("'")
                Set-Item -Path "env:$name" -Value $value
            }
        }
    } finally {
        Pop-Location
    }
}

# ---------------------------------------------------------------------------
# The service catalog.
#
# Queue concurrencies mirror docker-compose.yml, which is the production
# topology. `-SplitWorkers` reproduces one-worker-per-queue locally; the default
# single worker is friendlier on a laptop and is what tasks.ps1 has always run.
# ---------------------------------------------------------------------------
$script:Queues = @(
    @{ Name = 'default';     Concurrency = 4 }
    @{ Name = 'reminders';   Concurrency = 4 }
    @{ Name = 'materialise'; Concurrency = 2 }
    @{ Name = 'portal';      Concurrency = 1 }
    @{ Name = 'ocr';         Concurrency = 2 }
    @{ Name = 'returns';     Concurrency = 2 }
    @{ Name = 'billing';     Concurrency = 1 }
    @{ Name = 'exports';     Concurrency = 2 }
)

function New-StackService {
    param(
        [string]$Name,
        [string]$Description,
        [string]$Exe,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $script:StackRoot,
        [int]$Port = 0,
        [bool]$Default = $true
    )
    [pscustomobject]@{
        Name        = $Name
        Description = $Description
        Exe         = $Exe
        Arguments   = $Arguments
        Cwd         = $WorkingDirectory
        Port        = $Port
        Default     = $Default
    }
}

function Get-StackServices {
    param([switch]$SplitWorkers)

    $services = @(
        New-StackService -Name 'web' -Description 'Django dev server' -Port 8000 `
            -Exe 'uv' -Arguments @('run', 'python', 'manage.py', 'runserver', '0.0.0.0:8000')

        New-StackService -Name 'sse' -Description 'ASGI server for Server-Sent Events' -Port 8001 `
            -Exe 'uv' -Arguments @('run', 'uvicorn', 'config.asgi:application', '--host', '0.0.0.0', '--port', '8001')
    )

    if ($SplitWorkers) {
        # Windows has no prefork pool; --pool=solo is the only reliable option,
        # so a split worker set here is eight single-task processes.
        foreach ($q in $script:Queues) {
            $services += New-StackService -Name "worker-$($q.Name)" -Description "Celery worker: $($q.Name)" `
                -Exe 'uv' -Arguments @('run', 'celery', '-A', 'config', 'worker', '--pool=solo', '-l', 'info', '-Q', $q.Name)
        }
    } else {
        $all = ($script:Queues | ForEach-Object { $_.Name }) -join ','
        $services += New-StackService -Name 'worker' -Description 'Celery worker (all queues, --pool=solo)' `
            -Exe 'uv' -Arguments @('run', 'celery', '-A', 'config', 'worker', '--pool=solo', '-l', 'info', '-Q', $all)
    }

    $services += New-StackService -Name 'beat' -Description 'Celery beat scheduler' `
        -Exe 'uv' -Arguments @('run', 'celery', '-A', 'config', 'beat', '-l', 'info',
                               '--scheduler', 'django_celery_beat.schedulers:DatabaseScheduler')

    $services += New-StackService -Name 'flower' -Description 'Celery inspector' -Port 5555 `
        -Exe 'uv' -Arguments @('run', 'celery', '-A', 'config', 'flower', '--port=5555')

    $services += New-StackService -Name 'assets' -Description 'Sass + esbuild watchers' `
        -Exe 'npm' -Arguments @('run', 'dev')

    # Opt-in: mobile/node_modules is a separate install that most work does not need.
    $services += New-StackService -Name 'mobile' -Description 'Framework7 mobile dev server' -Port 3002 -Default $false `
        -Exe 'npm' -Arguments @('run', 'dev') -WorkingDirectory (Join-Path $script:StackRoot 'mobile')

    return $services
}

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
<#
Resolves a command name to something Start-Process can actually launch.

Start-Process does not apply PATHEXT the way the shell does: `npm` resolves to
the extensionless POSIX shell script that ships alongside npm.cmd, and Windows
rejects it with "%1 is not a valid Win32 application". Returns the executable
and (possibly rewritten) argument list.
#>
function Resolve-Executable {
    param([string]$Exe, [string[]]$Arguments)

    $found = Get-Command $Exe -ErrorAction SilentlyContinue
    if ($found -and $found.CommandType -eq 'Application' -and
        ([IO.Path]::GetExtension($found.Source) -in '.exe', '.com')) {
        return [pscustomobject]@{ Exe = $found.Source; Arguments = $Arguments }
    }

    foreach ($ext in '.cmd', '.bat', '.exe') {
        $shim = Get-Command "$Exe$ext" -ErrorAction SilentlyContinue
        if ($shim) {
            if ($ext -eq '.exe') {
                return [pscustomobject]@{ Exe = $shim.Source; Arguments = $Arguments }
            }
            # A batch shim has to be run by cmd.exe. /c so the console exits with it.
            return [pscustomobject]@{ Exe = 'cmd.exe'; Arguments = @('/c', $shim.Source) + $Arguments }
        }
    }

    return [pscustomobject]@{ Exe = $Exe; Arguments = $Arguments }
}

function Get-StateFile { param([string]$Name) Join-Path $script:RunDir "$Name.json" }
function Get-LogFile   { param([string]$Name) Join-Path $script:LogDir "$Name.log" }
function Get-ErrFile   { param([string]$Name) Join-Path $script:LogDir "$Name.err.log" }

function Save-ServiceState {
    param([string]$Name, [System.Diagnostics.Process]$Process, [string]$CommandLine)
    Initialize-StackDirs
    [pscustomobject]@{
        name      = $Name
        pid       = $Process.Id
        startTime = $Process.StartTime.ToString('o')
        command   = $CommandLine
        log       = (Get-LogFile $Name)
    } | ConvertTo-Json | Set-Content -Path (Get-StateFile $Name) -Encoding utf8
}

function Get-ServiceState {
    param([string]$Name)
    $file = Get-StateFile $Name
    if (-not (Test-Path $file)) { return $null }
    try { return (Get-Content $file -Raw | ConvertFrom-Json) } catch { return $null }
}

function Remove-ServiceState {
    param([string]$Name)
    $file = Get-StateFile $Name
    if (Test-Path $file) { Remove-Item $file -Force }
}

<#
Returns the live Process for a recorded service, or $null.

The start-time comparison is the PID-reuse guard: a recycled PID belongs to a
process that started later than the one we recorded, so it fails the match and
we treat the service as dead rather than killing an innocent bystander.
#>
function Get-LiveServiceProcess {
    param([string]$Name)
    $state = Get-ServiceState $Name
    if (-not $state) { return $null }
    try { $proc = Get-Process -Id $state.pid -ErrorAction Stop } catch { return $null }
    if ($state.PSObject.Properties.Name -contains 'startTime' -and $state.startTime) {
        try {
            if ($proc.StartTime.ToString('o') -ne $state.startTime) { return $null }
        } catch {
            return $null
        }
    }
    return $proc
}

function Test-ServiceRunning {
    param([string]$Name)
    return $null -ne (Get-LiveServiceProcess -Name $Name)
}

function Get-TrackedServiceNames {
    if (-not (Test-Path $script:RunDir)) { return @() }
    return @(Get-ChildItem -Path $script:RunDir -Filter '*.json' -File | ForEach-Object { $_.BaseName })
}

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------
function Test-PortListening {
    param([int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(300)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-Port {
    param([int]$Port, [int]$TimeoutSeconds = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortListening -Port $Port) { return $true }
        Start-Sleep -Milliseconds 400
    }
    return $false
}

# ---------------------------------------------------------------------------
# Stopping
#
# taskkill /T is used rather than Stop-Process because every service here is
# `uv run <something>`: the process we launched is a launcher, and the Python
# process doing the actual work is its child. Killing only the parent orphans
# the worker and leaves port 8000 held by a ghost.
# ---------------------------------------------------------------------------
<#
Runs taskkill through cmd.exe, with the redirection done by cmd rather than by
PowerShell. Windows PowerShell turns a native command's stderr into an
ErrorRecord when it is redirected, which under $ErrorActionPreference = 'Stop'
makes a routine "could not be terminated" message abort the whole script.

Returns taskkill's exit code: 0 means Windows accepted the request.
#>
function Invoke-TaskKill {
    param([int]$ProcessId, [switch]$Force, [switch]$Tree)
    $flags = ''
    if ($Tree)  { $flags = '/T' }
    if ($Force) { $flags = "$flags /F" }
    & cmd.exe /c "taskkill /PID $ProcessId $flags >nul 2>&1"
    return $LASTEXITCODE
}

<#
Every descendant of a PID, deepest first, with the root last.

taskkill /T is not enough on its own. `manage.py runserver` is three processes
deep (uv -> autoreloader -> the process actually holding port 8000), and /T
resolves the tree as it goes: kill an intermediate parent and its children are
orphaned before taskkill reaches them, leaving the port held by a process with
no recorded PID. Snapshotting the tree first, then killing leaves upwards, also
means Django's reloader sees its child die abnormally and gives up rather than
respawning it.
#>
function Get-ProcessTreeIds {
    param([int]$RootId)

    $byParent = @{}
    foreach ($p in Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId -ErrorAction SilentlyContinue) {
        $parent = [int]$p.ParentProcessId
        if (-not $byParent.ContainsKey($parent)) { $byParent[$parent] = @() }
        $byParent[$parent] += [int]$p.ProcessId
    }

    $ordered = @()
    $frontier = @($RootId)
    while ($frontier) {
        $ordered = @($frontier) + $ordered   # prepend: deeper generations end up first
        $next = @()
        foreach ($id in $frontier) {
            if ($byParent.ContainsKey($id)) { $next += $byParent[$id] }
        }
        $frontier = $next | Where-Object { $ordered -notcontains $_ }   # cycle guard
    }
    return $ordered
}

function Stop-StackService {
    param(
        [string]$Name,
        [int]$GraceSeconds = 8,
        [switch]$Force
    )

    $proc = Get-LiveServiceProcess -Name $Name
    if (-not $proc) {
        if (Get-ServiceState $Name) {
            Remove-ServiceState $Name
            Write-Info "$Name was not running (stale state cleaned up)"
            return 'stale'
        }
        Write-Info "$Name is not running"
        return 'absent'
    }

    # Snapshot the tree before anything dies; see Get-ProcessTreeIds.
    $treeIds = Get-ProcessTreeIds -RootId $proc.Id

    if (-not $Force) {
        # A hidden console process usually refuses the polite request, and
        # taskkill says so immediately. Waiting out the full grace period in that
        # case just makes `stop` slow, so only wait when it was accepted.
        if ((Invoke-TaskKill -ProcessId $proc.Id -Tree) -eq 0) {
            $deadline = (Get-Date).AddSeconds($GraceSeconds)
            while ((Get-Date) -lt $deadline) {
                if (-not (Get-LiveServiceProcess -Name $Name)) {
                    Remove-ServiceState $Name
                    Write-Ok "$Name stopped (pid $($proc.Id))"
                    return 'stopped'
                }
                Start-Sleep -Milliseconds 300
            }
        }
    }

    foreach ($id in $treeIds) { Invoke-TaskKill -ProcessId $id -Force -Tree | Out-Null }
    # Anything spawned between the snapshot and now.
    Invoke-TaskKill -ProcessId $proc.Id -Force -Tree | Out-Null
    Start-Sleep -Milliseconds 400
    if (Get-LiveServiceProcess -Name $Name) {
        Write-Err "$Name (pid $($proc.Id)) would not die; kill it by hand"
        return 'failed'
    }
    Remove-ServiceState $Name
    Write-Ok "$Name stopped (pid $($proc.Id), forced)"
    return 'killed'
}
