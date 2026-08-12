<#
.SYNOPSIS
    Starts the whole STACOS development stack in the background.

.DESCRIPTION
    Django, the SSE server, Celery worker, Celery beat, Flower and the asset
    watchers, in one command. Each runs detached with its output redirected to
    `.run/logs/<name>.log`, and its PID recorded in `.run/<name>.json` so that
    `stop.ps1` can shut exactly these processes down again.

    PostgreSQL and Memurai are Windows services and are NOT managed here -- the
    script checks they are up and starts them if they are installed but stopped.

.PARAMETER Only
    Start just these services. Names: web, sse, worker, beat, flower, assets, mobile.

.PARAMETER Skip
    Start the default set minus these.

.PARAMETER WithMobile
    Also start the Framework7 mobile dev server on :3002 (off by default).

.PARAMETER SplitWorkers
    One Celery worker per queue, matching the production topology in
    docker-compose.yml, instead of a single worker consuming all queues.

.PARAMETER Migrate
    Run migrations and sync_system_roles before starting anything.

.PARAMETER Build
    Build CSS/JS once before starting, so the first page load is not unstyled.

.PARAMETER Restart
    Stop anything already running under the same name, then start it again.

.PARAMETER Status
    Report what is running and exit.

.EXAMPLE
    .\start.ps1
    .\start.ps1 -Migrate -Build
    .\start.ps1 -Only web,worker
    .\start.ps1 -Skip flower,sse
    .\start.ps1 -Status
#>
[CmdletBinding()]
param(
    [string[]]$Only,
    [string[]]$Skip,
    [switch]$WithMobile,
    [switch]$SplitWorkers,
    [switch]$Migrate,
    [switch]$Build,
    [switch]$Restart,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
. (Join-Path $PSScriptRoot 'scripts\_stack.ps1')

$services = Get-StackServices -SplitWorkers:$SplitWorkers

# ---------------------------------------------------------------------------
# -Status: report and leave.
# ---------------------------------------------------------------------------
if ($Status) {
    Write-Step 'STACOS stack status'
    $tracked = Get-TrackedServiceNames
    if (-not $tracked) {
        Write-Info 'Nothing is running (no state in .run/).'
    }
    foreach ($name in $tracked) {
        $proc = Get-LiveServiceProcess -Name $name
        if ($proc) {
            $uptime = [int]((Get-Date) - $proc.StartTime).TotalMinutes
            Write-Ok "$name  pid $($proc.Id)  up ${uptime}m  log $(Get-LogFile $name)"
        } else {
            Write-Warn "$name  not running (stale state; run .\stop.ps1 to tidy up)"
        }
    }
    foreach ($svc in $services) {
        if ($svc.Port -gt 0 -and $tracked -notcontains $svc.Name -and (Test-PortListening -Port $svc.Port)) {
            Write-Warn "port $($svc.Port) is in use by something this script did not start ($($svc.Name))"
        }
    }
    return
}

# ---------------------------------------------------------------------------
# Which services
# ---------------------------------------------------------------------------
$selected = @()
if ($Only) {
    foreach ($name in $Only) {
        $match = $services | Where-Object { $_.Name -eq $name }
        if (-not $match) {
            throw "Unknown service '$name'. Known: $(($services | ForEach-Object { $_.Name }) -join ', ')"
        }
        $selected += $match
    }
} else {
    $selected = $services | Where-Object { $_.Default -or ($WithMobile -and $_.Name -eq 'mobile') }
    if ($Skip) {
        $selected = $selected | Where-Object { $Skip -notcontains $_.Name }
    }
}
if (-not $selected) { throw 'No services selected.' }

Initialize-StackDirs
Import-DotEnv

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
Write-Step 'Prerequisites'

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is not on PATH. Install it, or run: winget install astral-sh.uv'
}

$needsNode = $selected | Where-Object { $_.Exe -eq 'npm' }
if ($needsNode -and -not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Warn 'npm is not on PATH; skipping asset/mobile watchers'
    $selected = $selected | Where-Object { $_.Exe -ne 'npm' }
}
if (($selected | Where-Object { $_.Name -eq 'assets' }) -and -not (Test-Path 'node_modules')) {
    Write-Warn 'node_modules is missing; run `npm install`. Skipping assets.'
    $selected = $selected | Where-Object { $_.Name -ne 'assets' }
}
if (($selected | Where-Object { $_.Name -eq 'mobile' }) -and -not (Test-Path 'mobile\node_modules')) {
    Write-Warn 'mobile\node_modules is missing; run `npm install` in mobile\. Skipping mobile.'
    $selected = $selected | Where-Object { $_.Name -ne 'mobile' }
}

