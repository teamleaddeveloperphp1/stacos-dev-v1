# Architecture decisions

Short records of the choices that would be expensive to revisit, and why they were made. Written so that the reasoning survives the person who made it.

---

## 1. Shared-schema multi-tenancy, not schema-per-tenant

**Decision.** Every tenant-owned row carries a `tenant_id`. The default manager raises when no scope is bound. PostgreSQL row-level security enforces the same boundary independently at the database.

**Rejected:** `django-tenants` schema-per-tenant.

**Why.** Four reasons, in order of weight:

- **Engagements are inherently cross-tenant reads.** A firm's portfolio board renders obligations belonging to 300 different organisation tenants in one view. Schema-per-tenant makes that 300 schema switches or a 300-way `UNION ALL` per page — and it is the *primary* screen of the professional half of the product.
- **The catalog joins to tenant data.** Every obligation instance joins a `ComplianceDefinition` that lives outside any tenant.
- **Migrations.** `migrate` per schema across 10,000 tenants takes hours, and a mid-way failure leaves a partially migrated fleet.
- **Global identity.** One user belongs to many tenants; partitioned tenant data makes the switcher awkward.

**The cost, stated honestly.** One missing `.filter(tenant_id=…)` leaks customer data. The entire answer is three independent layers: a manager that raises rather than returning everything, RLS as a database backstop, and an adversarial test suite parametrised over every scoped model.

---

## 2. Row-level security enabled in v1, with `FORCE` and a bypass flag

**Decision.** Every scoped table has RLS `ENABLE`d **and** `FORCE`d, with a policy comparing `tenant_id` against a transaction-local `stacos.tenant_ids` setting. A `stacos.bypass_rls` flag exists for data migrations and platform administration.

**Why `FORCE`.** Without it the table owner bypasses every policy — and in the test environment the owner *is* the connecting role, which would make every RLS test silently pass.

**Why a bypass flag, and what it costs.** Data migrations and platform administration genuinely need to see every row. The consequence is that any role able to execute `SET` can disable RLS. So the honest claim is the narrower one: **RLS here defends against application bugs** — a forgotten filter in raw SQL, a `RawSQL` aggregate — **not against an attacker with arbitrary SQL execution.** That attacker has already won.

**Why `ATOMIC_REQUESTS` is not used.** Middleware runs *outside* the `ATOMIC_REQUESTS` transaction, so a plain `SET` would leak across pooled connections — worse than no RLS at all. `ScopeMiddleware` opens its own transaction and issues `set_config(..., is_local => true)` as the first statement.

---

## 3. Two database roles

**Decision.** `stacos_migrator` owns the schema and runs migrations. `stacos_app` is the runtime role: `NOSUPERUSER`, `NOBYPASSRLS`.

**Why.** Running the application as the table owner silently defeats RLS. Retrofitting table ownership once production data exists is a real maintenance window, so the split lands in the first migration even though it only pays off later.

---

## 4. UUIDv7 primary keys, generated in Python

**Decision.** `stacos.core.ids.uuid7`, hand-rolled, zero dependencies.

**Why UUIDv7.** Time-ordered, so B-tree locality approaches a bigint sequence — which matters because the obligation register becomes the largest table. Not enumerable, so a scoping bug does not become a walk of the whole table. Generable offline, which the mobile client needs for evidence captured on a factory floor with no signal.

**Why hand-rolled.** Python 3.12 has no `uuid.uuid7()` (3.14) and PostgreSQL 16 has no `uuidv7()` (18). A Rust-backed dependency would add a build requirement to CI and production for twenty lines of bit manipulation. The implementation is unit-tested for monotonicity across 10,000 ids.

**Constraint.** `uuid7` must never move from that module path — Django serialises the function reference into every migration using it as a default.

**Accepted trade-off.** UUIDv7 leaks creation time to anyone holding the id. Acceptable for a customer's own data; if entity or engagement creation timing ever becomes competitively sensitive, the fix is a separate opaque public reference, which the URL scheme can accommodate.

---

## 5. Django 5.2 LTS, not the latest release

**Decision.** Pinned to `>=5.2.17,<6.0` although 6.1 is current.

**Why.** 5.2 is the LTS with security support to April 2028. A compliance product should not chase feature releases; the buyers who care most about this software are the ones who ask which version it runs.

---

## 6. Dual OTP as one screen, one call, one rate limit

**Decision.** Both an email code and a phone code, submitted together, to complete registration and to sign in from an unrecognised device. Social sign-in satisfies neither.

