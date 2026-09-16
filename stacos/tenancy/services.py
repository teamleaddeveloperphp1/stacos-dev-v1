"""
Creating an organisation, and recording a fact about one.

Two operations that used to exist only inside the onboarding wizard, extracted
because two other callers now need them and a second implementation of either is
how tenants start appearing without a jurisdiction pack.

:func:`provision_tenant` is the *only* way a ``Tenant`` and its owner
``Membership`` come into existence. It refuses to create one without a
:class:`~stacos.jurisdictions.models.JurisdictionPack`: entity types, tax
identifier validators, fiscal-year boundaries and every due date are read from
the pack, so a tenant without one is a workspace where nothing downstream can be
computed — and the failure surfaces days later as an empty calendar rather than
here as an error.

:func:`record_fact` writes one profile answer for an entity that already exists.
The onboarding wizard collects answers into a session draft and writes them all
at commit; everything after onboarding — confirming an obligation, correcting a
turnover — has exactly one answer and a saved entity, which is this.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from stacos.accounts.models import User
from stacos.core.audit import AuditAction, record_event
from stacos.core.scope import tenant_context
from stacos.jurisdictions.facts import REGISTRY
from stacos.jurisdictions.models import JurisdictionPack
from stacos.tenancy.models import (
    Entity,
    EntityFactValue,
    EntityProfile,
    Membership,
    Role,
    Tenant,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "OWNER_ROLE_CODES",
    "PROVISIONAL_NAME_KEY",
    "default_workspace_name",
    "name_is_provisional",
    "provision_tenant",
    "record_fact",
    "rename_tenant",
    "unique_slug",
]

#: Profile facts that have their own typed column rather than living in the
#: ``facts`` JSONB. ``women_employees_count`` and ``net_profit`` are registered,
#: askable facts (``jurisdictions/facts.py``) with no matching column on
#: ``EntityProfile`` — passing either to ``setattr(profile, key, value)`` would
#: raise. Every other caller that builds an ``EntityProfile`` (the
#: ``manufacturer`` test fixture, ``seed_dev``, ``catalog.personas``) already
#: puts both in ``facts``, never as a column — this is the one place that
#: distinction is decided.
_PROFILE_COLUMNS = frozenset(
    {
        "aggregate_turnover",
        "employee_count",
        "contractor_count",
        "paid_up_capital",
        "net_worth",
        "nic_code",
        "sector",
        "sub_sector",
    }
)

#: The system role that owns a newly created tenant, per tenant type.
#:
#: Keyed by the plain string rather than the enum member, because callers pass
#: `Tenant.Type.ORGANISATION` (which *is* a str) and mypy will not accept a
#: `str` index into a dict keyed by the enum.
OWNER_ROLE_CODES: dict[str, str] = {
    Tenant.Type.ORGANISATION: "org-owner",
    Tenant.Type.PRACTICE: "practice-partner",
    Tenant.Type.DEALER: "dealer-principal",
}


#: Marks a tenant whose name nobody has chosen — see
#: :func:`default_workspace_name`. Lives in ``Tenant.settings`` rather than as a
#: column because it is a fact about onboarding that stops being true within a
#: day or two of the account existing, and a boolean column would outlive its
#: usefulness by years.
PROVISIONAL_NAME_KEY = "name_is_provisional"


def default_workspace_name(owner: User) -> str:
    """What to call a workspace nobody has named yet.

    Sign-up asks for a person, not a business. That is the right trade — the one
    thing a user cannot answer at the sign-up screen is what the product will do
    with "organisation name", and asking anyway turns a thirty-second form into a
    decision — but a tenant still needs *a* name, because it is what the sidebar,
    the audit log and every invitation email say.

    The person's own name is the honest answer, and it is also the answer that
    reads correctly for the largest group of users this product has: one person,
    one business, no distinction they care about. It is replaced the moment
    somebody says otherwise (``rename_tenant``), and
    :func:`name_is_provisional` is what lets the dashboard ask.
    """
    name = (owner.full_name or "").strip()
    if not name:
        # No name at all only happens for an account created outside sign-up —
        # a fixture, an import, a social sign-in that returned nothing useful.
        # The local part of the email is still better than "Untitled".
        name = owner.email.partition("@")[0].replace(".", " ").strip().title()
    return (name or "My workspace")[:200]


def name_is_provisional(tenant: Tenant) -> bool:
    """True while the workspace is still wearing the name we picked for it."""
    return bool((tenant.settings or {}).get(PROVISIONAL_NAME_KEY))


def unique_slug(name: str) -> str:
    """A URL-safe slug for ``name`` that no tenant is already using."""
    base = slugify(name)[:50] or "organisation"
    slug = base
    suffix = 2
    while Tenant.objects.filter(slug=slug).exists():
        slug = f"{base}-{suffix}"[:60]
        suffix += 1
    return slug


@transaction.atomic
def provision_tenant(
    name: str,
    *,
    owner: User,
    country: str = "IN",
    tenant_type: str = Tenant.Type.ORGANISATION,
    reason: str = "provision",
    name_provisional: bool = False,
) -> Tenant:
    """Create a tenant with a jurisdiction pack, and make ``owner`` its owner.

    Raises rather than degrading. Both failure modes here — no pack for the
    country, no synced system roles — produce a workspace that looks fine and
    does nothing, and a user who can sign in and reach no feature is far more
    expensive to diagnose than an error at the moment of creation.
    """
    pack = JurisdictionPack.objects.filter(country=country).first()
    if pack is None:
        raise RuntimeError(
            f"No JurisdictionPack for country {country!r}. "
            f"Run manage.py loadpack {country} before creating a tenant there."
        )

    role_code = OWNER_ROLE_CODES[tenant_type]
    role = Role.objects.filter(code=role_code, tenant__isnull=True, tenant_type=tenant_type).first()
    if role is None:
        # Loud, not silent. A membership with no role is a user who can sign in
        # and do nothing, and the cause would be invisible from the symptom.
        raise RuntimeError(
            f"Missing system role {role_code!r}. Run manage.py sync_system_roles first."
        )

    tenant = Tenant.objects.create(
        name=name,
        slug=unique_slug(name),
        type=tenant_type,
        status=Tenant.Status.TRIAL,
        country=country,
        jurisdiction_pack=pack,
        settings={PROVISIONAL_NAME_KEY: True} if name_provisional else {},
    )

    # Not platform_scope: that would need an allowlist entry and write a bypass
    # audit row for a tenant this user is about to own outright.
    with tenant_context(tenant_ids={tenant.id}, reason=reason):
        Membership.objects.create(
            tenant=tenant,
            user=owner,
            role=role,
            status=Membership.Status.ACTIVE,
            all_entities=True,
            joined_at=timezone.now(),
        )
        record_event(action=AuditAction.CREATE, actor=owner, obj=tenant)

    logger.info(
        "tenancy.provisioned",
        tenant_id=str(tenant.id),
        owner_id=str(owner.pk),
        country=country,
        pack=pack.code if hasattr(pack, "code") else country,
    )
    return tenant


@transaction.atomic
def rename_tenant(tenant: Tenant, name: str, *, actor: User | None) -> Tenant:
    """Give the workspace the name its owner actually wants.

    The slug is deliberately **not** regenerated. It is the tenant's stable
    identifier — it appears in support conversations and, the moment subdomains
    or exports exist, in addresses people have saved — and rotating it because
    somebody fixed a spelling is how a link goes dead for a reason nobody can
    reconstruct afterwards.

    Audited like any other state change (``CLAUDE.md`` rule 6): "who renamed the
    organisation, and from what" is exactly the sort of question a due-diligence
    reviewer asks.
    """
    before = {"name": tenant.name}
    settings_after = dict(tenant.settings or {})
    settings_after.pop(PROVISIONAL_NAME_KEY, None)

    tenant.name = name.strip()[:200]
    tenant.settings = settings_after
    tenant.save(update_fields=["name", "settings", "updated_at"])

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=tenant,
        before=before,
        after={"name": tenant.name},
    )
    return tenant


def record_fact(
    entity: Entity,
    key: str,
    value: Any,
    *,
    actor: User | None,
    as_of: date,
) -> None:
    """Record one profile answer for a saved entity, both places it belongs.

    Two writes, deliberately. The typed column or the JSONB blob on
    :class:`EntityProfile` is what the planner reads *now*; an
    :class:`EntityFactValue` row is what makes the answer effective-dated, and
    ``build_profile_view`` layers those over the profile. Writing only the
    profile makes today's answer silently true for every period that ever was —
    the failure ``EntityFactValue`` exists to prevent — and writing only the fact
    row leaves the current view unchanged for a fact that is not effective-dated.

    """
    profile = EntityProfile.objects.filter(entity=entity).first()
    if profile is None:
        profile = EntityProfile.objects.create(tenant_id=entity.tenant_id, entity=entity)

    before = {key: _current_value(profile, key)}

    if hasattr(profile, key) and key in _profile_column_names():
        setattr(profile, key, value)
        profile.save(update_fields=[key, "updated_at"])
    else:
        facts = dict(profile.facts or {})
        facts[key] = value
        profile.facts = facts
        profile.save(update_fields=["facts", "updated_at"])

    if key in REGISTRY.effective_dated_keys() and value not in (None, ""):
        # The financial year the answer is a statement about, on the same rule
        # the wizard uses: a turnover given today describes the current year.
        year_start = date(as_of.year if as_of.month >= 4 else as_of.year - 1, 4, 1)
        EntityFactValue.objects.create(
            tenant_id=entity.tenant_id,
            entity=entity,
            key=key,
            value=value,
            valid_from=year_start,
            source="USER",
            recorded_by=actor,
        )

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=profile,
        before=before,
        after={key: value},
        context={"fact": key},
    )


def _profile_column_names() -> frozenset[str]:
    return _PROFILE_COLUMNS


def _current_value(profile: EntityProfile, key: str) -> Any:
    if key in _profile_column_names():
        return getattr(profile, key, None)
    return (profile.facts or {}).get(key)
