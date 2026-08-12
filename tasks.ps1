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
                 'css', 'watch', 'seed', 'superuser', 'help')]
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
        uv run python manage.py runserver 0.0.0.0:8000 @Rest
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

    'check' {
        # Every CI gate, in the order CI runs them.
        Invoke-Section 'ruff check';            uv run ruff check .
        Invoke-Section 'ruff format --check';   uv run ruff format --check .
        Invoke-Section 'mypy';                  uv run mypy stacos config
        Invoke-Section 'django check';          uv run python manage.py check
        Invoke-Section 'migration drift'
        # The schema-owner URL is needed to compare against the real schema, so
        # it is set for these three commands only and put back afterwards. Left
        # in place, the test run would connect as the owner rather than as the
        # runtime role — and Row-Level Security tests are meaningless against a
        # role that is not the one production uses.
        $runtimeDatabaseUrl = $env:DATABASE_URL
        if ($env:DATABASE_MIGRATE_URL) { $env:DATABASE_URL = $env:DATABASE_MIGRATE_URL }
        uv run python manage.py makemigrations --check --dry-run
        Invoke-Section 'view permission declarations'; uv run python manage.py check_view_permissions
        Invoke-Section 'row-level security';           uv run python manage.py ensure_rls
        $env:DATABASE_URL = $runtimeDatabaseUrl

        # Reads YAML, touches no database. Catches the two ways catalog content
        # goes wrong that nothing else can see: a rule that fires for nobody (a
        # typo in a fact name) and one that fires for everybody (a missing
        # clause). Cheap, and the alternative is a client finding out.
        Invoke-Section 'catalog validation'
        uv run python manage.py validatecatalog --strict

        # Built before the tests rather than assumed current. A stylesheet is a
        # build artefact, and `tests/test_css_coverage.py` checks every class a
        # template names against the bundle that page actually loads — which
        # tests yesterday's CSS if nobody rebuilt. Git does not preserve
        # modification times either, so on a fresh clone the ordering is
        # arbitrary unless the build runs here.
        Invoke-Section 'css build'
        npm run build:css
        if ($LASTEXITCODE -ne 0) { throw "Sass build failed with exit code $LASTEXITCODE." }

        Invoke-Section 'pytest'
        uv run pytest
        if ($LASTEXITCODE -ne 0) { throw "pytest failed with exit code $LASTEXITCODE." }

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
    .\tasks.ps1 run                 Django dev server on :8000
    .\tasks.ps1 watch               Rebuild CSS/JS on change
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
