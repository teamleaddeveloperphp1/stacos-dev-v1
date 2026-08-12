# STACOS

**Skorydov Tax and Compliance Software** — the shared workspace between businesses and the professionals who serve them (CAs, CSs, Cost Accountants, tax consultants, law firms).

Two halves of one product:

- **For businesses** — know every compliance obligation that applies to you, never miss a due date, hold every filing, notice, register and minute in one auditable place, and collaborate with your CA/CS inside the system.
- **For professionals** — run the whole practice: every client entity, every obligation on one board, information requests, return preparation, client approval, time tracking and billing.

The product wins or loses on one thing: **does the compliance calendar for a given entity get generated correctly and automatically, with zero manual setup?**

---

## Status

Foundation (§2) — tenancy, identity, engagements, auth, RBAC, the design system and the dual-render convention. No compliance feature code yet; see `docs/engine-design.md` for the catalog/due-date engine spec that the next milestone implements.

## Stack

Django 5.2 LTS · PostgreSQL 16 · Celery + Redis · HTMX + Alpine.js + Bootstrap 5 (Sass) · django-cotton components · DRF (mobile and webhooks only) · Framework7 + Capacitor mobile client.

Server-rendered HTML is the primary interface. There is no SPA and no client-side router. The web app talks HTML; only the mobile app talks JSON.

---

## Getting started (Windows)

```powershell
# 1. Configure. Set POSTGRES_SUPERUSER_PASSWORD in .env so the bootstrap can run
#    unattended, or leave it blank and the script will prompt.
Copy-Item .env.example .env

# 2. Install PostgreSQL and Memurai if missing, then create the roles, database
#    and extensions. Safe to re-run.
powershell -ExecutionPolicy Bypass -File scripts\install-dev-stack.ps1

# 3. Install dependencies
uv sync
npm install

# 4. Build assets, migrate, seed, run
npm run build
.\tasks.ps1 migrate
uv run python manage.py sync_system_roles
.\tasks.ps1 seed
.\tasks.ps1 run
```

The seed builds a deliberately awkward world rather than a tidy one: a Gujarat textile manufacturer with **two GST registrations and two factories**, a Bengaluru software LLP, and a CA firm engaged on only *one* of the two entities and limited to tax categories. That is what makes per-registration fan-out and engagement scoping visible from the first run instead of assumed.

| Surface | URL |
|---|---|
| Marketing + pricing | http://localhost:8000/ |
| Web app | http://localhost:8000/app/ |
| API schema | http://localhost:8000/api/v1/schema/swagger-ui/ |
| Celery inspector (Flower) | http://localhost:5555/ |
| Mobile dev server | http://localhost:3002/ |

`.\tasks.ps1` with no arguments lists every task.

---

## Two database roles, on purpose

STACOS runs the application as **`stacos_app`** — `NOSUPERUSER`, `NOBYPASSRLS` — so PostgreSQL Row-Level Security genuinely applies to it. Migrations run as the schema owner **`stacos_migrator`**.

This matters: running the app as the table owner would silently defeat RLS, and RLS is the second line of defence behind the application's tenant-scoped query manager. `.\tasks.ps1 migrate` swaps the connection URL for you.

## Tenant isolation

STACOS is shared-schema multi-tenant: every tenant-owned row carries a `tenant_id`, and the **default manager raises `UnscopedQueryError` when no scope is bound**. You cannot forget to scope a query, because the unscoped query does not exist.

Three layers, independently:

1. the scoped default manager (structural — the failure mode is an exception, not a leak),
2. PostgreSQL RLS as a hard backstop at the database,
3. `tests/security/` — an adversarial suite, parametrised over *every* scoped model, that attempts cross-tenant reads and writes and asserts failure.

Escape hatches (`Model.objects_unscoped`, `platform_scope()`) are deliberately ugly, individually allowlisted, and audited.

## Windows development notes

| Thing | Note |
|---|---|
| Celery | Prefork does not work on Windows. Dev uses `--pool=solo` (one task at a time). Linux CI and prod use prefork. Do not use eventlet/gevent — monkey-patching plus psycopg3 hangs. |
| Redis | Memurai is a native Windows Redis-compatible server. `scripts/install-dev-stack.ps1` installs it. |
| Docker | Not required for development. `docker-compose.yml` exists for CI and production parity. |
| Playwright | `uv run playwright install chromium` once, before end-to-end tests. |
| WeasyPrint | Needs GTK, which is painful on Windows. Not a Foundation dependency; PDF generation is abstracted behind `stacos/core/pdf.py` when it lands. |

## Quality gates

`.\tasks.ps1 check` runs every gate CI runs:

- `ruff check` and `ruff format --check`
- `mypy` (strict on `core`, `tenancy`, `engagements`, `engine`)
- `manage.py makemigrations --check --dry-run` — no migration drift
- `manage.py check_view_permissions` — every app view declares a required permission, verified through the URL resolver rather than by grepping
- `manage.py ensure_rls` — every tenant-scoped table has RLS enabled and a policy attached
- `pytest`, including `tests/security/`

## Layout

```
config/          settings split, celery, root urls/asgi/wsgi
stacos/
  core/          tenancy scoping, audit log, permissions, HTMX helpers, formatters
  accounts/      User, dual-OTP, trusted devices, step-up, sessions
  tenancy/       Tenant, Entity, profiles, registrations, premises, roles, memberships
  engagements/   cross-tenant access, invitations, scope resolution
  jurisdictions/ jurisdiction packs, calendars, fact registry, tax-ID validators
  marketing/     public pages and pricing
  api/           DRF: mobile and webhooks only
  platformadmin/ catalog authoring, offline payments, consented impersonation
templates/       components/ (design system), layouts/, <app>/_fragments/
assets/scss/     tokens -> Bootstrap overrides -> components
mobile/          Framework7 + React + Capacitor
tests/security/  adversarial tenant-isolation suite
docs/            architecture decisions and the engine spec
```
