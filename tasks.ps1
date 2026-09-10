<#
.SYNOPSIS
    STACOS developer task runner.

.DESCRIPTION
    Wraps the commands you run daily. The important one is `migrate`: STACOS runs
    the application as `stacos_app` (NOSUPERUSER / NOBYPASSRLS, so Row-Level
    Security genuinely applies) but migrations must run as the schema owner
    `stacos_migrator`. This script swaps DATABASE_URL for that one command so you
    never have to think about it.

.EXAMPLE
    .\tasks.ps1 run
    .\tasks.ps1 migrate
    .\tasks.ps1 test
    .\tasks.ps1 check          # every CI gate, locally
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('run', 'start', 'stop', 'status', 'migrate', 'makemigrations', 'shell',
                 'test', 'lint', 'fmt', 'types', 'check', 'worker', 'beat', 'flower',
                 'css', 'watch', 'seed', 'superuser', 'deploycheck', 'help')]
    [string]$Task = 'help',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

function Import-DotEnv {
    if (-not (Test-Path '.env')) {
        Write-Host 'No .env found. Copying from .env.example.' -ForegroundColor Yellow
        Copy-Item '.env.example' '.env'
    }
    Get-Content '.env' | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $name = $Matches[1]
            $value = $Matches[2].Trim().Trim('"').Trim("'")
            Set-Item -Path "env:$name" -Value $value
        }
    }
}

function Invoke-Section { param([string]$Name) Write-Host "`n--- $Name ---" -ForegroundColor Cyan }

<#
Run one CI gate and stop the run if it fails.

`$ErrorActionPreference = 'Stop'` does nothing for a native executable's exit
code — ruff, mypy, npm and pytest all report failure that way and PowerShell
carries on regardless. Every gate therefore goes through here, so that a
non-zero exit is a thrown error rather than a line of red text somebody scrolls
past on the way to a green "All gates passed".
#>
function Invoke-Gate {
    param([string]$Name, [scriptblock]$Command)

    Invoke-Section $Name
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Gate '$Name' failed with exit code $LASTEXITCODE."
    }
}

<#
Turns trailing arguments into a hashtable for splatting.

`& script @Rest` cannot be used to forward flags: splatting an *array* passes
every element positionally, so `-Ports` arrives as a value rather than binding
the switch. A hashtable splat binds by name, which is what pass-through needs.
#>
function ConvertTo-ParameterSplat {
    param([string[]]$Tokens)

    $splat = @{}
    for ($i = 0; $i -lt $Tokens.Count; $i++) {
        $token = $Tokens[$i]
        if ($token -notmatch '^-{1,2}([A-Za-z][A-Za-z0-9]*)$') {
            throw "Cannot forward '$token'. Call the script directly for arguments like this."
        }
        $name = $Matches[1]
        # A following token that is not itself a flag is this parameter's value.
        # `-\D` rather than `-`, so a negative number still reads as a value.
        if ($i + 1 -lt $Tokens.Count -and $Tokens[$i + 1] -notmatch '^-\D') {
            # ValueFromRemainingArguments flattens `-Only web,worker` into the
            # single string "web worker", so a list is re-split on either
            # separator. No parameter here takes a value containing a space.
            $value = $Tokens[$i + 1]
            if ($value -match '[,\s]') { $splat[$name] = $value -split '[,\s]+' } else { $splat[$name] = $value }
            $i++
        } else {
            $splat[$name] = $true
        }
    }
    return $splat
}

Import-DotEnv

