# The compliance engine — design specification

> **Status: built.** The pure engine, the `catalog` app, the `obligations`
> register and the calendar UI all exist, with 129 seed definitions for India.
> This document is now a description of what was built rather than a plan,
> except where noted below.
>
> **Two things were built differently from this specification**, both for
> reasons worth knowing:
>
> * **Month-keyed offset overrides.** §3 models one offset per definition. The
>   TDS returns needed two — January–March is due 31 May while the other three
>   quarters are due one month after they close — and so did the March TDS
>   payment. `due.offset_by_month` handles it as data. The alternative was a
>   duplicate definition carrying a copy of the applicability rule, and two
>   copies of one rule drift apart within a year.
> * **Event-driven materialisation is not built.** §8's 26QB/QC/QD family and
>   anything else with `periodicity: EVENT_BASED` generates no periods, so those
>   definitions were left out of the seed catalog rather than shipped as rows
>   that never appear. `PREVIOUS_INSTANCE_DATE` similarly resolves only through a
>   declared fallback.
>
> **Still open from §7:** the golden-file scenario suite. The persona library
> exists (`stacos/catalog/personas.py`, 16 personas) and drives the dead-rule and
> discrimination checks; pinning "this persona gets exactly these 47 obligations"
> against a frozen catalog snapshot has not been done.

The product wins or loses on one thing: **does the compliance calendar for a given entity get generated correctly and automatically, with zero manual setup?** Everything else is supporting cast. This document is the design for that engine.

---

## 1. Shape

Two layers, and the distinction is load-bearing:

| | Global Compliance Catalog | Entity Obligation Register |
|---|---|---|
| Owner | Platform | Tenant |
| Rows | ~140 definitions × versions (India v1) | entities × definitions × periods × scopes |
| Mutability | Append-only, versioned, effective-dated | Mutable lifecycle, append-only event log |
| Tenancy | None — global reference data | `tenant_id` on every row, under RLS |
| Source of truth | YAML in git, loaded into the database | The database |

`stacos.engine` is **pure Python**: no `django`, `celery` or `psycopg` imports, and no `date.today()` / `datetime.now()` — every entry point takes `as_of` explicitly. Without that, golden-file testing is impossible. An AST test in CI enforces both.

```
stacos/engine/
  types.py            frozen dataclasses crossing the ORM boundary
  facts/              fact registry, derived facts, as-of resolution
  rules/              schema → validate → compile → evaluate → explain
  dates/              fiscal years, period generation, calendars, shifting, anchors
  extensions.py       government extensions
  planner/            the pure plan() function
  lifecycle/          transition table, guards, derived status
```

---

## 2. Applicability rules

Declarative JSON evaluated against a fact dictionary. `if industry == "pharma"` is banned outright and enforced by a CI lint.

```json
{ "all": [
  { "fact": "registrations", "op": "includes", "value": "GST",
    "explain": "you are registered under GST" },
  { "fact": "gst_scheme", "op": "eq", "value": "REGULAR" },
  { "any": [
    { "fact": "aggregate_turnover", "op": "gt", "value": 50000000,
      "explain": "annual turnover above ₹5 crore" },
    { "fact": "qrmp_opted", "op": "eq", "value": false }
  ]}
]}
```

**Operators.** `eq ne gt gte lt lte in not_in includes excludes between exists matches count_gte count_lte`, combined with `all any none not` and the literal `const`.

**Tri-state (Kleene) evaluation is non-negotiable.** A missing fact yields `UNKNOWN`, never `FALSE`:

```python
def k_all(vs):  # FALSE dominates, then UNKNOWN
    if V.FALSE in vs:
        return V.FALSE
    if V.UNKNOWN in vs:
        return V.UNKNOWN
    return V.TRUE
```

A definition evaluating to `UNKNOWN` is **materialised anyway**, flagged `UNCONFIRMED`, and shown as "we think this may apply — confirm". Silently dropping it is the failure mode that gets a client penalised.

Note that `all` still short-circuits to `FALSE` despite unknowns — if the entity is definitively not GST-registered, no amount of unknown turnover makes GSTR-3B apply. That is what makes onboarding tolerable rather than a wall of questions.

**Explainability is a requirement, not a nicety.** The evaluator returns a trace, pruned to the *minimal sufficient reason*: for a `FALSE` `all`, the first failing child; for a `TRUE` `all`, every child. Rendered through fact metadata rather than hardcoded strings, that produces:

> Applicable because: you are registered under GST; your GST scheme is Regular; your annual aggregate turnover (₹80.50 Cr) is above ₹5.00 Cr.

