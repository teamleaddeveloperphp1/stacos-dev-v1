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

__all__ = ["OWNER_ROLE_CODES", "provision_tenant", "record_fact", "unique_slug"]

#: The system role that owns a newly created tenant, per tenant type.
OWNER_ROLE_CODES = {
    Tenant.Type.ORGANISATION: "org-owner",
    Tenant.Type.PRACTICE: "practice-partner",
    Tenant.Type.DEALER: "dealer-principal",
}


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
    role = Role.objects.filter(
        code=role_code, tenant__isnull=True, tenant_type=tenant_type
    ).first()
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

    Mirrors ``onboarding.services._write_fact_history``, which does the same for
    a whole draft at once.
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
    from stacos.tenancy.onboarding.services import _PROFILE_COLUMNS

    return _PROFILE_COLUMNS


def _current_value(profile: EntityProfile, key: str) -> Any:
    if key in _profile_column_names():
        return getattr(profile, key, None)
    return (profile.facts or {}).get(key)