**Why not two steps.** Splitting it lets one channel be satisfied and the other deferred, which is exactly what the policy exists to prevent.

**Why per-field errors are still shown.** Revealing which code was wrong buys an attacker almost nothing against a five-attempt limit, and saves a legitimate user retyping two codes on a phone. The attempt counts **once** either way.

**Why WhatsApp and not SMS.** The phone-side code goes over WhatsApp. For Indian businesses that is the channel people actually read — an SMS competes with a hundred promotional messages a day. It also renders as an authentication template with a copy-code button, which removes the transcription error that makes six-digit codes irritating on a phone.

The trade-off, stated plainly: **a number with no WhatsApp account cannot be reached at all.** Rare for an Indian business contact, not impossible. `WhatsAppResult.not_on_whatsapp` is a distinct outcome from a delivery failure precisely so an SMS fallback can be added later without changing a single caller — but no fallback exists today, and a user whose number is not on WhatsApp currently cannot complete sign-up.

**What makes it workable.** Trusted devices. Two codes on every sign-in would produce a support queue rather than a security control. Device trust carries the large majority of sign-ins; the codes appear on first use of a browser and then not for thirty days.

**Rate limiting, not hashing, is the protection.** A six-digit code has a million possibilities. Codes are HMAC'd with a server pepper — a slow hash would be theatre. Four independent limits: per identity per hour and per day, per IP per hour, exponential resend backoff, and a global daily spend cap that **fails closed**, because an unthrottled send endpoint is a way to spend someone else's money.

---

## 7. `security_stamp` in the session hash and the JWT

**Decision.** A rotating UUID on `User`, mixed into `get_session_auth_hash()` and carried as a `sec` claim in mobile tokens.

**Why.** Django validates a session against a hash of the *password*, so a permission change cannot invalidate a live session. Without the stamp, "sign out everywhere" and forced sign-out on a role change are aspirational.

**Why the JWT claim specifically.** A JWT bypasses `SessionMiddleware` entirely, so the verification gate, step-up and forced sign-out do not apply to the API by default. Ship it that way and signing out everywhere quietly does nothing to the mobile app until the token expires — the most commonly shipped hole of this shape.

---

## 8. Phone numbers are unique only among *verified* identities

**Decision.** A partial unique constraint on `phone_e164 WHERE phone_verified`.

**Why.** Indian proprietorships and family businesses routinely share a number. A hard global unique both blocks legitimate signups and creates an account-squatting denial of service: register with someone's number, never verify, and they can never use it.

**Open.** If the product later wants "one human, one phone" as an identity guarantee, that changes the recovery flow and needs deciding before it is assumed anywhere.

---

## 9. Permissions are strings in a registry, not role names

**Decision.** A `PermissionRegistry` of dotted codes. Roles are named bundles. Every view declares its requirement, verified in CI by walking the **URL resolver**, not by grepping source.

**Why the resolver.** A grep misses permissions applied at `as_view()` time and produces false positives for helper functions that merely look like views. The resolver sees exactly what is routable.

**What the check does and does not prove.** It proves a declaration *exists*. It says nothing about whether the declaration is *correct*, and nothing about object-level scoping. Two people will read a green build as "authorisation is tested" — treat it as a lint. The security control is `tests/security/`, which acts as one tenant and demands another's rows.

---

## 10. Audit log: append-only by trigger, not partitioned in v1

**Decision.** A PostgreSQL trigger rejects `UPDATE` and `DELETE` on `core_auditlog` unless a maintenance flag is deliberately set. BRIN index on `occurred_at`. **Not** partitioned.

**Why not partitioned.** Partitioning from day one needs either a real dependency in the migration path or hand-rolled DDL with `managed = False`, where model and schema can silently diverge. Not partitioning means converting later requires a table rewrite — but with a 24-month hot window and monthly archival to object storage, that rewrite is bounded rather than unbounded. A judgement call, and worth revisiting once real volume exists.

**Why the trigger at all.** An audit trail a compromised application can rewrite is not evidence, and evidence is the product.

---

## 11. Dealers see billing, never compliance data

**Decision.** The dealer role bundle contains no compliance, document or financial permission. Access requires an explicit, time-boxed, client-approved grant, modelled as an engagement like any other.

**Why.** Widening later is easy; narrowing later is a breach notification. This is the difference between a trusted platform and a headline.

---

## 12. crispy-forms renders fields; django-cotton renders everything else