The set of *decisive* missing facts becomes the onboarding question queue, ranked by how many definitions each one unlocks.

**Performance.** Rules compile to nested closures, cached by hash. ~10–25 ms to evaluate a full catalog against one profile — fine for an interactive diff preview. Three real optimisations, in order of value: SQL prefilter by country and jurisdiction (2000 → ~120 definitions); a `facts_used` inverted index so a profile edit re-evaluates only what it could have affected; result memoisation keyed on a fingerprint of the relevant facts.

---

## 3. Due dates

```json
{ "anchor": "PERIOD_END", "offset": {"months": 1, "day_of_month": 20},
  "shift_if_holiday": "NONE", "calendars": ["IN-GST"] }
```

**Anchors:** `PERIOD_START PERIOD_END FY_START FY_END EVENT_DATE LICENCE_EXPIRY PREVIOUS_INSTANCE_DATE FIXED_DATE`.

**Offsets are months-plus-day-of-month, not days.** "The 20th of the following month" expressed as `offset_days: 20` silently drifts across February and unequal month lengths. `{"months": 1, "day_of_month": 20}` is the correct model; `day_of_month: -1` means month end, and values beyond the month length clamp.

**Unresolvable anchors return `UNRESOLVED`, never a guess.** No recorded AGM date means AOC-4 materialises with `due_date = NULL` and a `needs_input` flag surfaced as "tell us your AGM date to schedule this". A calendar that tells you what it does not know is a feature.

### Three corrections worth stating plainly

1. **Indian statutory tax dates do not shift for weekends or holidays.** The portals accept filings on a Sunday; relief comes through explicit government extensions. Defaulting to `NEXT_WORKING_DAY` produces dates that are *wrong by law*. `shift_if_holiday` is therefore mandatory in the YAML so an author must decide consciously, and shifting is opted into only for physically-dependent obligations — counter filings, cheque payments, inspections.

2. **The horizon must be filtered by due date, not by period.** GSTR-9 for FY 2025-26 is due 31 December 2026: a period that ended before the horizon opened produces an obligation inside it. Generate periods over `[horizon_start − max_lag, horizon_end]`, then filter on the resolved due date. The naive implementation silently drops every annual return.

3. **Weekend rules are effective-dated.** The UAE moved its public-sector weekend from Friday–Saturday to Saturday–Sunday in 2022. A constant produces wrong dates for every earlier date. *(Foundation already models this — `jurisdictions.WeekendRule` carries a validity window.)*

**Period keys** sort chronologically as strings within a periodicity and stay human-legible: `2026-07` → "Jul 2026"; `FY2026-27-Q2` → "Q2 FY2026-27"; `FY2026-27` → "FY 2026-27". TDS quarters are FY-anchored (Q1 = Apr–Jun) while some state professional tax is calendar-quarter, so `period_anchor` is per definition. Nothing about April–March appears in engine code; it comes from `JurisdictionPack.fy_start_month`.

---

## 4. Government extensions — first-class from day one

Indian due dates are extended by notification several times a year. Retrofitting this is painful, so it is built into the resolver signature from the first commit.

```python
def resolve_due(rule, period, ctx, extensions) -> DueDateResolution:
    base = _resolve_anchor_and_offset(rule, period, ctx)
    shifted = shift(base, rule.get("shift_if_holiday", "NONE"), ...)
    ext = extensions.match(...)
    if ext and ext.kind in ("EXTENSION", "ADVANCEMENT"):
        return DueDateResolution(original_date=shifted, effective_date=ext.new_due_date, ...)
    return DueDateResolution(original_date=shifted, effective_date=shifted, ...)
```

Four kinds, and conflating them corrupts data:

| Kind | Effect |
|---|---|
| `EXTENSION` | due date moves later |
| `ADVANCEMENT` | due date moves earlier (rare, happens) |
| `WAIVER` | **late fee or interest waived — the date does not change** |
| `AMNESTY` | a window to file old periods without penalty |

`WAIVER ≠ EXTENSION` is the most common vendor bug in this space.

Extensions frequently apply to *subsets* — state-specific flood relief, turnover-banded relief — so `scope_rule` reuses the applicability DSL. Zero new machinery.

Publishing is a single indexed `UPDATE ... WHERE (definition_code, period_key)` in the common case, which is why both columns are denormalised onto the instance rather than reached through a join. Reminders are **derived, never authoritative**: on any date change, delete future unsent reminders and regenerate; never touch sent ones, which are history.

Retroactive extensions must **not** silently reclassify an already-filed return as on-time or late. Terminal-state instances are frozen and the change is recorded informationally.

---

## 5. Materialisation