switch ($Task) {
    'run' {
        <#
        The dev server plus the asset watchers, because they are not optional.

        `runserver` reloads Python, but nothing rebuilds `static/css/app.css` or
        `static/js/app.js` -- so a styling change looks like it did nothing until
        somebody remembers to run `tasks.ps1 watch` in a second terminal, and the
        browser quietly renders whatever was compiled last. `start.ps1` already
        keeps an `assets` service alive for exactly this reason; `run` was the
        path that missed it.

        Build once first so the very first page load is not unstyled, then leave
        sass and esbuild watching for as long as this server runs.
        #>
        $assets = $null
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            Write-Host 'npm is not on PATH; asset watchers not started.' -ForegroundColor Yellow
        } elseif (-not (Test-Path 'node_modules')) {
            Write-Host 'node_modules is missing; run `npm install` first.' -ForegroundColor Yellow
        } else {
            Invoke-Gate 'asset build' { npm run build }
            $assets = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', 'npm', 'run', 'dev' -PassThru -WindowStyle Hidden
            Write-Host 'Watching assets/scss and assets/js.' -ForegroundColor Cyan
        }

        try {
            uv run python manage.py runserver 0.0.0.0:8000 @Rest
        } finally {
            # taskkill /T rather than Stop-Process: npm spawns sass and esbuild as
            # children, and killing only the parent leaves both watching files and
            # holding the output open. Same reasoning as scripts/_stack.ps1.
            if ($assets -and -not $assets.HasExited) {
                & cmd.exe /c "taskkill /PID $($assets.Id) /T /F >nul 2>&1"
            }
        }
    }

    # `run` is the server alone, in this terminal. `start` is the whole stack --
    # server, workers, beat, flower, asset watchers -- detached, logging to .run\logs.
    'start'  { $fwd = ConvertTo-ParameterSplat $Rest; & (Join-Path $PSScriptRoot 'start.ps1') @fwd }
    'stop'   { $fwd = ConvertTo-ParameterSplat $Rest; & (Join-Path $PSScriptRoot 'stop.ps1') @fwd }
    'status' { & (Join-Path $PSScriptRoot 'start.ps1') -Status }

    'migrate' {
        # Migrations run as the schema owner, not as the runtime role.
        if (-not $env:DATABASE_MIGRATE_URL) { throw 'DATABASE_MIGRATE_URL is not set in .env' }
        $env:DATABASE_URL = $env:DATABASE_MIGRATE_URL
        Write-Host 'Running migrations as stacos_migrator (schema owner)...' -ForegroundColor Cyan
        uv run python manage.py migrate @Rest
    }

    'makemigrations' {
        if ($env:DATABASE_MIGRATE_URL) { $env:DATABASE_URL = $env:DATABASE_MIGRATE_URL }
        uv run python manage.py makemigrations @Rest
    }

    'shell'     { uv run python manage.py shell @Rest }
    'superuser' {
        if ($env:DATABASE_MIGRATE_URL) { $env:DATABASE_URL = $env:DATABASE_MIGRATE_URL }
        uv run python manage.py createsuperuser @Rest
    }
    'seed'      { uv run python manage.py seed_dev @Rest }

    'test'      { uv run pytest @Rest }
    'lint'      { uv run ruff check . @Rest }
    'fmt'       { uv run ruff format .; uv run ruff check --fix . }
    'types'     { uv run mypy stacos config @Rest }

    <#
    The pre-deploy gate, deliberately separate from `check`.

    It runs against *production* settings, which is the only configuration where
    the deploy-only checks say anything: `stacos.vault.E001` fires when the
    development virus scanner would run with DEBUG off, and that is invisible
    under dev settings by construction. Not folded into `check` because a
    developer's machine legitimately has no clamd, and a gate that is always red
    locally is a gate people learn to ignore.
    #>
    'deploycheck' {
        $env:DJANGO_SETTINGS_MODULE = 'config.settings.prod'
        Invoke-Gate 'deployment check' { uv run python manage.py check --deploy }
        Write-Host "`nDeployment checks passed." -ForegroundColor Green
    }

    'check' {
        <#
        Every CI gate, in the order CI runs them.

        Each one goes through `Invoke-Gate`, and that is not decoration. A native
        executable that exits non-zero does NOT stop this script:
        `$ErrorActionPreference = 'Stop'` governs PowerShell errors, not process
        exit codes. Written the obvious way, `ruff` could find twenty violations
        and the script would carry on and print "All gates passed" in green — a
        gate that reports success over a failure is worse than no gate, because
        people stop reading the output above it.
        #>
        Invoke-Gate 'ruff check'          { uv run ruff check . }
        Invoke-Gate 'ruff format --check' { uv run ruff format --check . }
        Invoke-Gate 'mypy'                { uv run mypy stacos config }
        Invoke-Gate 'django check'        { uv run python manage.py check }

        # The schema-owner URL is needed to compare against the real schema, so
        # it is set for these three commands only and put back afterwards. Left
        # in place, the test run would connect as the owner rather than as the
        # runtime role — and Row-Level Security tests are meaningless against a
        # role that is not the one production uses.
        $runtimeDatabaseUrl = $env:DATABASE_URL
        if ($env:DATABASE_MIGRATE_URL) { $env:DATABASE_URL = $env:DATABASE_MIGRATE_URL }
        try {
            Invoke-Gate 'migration drift'                { uv run python manage.py makemigrations --check --dry-run }
            Invoke-Gate 'view permission declarations'   { uv run python manage.py check_view_permissions }
            Invoke-Gate 'row-level security'             { uv run python manage.py ensure_rls }
        } finally {
            $env:DATABASE_URL = $runtimeDatabaseUrl
        }

        # Reads YAML, touches no database. Catches the two ways catalog content
        # goes wrong that nothing else can see: a rule that fires for nobody (a
        # typo in a fact name) and one that fires for everybody (a missing
        # clause). Cheap, and the alternative is a client finding out.
        Invoke-Gate 'catalog validation' { uv run python manage.py validatecatalog --strict }

        # Built before the tests rather than assumed current. A stylesheet is a
        # build artefact, and `tests/test_css_coverage.py` checks every class a
        # template names against the bundle that page actually loads — which
        # tests yesterday's CSS if nobody rebuilt. Git does not preserve
        # modification times either, so on a fresh clone the ordering is
        # arbitrary unless the build runs here.
        Invoke-Gate 'css build' { npm run build:css }

        # Same reasoning for the script bundle. `static/js/app.js` is committed,
        # so a change to `assets/js/` that nobody rebuilt ships the old
        # behaviour to production while the source in review looks correct.
        Invoke-Gate 'js build' { npm run build:js }

        # And then prove it was committed. Building it here makes the *tests*
        # correct while leaving the repository holding a stale bundle -- and the
        # repository is what deploys. `git diff --exit-code` is the only thing
        # that notices. `static/css/` is gitignored, so this applies to the
        # script bundle alone.
        Invoke-Gate 'js bundle committed' { git diff --exit-code -- static/js/app.js }

        Invoke-Gate 'pytest' { uv run pytest }

        Write-Host "`nAll gates passed." -ForegroundColor Green
    }

    'worker' {
        # Windows has no prefork; --pool=solo is the only reliable option in dev.
        $queues = 'default,reminders,materialise,portal,ocr,returns,billing,exports'
        uv run celery -A config worker --pool=solo -l info -Q $queues @Rest
    }
    'beat'   { uv run celery -A config beat -l info @Rest }
    'flower' { uv run celery -A config flower --port=5555 @Rest }

    'css'   { npm run build:css }
    'watch' { npm run dev }

    default {
        Write-Host @'
STACOS task runner

  Setup
    scripts\install-dev-stack.ps1   Install Postgres 16 + Memurai, bootstrap roles
    uv sync                         Install Python dependencies
    npm install                     Install Sass/esbuild + Bootstrap/HTMX/Alpine

  Whole stack (detached; logs in .run\logs)
    .\start.ps1                     web, sse, worker, beat, flower, assets
    .\start.ps1 -Status             what is running
    .\stop.ps1                      stop it all
    (.\tasks.ps1 start/stop/status are aliases; extra arguments pass through)

  One process at a time, in this terminal
    .\tasks.ps1 run                 Django dev server on :8000, watching assets
    .\tasks.ps1 watch               Rebuild CSS/JS on change (watchers alone)
    .\tasks.ps1 worker              Celery worker (--pool=solo, Windows)
    .\tasks.ps1 beat                Celery beat scheduler
    .\tasks.ps1 flower              Celery inspector on :5555

  Database  (migrations run as stacos_migrator automatically)
    .\tasks.ps1 migrate
    .\tasks.ps1 makemigrations
    .\tasks.ps1 seed                Demo tenants, entities, engagements
    .\tasks.ps1 superuser

  Quality
    .\tasks.ps1 check               Every CI gate, locally
    .\tasks.ps1 test / lint / fmt / types
'@ -ForegroundColor Gray
    }
}