**Decision.** A hard boundary, settled before either was used.

**Why.** Both libraries want to own rendering. Ambiguity here produces two design systems inside one product, and the seam is visible to users within a month.

---

# Open questions

Things that need a decision from the product owner before the module they affect is built.

### Before the engine milestone

1. **Retroactive fact changes.** Turnover for FY 2025-26 is only known once books close, and is frequently restated. A restatement produces *back-dated* obligations. `EntityFactValue` stores the history; the policy — flag and route to a human, or auto-apply — is undecided. Getting this wrong means confidently telling a client they had no obligation they in fact had.
2. **Catalog publish blast radius.** A bad rule reaches every tenant at once. Proposal: staged rollout (internal → 1% → all), auto-apply only for additive and date-neutral changes, human review for anything that would supersede an existing instance.

### Before the notices milestone

3. **Government portal scraping — decided: no scraping in v1.** Automated retrieval from the income tax, GST and MCA portals would require holding client credentials and driving a session against terms of service that do not contemplate it. Both are legal exposures, not technical ones, and neither is worth taking before there is a product to protect.

   The notices module therefore shipped with **manual entry**, and a documented adapter interface with no implementation behind it (`stacos/notices/portals.py`). A test asserts the registry is empty, so the day an adapter appears is the day somebody has to justify it. Email ingestion reuses the same `ingest_retrieved` path and is not yet wired to a mailbox. That interface is the whole point: it keeps the decision reversible, so that a written legal position — or an official API, which is the outcome actually worth waiting for — turns into an adapter rather than a rewrite. The pricing page already says "notice auto-retrieval, where the authority permits it", which is the honest formulation and should stay that way.

### Before launch

4. **WhatsApp template approval.** Business-initiated messages require templates pre-approved by Meta, and one-time passcodes must use the **AUTHENTICATION** category — Meta rejects OTPs sent through marketing or utility templates. Approval is per WhatsApp Business Account and takes days to weeks, so it **should start now**, in parallel with development, not when the code is ready to test. It blocks exactly the way TRAI DLT registration blocks SMS. The three templates STACOS needs are named in `stacos/accounts/whatsapp.py`: `stacos_registration_code`, `stacos_login_code`, `stacos_step_up_code`.
5. **Apple Sign in client secret expiry.** It is a JWT valid for at most six months and needs a rotation job. Everyone forgets this and it fails at 3 a.m. six months after launch.
6. **Passkeys.** django-allauth ≥65 ships WebAuthn. A one-time code delivered to a device is phishable whatever the channel; passkeys are not, cost nothing per authentication, and Indian professionals on Windows Hello can use them today. Worth considering as an *additional* factor even though the dual-channel requirement stands.

7. **A fallback for numbers not on WhatsApp — decided: WhatsApp is a hard requirement, stated plainly.** No SMS fallback in v1. Adding one means TRAI DLT template registration, which takes weeks and buys a second half-working channel; a single channel that says exactly what it needs is better than two that each work sometimes.

   What shipped with the engine milestone: the registration form already asks for a "WhatsApp number" and says the code goes there. `PendingVerification.phone_unreachable` now persists the `not_on_whatsapp` outcome, and the verification screen tells the user that resending will not help and they need a number with WhatsApp on it. Previously this outcome was only written to the log — which meant the one person who needed to know was the only one who could not see it.

   The decision stays reversible at no cost: `WhatsAppResult.not_on_whatsapp` is still a distinct outcome from a delivery failure, so adding SMS later changes no caller.

### Product shape

7. **Entity merge across tenants.** A CA firm can create a client entity with a GSTIN before the client has an account. When that client later signs up independently and enters the same GSTIN, there are two entity rows for one legal person, in two tenants, both with compliance history. Cross-tenant merge is one of the hardest operations in the system — which tenant owns the merged row, and what happens to the loser's obligations, documents and audit trail? A duplicate detector exists (`entityreg_type_value_idx`). The merge flow should either be designed properly or declared out of scope with a manual support runbook. It should not be discovered in month six.
8. **DPDP Act and pre-consent data.** The same flow means a practice enters someone's PAN and GSTIN before that person has consented. The platform's role — processor or fiduciary — needs a written position.
9. **`django-waffle` is not tenant-aware.** Its flags are global, per-user or per-group; a B2B SaaS needs per-tenant rollout. Solvable with `WAFFLE_FLAG_MODEL` and a custom flag model, but worth knowing before the first staged rollout rather than during it.