### Instance identity

```
(entity_id, definition_code, scope_ref, period_key, occurrence)
```

**`definition_version` is deliberately not in the key** — otherwise every catalog version bump duplicates every instance. It is an attribute the planner updates in place.

**`scope_ref` is essential.** GSTR-3B is filed *per GSTIN*: an entity with GST registrations in six states files six every month. Professional tax is per state registration; factory returns are per premises. *(Foundation already provides this — `EntityRegistration` and `EntityPremises` are individually addressable and validity-windowed.)*

### The planner is a pure function

```python
def plan(*, profile, catalog, extensions, calendars, pack, horizon, as_of,
         existing, overrides) -> MaterialisationPlan
```

Returns `to_create / to_update / to_supersede / to_archive / unchanged / diagnostics`. The Django side only applies it, in one transaction, after checking the catalog fingerprint has not moved since the preview was generated.

**Nothing with history is ever destroyed:**

```python
def classify_removal(inst):
    if inst.state in TERMINAL:          return KEEP_FROZEN
    if inst.has_evidence or inst.has_events or inst.state != "NOT_STARTED":
        return SUPERSEDE(state="NOT_APPLICABLE", reason=<pruned trace>, retain=True)
    return ARCHIVE(...)                 # soft delete, clean rows only
```

**Idempotency is a property, provable by test:** `plan(state_after_apply(plan)) == empty_plan`. A hypothesis property, not a hope.

**User overrides are an input.** A nightly job that resurrects obligations the user marked not-applicable destroys trust faster than any bug.

