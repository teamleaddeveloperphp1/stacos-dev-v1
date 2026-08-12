"""
Shared fixtures.

Two worlds are built once per test that needs them: an organisation with two
entities, and a professional firm engaged on exactly one of them. Almost every
isolation question the product has to answer is some variation of "can the firm
reach the entity it was not engaged on".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command

from stacos.accounts.models import User
from stacos.core.scope import AccessScope, platform_scope, tenant_context
from stacos.engagements.models import Engagement
from stacos.jurisdictions.models import JurisdictionPack
from stacos.tenancy.models import (
    ComplianceCategory,
    Entity,
    EntityProfile,
    Membership,
    Role,
    Tenant,
)


@pytest.fixture(autouse=True)
def _reset_sms_outbox() -> Iterator[None]:
    from stacos.accounts.sms import MemorySmsProvider

    MemorySmsProvider.clear()
    yield
    MemorySmsProvider.clear()


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup: Any, django_db_blocker: Any) -> None:
    """Load the system roles once, after the test database is created."""
    with django_db_blocker.unblock():
        call_command("sync_system_roles", verbosity=0)


@pytest.fixture
def platform(db: Any) -> Iterator[AccessScope]:
    """Unrestricted scope, for building fixtures."""
    with platform_scope(reason="test-fixture") as scope:
        yield scope


# ---------------------------------------------------------------------------
# Jurisdiction
# ---------------------------------------------------------------------------


@pytest.fixture
def india(platform: AccessScope) -> JurisdictionPack:
    return JurisdictionPack.objects.create(
        country="IN",
        name="India",
        currency="INR",
        currency_symbol="₹",
        default_timezone="Asia/Kolkata",
        digit_grouping=JurisdictionPack.DigitGrouping.INDIAN,
        fy_start_month=4,
        fy_start_day=1,
        is_published=True,
    )


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------


@pytest.fixture
def org(platform: AccessScope, india: JurisdictionPack) -> Tenant:
    return Tenant.objects.create(
        type=Tenant.Type.ORGANISATION,
        name="Acme Manufacturing",
        slug="acme",
        status=Tenant.Status.ACTIVE,
        jurisdiction_pack=india,
    )


@pytest.fixture
def other_org(platform: AccessScope, india: JurisdictionPack) -> Tenant:
    """A completely unrelated customer. The one that must never be reachable."""
    return Tenant.objects.create(
        type=Tenant.Type.ORGANISATION,
        name="Rival Industries",
        slug="rival",
        status=Tenant.Status.ACTIVE,
        jurisdiction_pack=india,
    )


@pytest.fixture
def practice(platform: AccessScope, india: JurisdictionPack) -> Tenant:
    return Tenant.objects.create(
        type=Tenant.Type.PRACTICE,
        name="Mehta & Co",
        slug="mehta",
        status=Tenant.Status.ACTIVE,
        jurisdiction_pack=india,
    )


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


def _make_entity(tenant: Tenant, name: str, code: str) -> Entity:
    entity = Entity.objects.create(
        tenant=tenant,
        name=name,
        short_code=code,
        entity_type="PVT_LTD",
        country="IN",
        incorporation_date=date(2015, 4, 1),
        registered_office_state="IN-GJ",
    )
    EntityProfile.objects.create(
        tenant=tenant,
        entity=entity,
        aggregate_turnover=Decimal("120000000"),
        employee_count=90,
        states_of_operation=["IN-GJ"],
        facts={"gst_scheme": "REGULAR", "qrmp_opted": False},
    )
    return entity


@pytest.fixture
def entity_a(platform: AccessScope, org: Tenant) -> Entity:
    """The entity the practice IS engaged on."""
    return _make_entity(org, "Acme Textiles Pvt Ltd", "ATPL")


@pytest.fixture
def entity_b(platform: AccessScope, org: Tenant) -> Entity:
    """A sibling entity in the same tenant that the practice is NOT engaged on."""
    return _make_entity(org, "Acme Logistics Pvt Ltd", "ALPL")


@pytest.fixture
def rival_entity(platform: AccessScope, other_org: Tenant) -> Entity:
    return _make_entity(other_org, "Rival Steel Pvt Ltd", "RSPL")


# ---------------------------------------------------------------------------
# Users and memberships
# ---------------------------------------------------------------------------


def _system_role(code: str, tenant_type: str) -> Role:
    return Role.objects.get(tenant__isnull=True, code=code, tenant_type=tenant_type)


@pytest.fixture
def org_owner(platform: AccessScope, org: Tenant) -> User:
    user = User.objects.create_user(
        email="owner@acme.example",
        password="test-password-12345",
        full_name="Anita Rao",
        phone_e164="+919800000001",
        email_verified=True,
        phone_verified=True,
    )
    Membership.objects.create(
        tenant=org,
        user=user,
        role=_system_role("org-owner", Tenant.Type.ORGANISATION),
        status=Membership.Status.ACTIVE,
    )
    return user


@pytest.fixture
def rival_owner(platform: AccessScope, other_org: Tenant) -> User:
    user = User.objects.create_user(
        email="owner@rival.example",
        password="test-password-12345",
        full_name="Vikram Singh",
        phone_e164="+919800000002",
        email_verified=True,
        phone_verified=True,
    )
    Membership.objects.create(
        tenant=other_org,
        user=user,
        role=_system_role("org-owner", Tenant.Type.ORGANISATION),
        status=Membership.Status.ACTIVE,
    )
    return user


@pytest.fixture
def practice_staff(platform: AccessScope, practice: Tenant) -> User:
    user = User.objects.create_user(
        email="staff@mehta.example",
        password="test-password-12345",
        full_name="Nikhil Mehta",
        phone_e164="+919800000003",
        email_verified=True,
        phone_verified=True,
    )
    Membership.objects.create(
        tenant=practice,
        user=user,
        role=_system_role("practice-staff", Tenant.Type.PRACTICE),
        status=Membership.Status.ACTIVE,
    )
    return user


@pytest.fixture
def practice_membership(practice_staff: User, practice: Tenant) -> Membership:
    return Membership.objects.get(user=practice_staff, tenant=practice)


# ---------------------------------------------------------------------------
# The engagement — scoped to entity_a only, tax categories only
# ---------------------------------------------------------------------------


@pytest.fixture
def engagement(platform: AccessScope, practice: Tenant, entity_a: Entity) -> Engagement:
    return Engagement.objects.create(
        tenant=entity_a.tenant,
        practice_tenant=practice,
        entity=entity_a,
        status=Engagement.Status.ACTIVE,
        initiated_by_side=Engagement.Side.ORGANISATION,
        categories=[ComplianceCategory.TAX_INDIRECT, ComplianceCategory.TAX_DIRECT],
        permissions=["tenancy.entity.view", "tenancy.profile.view"],
        starts_on=date(2024, 4, 1),
    )


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def in_org(org: Tenant) -> Iterator[AccessScope]:
    """Bind a scope as if an organisation user were making a request."""
    with tenant_context(
        tenant_ids=org.id,
        reason="test:org",
        permissions=["tenancy.entity.view", "finance.view"],
    ) as scope:
        yield scope


@pytest.fixture
def in_practice(
    practice_membership: Membership,
    engagement: Engagement,
) -> Iterator[AccessScope]:
    """Bind the scope a practice user would actually receive.

    Built through the real resolver rather than by hand, so the test exercises
    the code that grants cross-tenant access rather than a convenient
    approximation of it.
    """
    from stacos.core.scope import _current, _sync_rls
    from stacos.tenancy.scope_resolver import resolve_scope_for_membership

    scope = resolve_scope_for_membership(practice_membership, reason="test:practice")
    token = _current.set(scope)
    _sync_rls(tenant_ids=scope.readable_tenant_ids)
    try:
        yield scope
    finally:
        _current.reset(token)
        _sync_rls(tenant_ids=frozenset())
