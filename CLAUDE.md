# STACOS — working notes for AI agents

Read this before writing code. These are the conventions that, if broken, cost far more to repair than to follow.

## Commands

```powershell
.\tasks.ps1 run          # dev server
.\tasks.ps1 migrate      # migrations (runs as stacos_migrator automatically)
.\tasks.ps1 test         # pytest
.\tasks.ps1 check        # every CI gate locally — run this before declaring work done
```

Never call `python manage.py migrate` directly: the app connects as `stacos_app`, which does not own the schema. `tasks.ps1 migrate` swaps in the owner role.

## The five rules

### 1. Never write an unscoped query

Every tenant-owned model inherits `TenantScopedModel`. Its default manager **raises** when no `AccessScope` is bound. That is deliberate: a forgotten scope is an exception, not a data leak.

```python
with tenant_context(tenant_ids={tenant.id}, reason="backfill"):
    Entity.objects.filter(...)
```

`Model.objects_unscoped` and `platform_scope()` exist, are deliberately ugly, are individually allowlisted in `tests/unscoped_allowlist.txt`, and write an audit row. If you reach for one, you are probably solving the wrong problem.

Celery tasks take `tenant_id` as an **explicit argument** and re-derive scope inside the task. A queued task must never inherit ambient authority.

### 2. Every view declares a permission

```python
@require_permission("compliance.obligation.close")
def close_obligation(request, pk): ...
```

`manage.py check_view_permissions` walks the URL resolver and fails the build for any undecorated app view. **HTMX fragment endpoints are real views** — directly reachable URLs, same decorator, no exceptions. "It's only loaded from an authenticated page" is how HTMX apps leak data.

The check proves a declaration exists, not that it is correct. Object-level scoping still needs a hostile-client test: act as tenant B, request tenant A's URL, assert 404.

### 3. Every view renders two ways

One page is two templates:

- `templates/<app>/<name>.html` — extends `layouts/app_shell.html`; its `{% block main %}` contains *only* an include of the fragment.
- `templates/<app>/_fragments/<name>_body.html` — the actual content, extends nothing.

`HtmxFragmentMixin` returns the fragment when `request.htmx`, the page otherwise. A direct GET returns the full page, so deep links and the back button work with no special handling. Use `oob()` to update several regions in one response instead of triggering a refetch.

### 4. Jurisdiction logic lives in data, never in code

No `if industry == "pharma"`, no hardcoded definition codes, no `financial_year_starts_april`. Applicability and due-date rules are JSON evaluated by `stacos.engine`; jurisdiction facts come from a `JurisdictionPack`. CI enforces this with a banned-literal lint.

`stacos/engine` is **pure Python**: no `django`, `celery` or `psycopg` imports, and no `date.today()` / `datetime.now()` — every entry point takes `as_of` explicitly, or the golden-file tests become impossible. An AST test enforces both.

Core tables carry no India-specific column names. Tax IDs go in `EntityRegistration {type, value, jurisdiction, valid_from, valid_to}` with per-type validators.

### 5. Audit every state change

`record_event()` on every transition, with actor, timestamp, IP, and before/after. `AuditLog` is append-only — a database trigger denies `UPDATE` and `DELETE`. This is what a business shows a regulator during due diligence; it is a feature, not plumbing.

## Conventions

- **UUIDv7 primary keys** via `stacos.core.ids.uuid7`. Keep that function at module scope forever — migrations serialise the reference by path.
- **crispy-forms renders form fields; django-cotton renders everything else.** Ambiguity here produces two design systems.
- **Status vocabulary is universal**: same colour, icon and word for "overdue" everywhere. Never encode status by colour alone.
- **Indian number formatting** (₹1,23,45,678) via the template filter, never hand-rolled.
- **Due dates are `DateField`, not `DateTimeField`.** "Due 20 September" is a legal date in a jurisdiction's timezone; storing an instant guarantees off-by-one-day bugs at month boundaries in a product whose entire value is not missing deadlines.
- `select_related` / `prefetch_related` on every list view, with a query-count assertion in its test. N+1 is why server-rendered apps feel slow.
- Server-side keyset pagination, never `LIMIT/OFFSET`.
- `aria-live` on every HTMX swap target — screen readers otherwise miss partial updates entirely.

## Definition of done

Permission declared · tenant-scoped querysets · cross-tenant leakage test · audit entries emitted · both render paths · empty/loading/error states · `aria-live` on swapped regions · query-count assertion · Celery tasks idempotent and tenant-scoped · seed data · tests on business logic · `.\tasks.ps1 check` green.