# PostgreSQL and Memurai are Windows services; nudge them awake rather than
# letting eight processes each fail their first connection.
foreach ($pattern in 'postgresql*', 'Memurai*') {
    $svcs = @(Get-Service -Name $pattern -ErrorAction SilentlyContinue)
    if (-not $svcs) {
        Write-Warn "No Windows service matching '$pattern'. Run scripts\install-dev-stack.ps1."
        continue
    }
    foreach ($winsvc in $svcs) {
        if ($winsvc.Status -ne 'Running') {
            Write-Info "Starting $($winsvc.Name)..."
            try {
                Start-Service $winsvc.Name
                Write-Ok "$($winsvc.Name) started"
            } catch {
                Write-Warn "Could not start $($winsvc.Name) (try an elevated shell): $($_.Exception.Message)"
            }
        } else {
            Write-Ok "$($winsvc.Name) is running"
        }
    }
}

# One warm-up so eight concurrent `uv run` invocations do not race to build the
# environment (and so a broken venv fails here, loudly, instead of in a log file).
Write-Info 'Preparing the Python environment (uv run)...'
& uv run python -c "pass"
if ($LASTEXITCODE -ne 0) { throw 'uv run failed. Try `uv sync`.' }
Write-Ok 'Python environment ready'

# ---------------------------------------------------------------------------
# Optional one-off steps
# ---------------------------------------------------------------------------
if ($Migrate) {
    Write-Step 'Migrations'
    if (-not $env:DATABASE_MIGRATE_URL) { throw 'DATABASE_MIGRATE_URL is not set in .env' }
    # Migrations run as the schema owner, never as the runtime role. Scoped to a
    # child scope so the app processes below still start as stacos_app.
    $runtimeUrl = $env:DATABASE_URL
    try {
        $env:DATABASE_URL = $env:DATABASE_MIGRATE_URL
        & uv run python manage.py migrate
        if ($LASTEXITCODE -ne 0) { throw 'migrate failed' }
        & uv run python manage.py sync_system_roles
        if ($LASTEXITCODE -ne 0) { throw 'sync_system_roles failed' }
    } finally {
        $env:DATABASE_URL = $runtimeUrl
    }
    Write-Ok 'Database is up to date'
}

if ($Build) {
    Write-Step 'Assets'
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw 'npm run build failed' }
    Write-Ok 'CSS and JS built'
}

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
Write-Step 'Starting services'

$started = @()
$failures = 0
foreach ($svc in $selected) {
    if (Test-ServiceRunning -Name $svc.Name) {
        if ($Restart) {
            Stop-StackService -Name $svc.Name | Out-Null
        } else {
            $proc = Get-LiveServiceProcess -Name $svc.Name
            Write-Info "$($svc.Name) already running (pid $($proc.Id)); use -Restart to replace it"
            continue
        }
    } else {
        Remove-ServiceState -Name $svc.Name
    }

    if ($svc.Port -gt 0 -and (Test-PortListening -Port $svc.Port)) {
        Write-Warn "$($svc.Name): port $($svc.Port) is already in use; not starting"
        continue
    }

    $log = Get-LogFile $svc.Name
    $err = Get-ErrFile $svc.Name
    Remove-Item $log, $err -ErrorAction SilentlyContinue

    $commandLine = "$($svc.Exe) $($svc.Arguments -join ' ')"
    $launch = Resolve-Executable -Exe $svc.Exe -Arguments $svc.Arguments
    try {
        $proc = Start-Process -FilePath $launch.Exe `
                              -ArgumentList $launch.Arguments `
                              -WorkingDirectory $svc.Cwd `
                              -RedirectStandardOutput $log `
                              -RedirectStandardError $err `
                              -WindowStyle Hidden `
                              -PassThru
    } catch {
        Write-Err "$($svc.Name): $($_.Exception.Message)"
        $failures++
        continue
    }

    Start-Sleep -Milliseconds 500
    if ($proc.HasExited) {
        Write-Err "$($svc.Name) exited immediately (code $($proc.ExitCode)). See $err"
        $failures++
        continue
    }

    Save-ServiceState -Name $svc.Name -Process $proc -CommandLine $commandLine
    Write-Ok "$($svc.Name.PadRight(16)) pid $($proc.Id)  $($svc.Description)"
    $started += $svc
}

# ---------------------------------------------------------------------------
# Wait for the ports that matter, so "started" means "actually accepting".
# ---------------------------------------------------------------------------
foreach ($svc in $started | Where-Object { $_.Port -gt 0 }) {
    if (Wait-Port -Port $svc.Port -TimeoutSeconds 40) {
        Write-Ok "$($svc.Name) is listening on :$($svc.Port)"
    } else {
        Write-Warn "$($svc.Name) is not listening on :$($svc.Port) yet. See $(Get-ErrFile $svc.Name)"
    }
}

Write-Step 'Up'
Write-Host @"
  Marketing        http://localhost:8000/
  Web app          http://localhost:8000/app/
  API schema       http://localhost:8000/api/v1/schema/swagger-ui/
  SSE (ASGI)       http://localhost:8001/
  Flower           http://localhost:5555/
  Mobile           http://localhost:3002/   (start with -WithMobile)

  Logs             .run\logs\
  Follow one       Get-Content .run\logs\web.log -Wait -Tail 40
  Status           .\start.ps1 -Status
  Stop everything  .\stop.ps1
"@ -ForegroundColor Gray
