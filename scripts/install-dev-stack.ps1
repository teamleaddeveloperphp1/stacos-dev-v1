<#
.SYNOPSIS
    Provisions the STACOS local development stack on Windows.

.DESCRIPTION
    Installs PostgreSQL and Memurai (a native Windows Redis-compatible server) if
    they are missing, then bootstraps the STACOS database, roles and extensions.

    STACOS uses TWO database roles on purpose:

      stacos_migrator  owns the schema and runs migrations.
      stacos_app       is the runtime role. It is NOSUPERUSER with no BYPASSRLS,
                       so PostgreSQL Row-Level Security genuinely applies to it.
                       This is the second line of defence behind the application's
                       tenant-scoped query manager.

    Running the app as the table owner would silently defeat RLS, which is exactly
    the failure this split prevents.

.PARAMETER SuperUserPassword
    Password for the `postgres` superuser. If omitted, it is read from
    POSTGRES_SUPERUSER_PASSWORD in .env; if that is empty you are prompted.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-dev-stack.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [string]$SuperUser = '',
    [string]$SuperUserPassword = '',
    [string]$DbName = 'stacos',
    [string]$AppPassword = 'stacos_app_dev',
    [string]$MigratorPassword = 'stacos_migrator_dev'
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

function Write-Step { param([string]$m) Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Write-Ok   { param([string]$m) Write-Host "  [ok] $m" -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Host "  [!!] $m" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# Locate psql. The PostgreSQL installer does not add it to PATH.
# ---------------------------------------------------------------------------
function Find-Psql {
    $onPath = Get-Command psql -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    foreach ($v in 18, 17, 16) {
        $candidate = "C:\Program Files\PostgreSQL\$v\bin\psql.exe"
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Read a value out of .env
# ---------------------------------------------------------------------------
function Get-EnvValue {
    param([string]$Name)
    if (-not (Test-Path '.env')) { return '' }
    $match = Select-String -Path '.env' -Pattern "^$Name=(.*)$" | Select-Object -First 1
    if (-not $match) { return '' }
    return $match.Matches[0].Groups[1].Value.Trim().Trim('"').Trim("'")
}

# ---------------------------------------------------------------------------
# 1. Install, if needed
# ---------------------------------------------------------------------------
if (-not $SkipInstall) {
    Write-Step 'Checking PostgreSQL and Memurai'

    if (Find-Psql) {
        Write-Ok "PostgreSQL found at $(Find-Psql)"
    } elseif (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host '  Installing PostgreSQL (you will be asked to set a superuser password)...'
        winget install --id PostgreSQL.PostgreSQL.18 --accept-package-agreements --accept-source-agreements
        Write-Warn 'Open a NEW terminal after installation, then re-run this script.'
    } else {
        throw 'PostgreSQL is not installed and winget is unavailable. Install it manually.'
    }

    if (Get-Service -Name 'Memurai*' -ErrorAction SilentlyContinue) {
        Write-Ok 'Memurai service is present'
    } elseif (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host '  Installing Memurai Developer (Redis-compatible, native Windows)...'
        winget install --id Memurai.MemuraiDeveloper --accept-package-agreements --accept-source-agreements
    } else {
        Write-Warn 'Memurai not found. Celery and caching will not work until it is installed.'
    }
}

# ---------------------------------------------------------------------------
# 2. Credentials
# ---------------------------------------------------------------------------
$psql = Find-Psql
if (-not $psql) { throw 'psql not found. Open a new terminal, or install PostgreSQL first.' }

if (-not $SuperUser) {
    $SuperUser = Get-EnvValue 'POSTGRES_SUPERUSER'
    if (-not $SuperUser) { $SuperUser = 'postgres' }
}
if (-not $SuperUserPassword) {
    $SuperUserPassword = Get-EnvValue 'POSTGRES_SUPERUSER_PASSWORD'
}
if (-not $SuperUserPassword) {
    $secure = Read-Host -AsSecureString "Password for the '$SuperUser' PostgreSQL superuser"
    $SuperUserPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
}

# psql reads PGPASSWORD from the environment, avoiding an interactive prompt.
$env:PGPASSWORD = $SuperUserPassword
$env:PGCLIENTENCODING = 'UTF8'

# ---------------------------------------------------------------------------
# 3. Bootstrap
# ---------------------------------------------------------------------------
Write-Step 'Creating roles, database and extensions'

$rolesSql = @"
DO `$`$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stacos_migrator') THEN
        -- CREATEDB so pytest can build the test database from this connection.
        CREATE ROLE stacos_migrator LOGIN PASSWORD '$MigratorPassword' CREATEDB;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'stacos_app') THEN
        -- NOSUPERUSER and NOBYPASSRLS are the entire point of this role.
        CREATE ROLE stacos_app LOGIN PASSWORD '$AppPassword'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
`$`$;
"@

$rolesSql | & $psql -U $SuperUser -d postgres -v ON_ERROR_STOP=1 -q
if ($LASTEXITCODE -ne 0) { throw 'Failed to create roles. Is the superuser password correct?' }
Write-Ok 'Roles stacos_migrator and stacos_app exist'

$exists = & $psql -U $SuperUser -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DbName'"
if ($exists -ne '1') {
    & $psql -U $SuperUser -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE $DbName OWNER stacos_migrator;"
    Write-Ok "Database '$DbName' created"
} else {
    Write-Ok "Database '$DbName' already exists"
}

$extensionsSql = @"
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- trigram search for the command palette
CREATE EXTENSION IF NOT EXISTS unaccent;     -- accent-insensitive search
CREATE EXTENSION IF NOT EXISTS btree_gist;   -- exclusion constraints over date ranges
CREATE EXTENSION IF NOT EXISTS pgcrypto;     -- random bytes for token material

GRANT CONNECT ON DATABASE $DbName TO stacos_app;
GRANT USAGE ON SCHEMA public TO stacos_app;

-- The app role reads and writes data but never owns or alters schema.
ALTER DEFAULT PRIVILEGES FOR ROLE stacos_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO stacos_app;
ALTER DEFAULT PRIVILEGES FOR ROLE stacos_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO stacos_app;
"@

$extensionsSql | & $psql -U $SuperUser -d $DbName -v ON_ERROR_STOP=1 -q
if ($LASTEXITCODE -ne 0) { throw 'Failed to create extensions or grants.' }
Write-Ok 'Extensions installed and privileges granted'

$env:PGPASSWORD = ''

# ---------------------------------------------------------------------------
# 4. Verify Memurai
# ---------------------------------------------------------------------------
Write-Step 'Verifying Memurai'
$memurai = Get-Service -Name 'Memurai*' -ErrorAction SilentlyContinue
if ($memurai -and $memurai.Status -eq 'Running') {
    Write-Ok 'Memurai is running on localhost:6379'
} elseif ($memurai) {
    Write-Warn "Memurai is installed but $($memurai.Status). Start it with: Start-Service Memurai"
} else {
    Write-Warn 'Memurai not installed. Celery and caching will not work.'
}

# ---------------------------------------------------------------------------
Write-Step 'Done'
Write-Host @"
  Next:
    .\tasks.ps1 migrate
    uv run python manage.py sync_system_roles
    .\tasks.ps1 seed
    .\tasks.ps1 run

  Migrations run as stacos_migrator; the app runs as stacos_app.
  tasks.ps1 handles that switch for you.
"@ -ForegroundColor Gray
