# STACOS — working notes for AI agents

Read this before writing code. These are the conventions that, if broken, cost far more to repair than to follow.

## Commands

```powershell
.\tasks.ps1 run          # dev server, this terminal
.\tasks.ps1 migrate      # migrations (runs as stacos_migrator automatically)
.\tasks.ps1 test         # pytest
.\tasks.ps1 check        # every CI gate locally — run this before declaring work done

.\start.ps1              # whole stack detached: web, sse, worker, beat, flower, assets
.\start.ps1 -Status      # what is running (logs in .run\logs\)
.\stop.ps1               # stop it all

python manage.py loadpack IN        # jurisdiction pack from catalog/packs/IN.yaml
python manage.py loadcatalog        # compliance definitions from catalog/definitions/
python manage.py validatecatalog    # semantic checks; CI runs it with --strict
```

`start.sh` / `stop.sh` are the Linux and macOS equivalents, with long-form flags.

Never call `python manage.py migrate` directly: the app connects as `stacos_app`, which does not own the schema. `tasks.ps1 migrate` swaps in the owner role.

`.env` sets `DJANGO_SETTINGS_MODULE=config.settings.dev` for the dev server, and pytest-django lets that environment variable beat its own ini setting — so pytest passes `--ds=config.settings.test` explicitly in `pyproject.toml`. Do not remove it: without it, anything that sources `.env` before running the suite silently tests a different configuration than the one it claims to certify.

## The six rules

### 1. Never write an unscoped query

Every tenant-owned model inherits `TenantScopedModel`. Its default manager **raises** when no `AccessScope` is bound. That is deliberate: a forgotten scope is an exception, not a data leak.

```python
with tenant_context(tenant_ids={tenant.id}, reason="backfill"):
    Entity.objects.filter(...)
```

`Model.objects_unscoped` and `platform_scope()` exist, are deliberately ugly, are individually allowlisted in `tests/unscoped_allowlist.txt`, and write an audit row. If you reach for one, you are probably solving the wrong problem.

**Scopes nest, and the inner one restores the outer on exit.** Billing opens a platform scope to write a commission row into a dealer's tenant while already inside the client's. An earlier version cleared PostgreSQL's RLS settings unconditionally on the inner exit, and because RLS fails closed the symptom was a query in the *caller* silently returning nothing — no error, no log. `tests/security/test_scope_nesting.py` is the regression suite; do not "simplify" `_restore_rls`.

**A `ModelForm` with a foreign key to a tenant-scoped model breaks at import time**, because Django builds the field's queryset when the class is defined and no scope is bound then. Use `stacos.core.forms.ScopedModelChoiceField`, which resolves its queryset when the field is rendered or validated — inside a request, and therefore also re-checking a forged id against the caller's scope.

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

### 5. The second auth channel is WhatsApp, not SMS

Verification codes go by **email and WhatsApp**, together, in one step. `stacos/accounts/whatsapp.py` owns delivery; `WHATSAPP_PROVIDER=console` prints codes to the terminal in development.

Business-initiated WhatsApp messages need templates pre-approved by Meta, and one-time passcodes must use the **AUTHENTICATION** category. Approval takes days to weeks — treat it like DLT registration for SMS and start it early.

A number with no WhatsApp account cannot be reached, and **WhatsApp is a hard requirement for sign-up** — decided, not deferred. `PendingVerification.phone_unreachable` persists the `not_on_whatsapp` outcome and the verification screen tells the user that resending will not help. `WhatsAppResult.not_on_whatsapp` stays a distinct outcome from a delivery failure so an SMS fallback can be added later without changing a caller.

### 6. Audit every state change

`record_event()` on every transition, with actor, timestamp, IP, and before/after. `AuditLog` is append-only — a database trigger denies `UPDATE` and `DELETE`. This is what a business shows a regulator during due diligence; it is a feature, not plumbing.

## The modules, and what each one refuses to do

| App | What it owns | The line it will not cross |
|---|---|---|
| `catalog` | Versioned compliance definitions, loaded from YAML — this fork carries only GST, income-tax and TDS | A published version is immutable; correcting a rule means a new one with a new effective window |
| `obligations` | The register: what each entity owes, and where it has got to | Never destroys an instance carrying history; suppressions are an input to the planner |
| `vault` | Documents, content-addressed and linked from anywhere | Bytes never leave without a permission check, a scan, and a download row |
| `requests` | Information requests, as a checklist rather than a message | State is derived from the items, never set by hand |
| `notices` | The notice tracker and its correspondence trail | No portal scraping. The adapter interface exists and is empty — see `docs/decisions.md` |
| `returns` | Working papers, reconciliations, maker-checker | The preparer cannot be the checker, enforced by a database constraint |
| `secretarial` | Meetings, resolutions, registers, cap table | Holdings are replayed from the ledger, never stored as a balance |
| `practice` | The firm's work board, time and profitability | Scoped to the practice tenant, so a client never sees an estimate or a margin |
| `billing` | Plans, subscriptions, invoices, payments | Amounts are integer paise; an issued invoice is never edited; payments are idempotent on the gateway reference |
| `dealers` | Commission plans, ledger, payouts | A dealer sees no compliance data at all. Naming a client on a commission row is not access to that client |

## Conventions

- **UUIDv7 primary keys** via `stacos.core.ids.uuid7`. Keep that function at module scope forever — migrations serialise the reference by path.
- **Money is stored in integer minor units** (paise), never as a decimal or float. Render with `|minor|inr`; rates are basis points, rendered with `|bps`.
- **crispy-forms renders form fields; django-cotton renders everything else.** Ambiguity here produces two design systems.
- **Status vocabulary is universal**: same colour, icon and word for "overdue" everywhere. Never encode status by colour alone.
- **Indian number formatting** (₹1,23,45,678) via the template filter, never hand-rolled.
- **Due dates are `DateField`, not `DateTimeField`.** "Due 20 September" is a legal date in a jurisdiction's timezone; storing an instant guarantees off-by-one-day bugs at month boundaries in a product whose entire value is not missing deadlines.
- `select_related` / `prefetch_related` on every list view, with a query-count assertion in its test. N+1 is why server-rendered apps feel slow.
- Server-side keyset pagination, never `LIMIT/OFFSET`.
- `aria-live` on every HTMX swap target — screen readers otherwise miss partial updates entirely.

## Definition of done

Permission declared · tenant-scoped querysets · cross-tenant leakage test · audit entries emitted · both render paths · empty/loading/error states · `aria-live` on swapped regions · query-count assertion · Celery tasks idempotent and tenant-scoped · seed data · tests on business logic · `.\tasks.ps1 check` green.