Triggers: nightly rolling 18-month horizon (auto-applied, since the daily delta is additive); on profile change (synchronous, ~10–25 ms, shown as a diff preview — "3 added, 1 removed, 2 dates changed"); on catalog publish (staged rollout, human review for anything that would supersede existing instances — a bad rule reaching 10,000 tenants at once is the platform's worst case).

---

## 6. Lifecycle

`NOT_STARTED → INFO_REQUESTED → IN_PREPARATION → PENDING_REVIEW → PENDING_CLIENT_APPROVAL → READY_TO_FILE → FILED → CLOSED`, plus `NOT_APPLICABLE`, `DEFERRED`, `DISPUTED`.

**Two states are derived, not stored:**

- `OVERDUE` — and it **cannot** be a PostgreSQL generated column, because those require an `IMMUTABLE` expression and `CURRENT_DATE` is only `STABLE`. Nor should it be a nightly-stamped boolean, which is wrong for up to 24 hours and generates every "why does this say on track" support ticket. Derive it in the query with a `Case` annotation, backed by a partial index on `(tenant_id, due_date) WHERE state IN (open states)`.
- `FILED_LATE` — a *fact* (`filed_on > due_date`), not a workflow position. As a stored state it collides with `CLOSED` and doubles the transition table.

The same derivation exists as a pure function in the engine, with a test asserting Python/SQL parity across a fixture matrix. Two implementations of one rule is the risk; the parity test is the mitigation.

**An explicit transition table, not a library.** `django-fsm` is ORM-coupled and could not live in a pure-Python engine, and the table has to be serialisable to the front end so the UI can ask "what can I do here" and get an answer filtered by permission. It is about seventy lines.

---

## 7. Testing

Five layers. 100% branch coverage on `stacos/engine`, enforced in CI.

1. **Truth tables** — exhaustively enumerate Kleene combinators over `{TRUE, FALSE, UNKNOWN}` against an independently written reference.
2. **Property-based (hypothesis)** for date maths: periods partition their window without gaps or overlap; keys sort chronologically; exactly one fiscal year contains any date; shifting always lands on a working day and moves the right way; **degrading information never flips a definite answer** — dropping a fact may turn `TRUE` into `UNKNOWN` but never into `FALSE`. That last one is the safety-critical invariant of the whole layer.
3. **Golden-file scenarios** — "a Gujarat textile manufacturer with ₹80 Cr turnover and 200 employees gets exactly these 47 obligations". Pinned to a **frozen catalog snapshot**: run against the live catalog, every legitimate edit would break every scenario and the suite would be switched off within a month. The failure message matters as much as the assertion — a reviewer must see "IN-LABOUR-POSH-ANNUAL is now applicable", not a 2000-line dict diff.
4. **Catalog validation** against the *live* catalog on every catalog PR. The highest-ROI suite in the project: every rule passes schema and semantic validation; every fact reference resolves; no overlapping effective windows; **dead-rule detection** (a definition true for no persona is a typo); **discrimination check** (a definition true for every persona is missing a clause); `shift_if_holiday` explicitly set; every definition reviewed within twelve months.
5. **Django integration** — apply twice, no change; constraints bite; RLS holds; the Python/SQL parity test.

A persona library of ~12 entities doubles as sales demo data.

---

## 8. Seed catalog

| Family | Definitions | |
|---|---:|---|
| GST | ~16 | GSTR-1/3B (monthly and QRMP), PMT-06, CMP-08, GSTR-4/5/6/7/8, 9, 9C, ITC-04, LUT |
| TDS/TCS | ~12 | 24Q/26Q/27Q/27EQ, monthly payments, Form 16/16A, 26QB/QC/QD |
| Income tax | ~12 | ITR by entity type × audit status, tax audit, 4 advance-tax instalments, 3CEB, 29B, 61A |
| MCA/ROC | ~18 | AOC-4, MGT-7/7A, DIR-3 KYC, ADT-1, DPT-3, MSME-1, BEN-2, PAS-6, CSR-2, AGM, board meetings, LLP 8 & 11 |
| PF/ESIC | ~8 | ECR, PF payment, ESIC contribution and half-yearly return |
| Professional tax | ~15 | State by state — where `jurisdiction[]` earns its existence |
| Labour / factories | ~15 | Factory licence, Form 21/22, S&E renewal, contract labour, POSH, bonus, LWF, gratuity |
| Environment / safety | ~10 | Consent to Operate, hazardous and e-waste, Form V, fire NOC, lift, pressure vessel |
| FEMA / RBI | ~8 | FLA, APR, FC-GPR, FC-TRS, ODI, ECB-2, Softex |
| Licences | ~8 | FSSAI, IEC, Udyam, trade licence, drug licence, legal metrology |
| Internal governance | ~10 | Fire extinguisher refill, first-aid checks, insurance and DSC renewals, DR test |

**≈140 definitions** makes the product real for roughly 90% of Indian SMEs. Architect for 2000 — the prefilter and facts-index design handles it — but do not *build* for 2000 on day one. The v1 risk is catalog correctness, not evaluator throughput.

**Storage: one YAML file per definition, in git, loaded by `manage.py loadcatalog`.** Rejected alternatives, with reasons:

- *Django fixtures* — no versioning semantics, no partial load, and a domain expert cannot review a fixture diff.
- *Data migrations* — catalog content changes weekly with every GST notification. Migrations are an append-only, non-re-runnable log designed for *schema*; you would accumulate hundreds of unreviewable files, could not correct a past one, and would couple catalog fixes to code deploys, which is exactly backwards.
- *Admin-UI-only authoring* — no code review, no CI validation, no rollback, no diff history. It becomes the source of the product's worst outages.

YAML gives a PR that reads like the law changed, CI validation as a merge gate, deterministic rebuild of any historical catalog state, and a one-command rollback because versions are immutable.

Versioning uses **append-only version rows**, unique on `(code, version)`, with a PostgreSQL exclusion constraint guaranteeing that at most one published version claims any statutory instant. Two independent time axes, and conflating them means a typo fix looks like a change in the law: `effective_from`/`effective_to` is *statutory validity*; `status`/`published_at` is *editorial lifecycle*.

---

## What Foundation already provides

Built and tested in §2, specifically so this milestone is pure feature work:

- `EntityRegistration` and `EntityPremises` — individually addressable, validity-windowed, so `scope_ref` fan-out works.
- `EntityFactValue` — bitemporal fact history, so applicability can be evaluated *as of a period* rather than as of today.
- `jurisdictions.FactRegistry` — typed facts with units and allowed values; validates rules at authoring time and drives the future admin rule editor.
- `JurisdictionPack` + `WeekendRule` + `HolidayCalendar` — fiscal year convention, effective-dated weekends, per-state holidays.
- `Authority` — the regulator registry the catalog references.
- The AST purity test and banned-literal lint, already in CI, so `stacos/engine` cannot drift from day one.

## Open questions for the engine milestone

1. **Effective-dated facts are the hardest unsolved problem.** `EntityFactValue` gives the storage; the policy is undecided. A retroactive turnover restatement produces back-dated obligations — these should be flagged `retroactive=True` and routed to a human, never auto-applied. Needs a product decision.
2. **`PREVIOUS_INSTANCE_DATE` creates a sequential dependency** (board meeting gap ≤ 120 days). Projection beyond depth 2 is speculative and should render differently, or clients will treat a projection as a commitment.
3. **Legal exposure of `plain_language_summary`.** Requires `reviewed_by` / `reviewed_at` / `confidence`, a visible disclaimer, and a CI check that no published definition has a review older than twelve months.
