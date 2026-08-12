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
    [ValidateSet('run', 'migrate', 'makemigrations', 'shell', 'test', 'lint', 'fmt',
                 'types', 'check', 'worker', 'beat', 'flower', 'css', 'watch',
                 'seed', 'superuser', 'help')]
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

Import-DotEnv

switch ($Task) {
    'run' {
        uv run python manage.py runserver 0.0.0.0:8000 @Rest
    }

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
        if ($env:DATABASE_MIGRATE_URL) { $env:DATABASE_URL = $env:DATABASE_MIGRATE_URL }
        uv run python manage.py makemigrations --check --dry-run
        Invoke-Section 'view permission declarations'; uv run python manage.py check_view_permissions
        Invoke-Section 'row-level security';           uv run python manage.py ensure_rls
        Invoke-Section 'pytest';                       uv run pytest
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

  Daily
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
