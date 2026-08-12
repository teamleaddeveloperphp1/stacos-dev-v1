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
def _reset_messaging_state() -> Iterator[None]:
    """Clear the outbox and the rate-limit counters between tests.

    The cache is not reset by pytest-django, so throttle state leaks forward:
    every test signs in from 127.0.0.1, and after twenty verification sends the
    per-IP hourly limit starts silently refusing to send. The symptom is a later
    test failing with an empty outbox while passing perfectly well on its own —
    so this is a correctness fixture, not tidiness.
    """
    from django.core.cache import cache

    from stacos.accounts.whatsapp import MemoryWhatsAppProvider

    MemoryWhatsAppProvider.clear()
    cache.clear()
    yield
    MemoryWhatsAppProvider.clear()
    cache.clear()


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup: Any, django_db_blocker: Any) -> None:
    """Load the system roles once, after the test database is created."""
    with django_db_blocker.unblock():
        call_command("sync_system_roles", verbosity=0)


@pytest.fixture
def platform(db: Any) -> Iterator[AccessScope]:
    """An explicitly held platform scope, for tests that need one.

    Data fixtures deliberately do **not** use this. ``platform_scope`` sets
    PostgreSQL's ``stacos.bypass_rls`` flag, and a fixture that yields from
    inside it holds that flag on for the entire test — which would silently
    disable Row-Level Security in exactly the tests written to prove it works.
    Fixtures below open and close the scope around their own writes instead.
    """
    with platform_scope(reason="test-fixture") as scope:
        yield scope


# ---------------------------------------------------------------------------
# Jurisdiction
# ---------------------------------------------------------------------------


@pytest.fixture
def india(db: Any) -> JurisdictionPack:
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
def org(india: JurisdictionPack) -> Tenant:
    return Tenant.objects.create(
        type=Tenant.Type.ORGANISATION,
        name="Acme Manufacturing",
        slug="acme",
        status=Tenant.Status.ACTIVE,
        jurisdiction_pack=india,
    )


@pytest.fixture
def other_org(india: JurisdictionPack) -> Tenant:
    """A completely unrelated customer. The one that must never be reachable."""
    return Tenant.objects.create(
        type=Tenant.Type.ORGANISATION,
        name="Rival Industries",
        slug="rival",
        status=Tenant.Status.ACTIVE,
        jurisdiction_pack=india,
    )


@pytest.fixture
def practice(india: JurisdictionPack) -> Tenant:
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
    """Create an entity and its profile inside a short-lived platform scope.

    Opened and closed here rather than held by a fixture, so the RLS bypass is
    off again by the time the test body runs.
    """
    with platform_scope(reason="test-fixture"):
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
def entity_a(org: Tenant) -> Entity:
    """The entity the practice IS engaged on."""
    return _make_entity(org, "Acme Textiles Pvt Ltd", "ATPL")


@pytest.fixture
def entity_b(org: Tenant) -> Entity:
    """A sibling entity in the same tenant that the practice is NOT engaged on."""
    return _make_entity(org, "Acme Logistics Pvt Ltd", "ALPL")


@pytest.fixture
def rival_entity(other_org: Tenant) -> Entity:
    return _make_entity(other_org, "Rival Steel Pvt Ltd", "RSPL")


# ---------------------------------------------------------------------------
# Users and memberships
# ---------------------------------------------------------------------------


def _system_role(code: str, tenant_type: str) -> Role:
    """Fetch a system role, loading the catalogue if it is missing.

    Self-healing because transactional tests truncate every table, including the
    roles loaded once at database setup.
    """
    role = Role.objects.filter(tenant__isnull=True, code=code, tenant_type=tenant_type).first()
    if role is None:
        call_command("sync_system_roles", verbosity=0)
        role = Role.objects.get(tenant__isnull=True, code=code, tenant_type=tenant_type)
    return role


def _make_member(tenant: Tenant, email: str, name: str, phone: str, role_code: str) -> User:
    with platform_scope(reason="test-fixture"):
        user = User.objects.create_user(
            email=email,
            password="test-password-12345",
            full_name=name,
            phone_e164=phone,
            email_verified=True,
            phone_verified=True,
        )
        Membership.objects.create(
            tenant=tenant,
            user=user,
            role=_system_role(role_code, tenant.type),
            status=Membership.Status.ACTIVE,
        )
        return user


@pytest.fixture
def org_owner(org: Tenant) -> User:
    return _make_member(org, "owner@acme.example", "Anita Rao", "+919800000001", "org-owner")


@pytest.fixture
def rival_owner(other_org: Tenant) -> User:
    return _make_member(
        other_org, "owner@rival.example", "Vikram Singh", "+919800000002", "org-owner"
    )


@pytest.fixture
def practice_staff(practice: Tenant) -> User:
    return _make_member(
        practice, "staff@mehta.example", "Nikhil Mehta", "+919800000003", "practice-staff"
    )


@pytest.fixture
def practice_membership(practice_staff: User, practice: Tenant) -> Membership:
    """Fetch the membership the scope resolver will be asked about.

    Needs a platform scope even though it uses ``objects_unscoped``: that manager
    bypasses the *application* filter, but Row-Level Security still applies at
    the database and returns nothing with no tenant bound. Both layers have to be
    satisfied, which is the whole point of having two.
    """
    with platform_scope(reason="test-fixture"):
        return Membership.objects_unscoped.select_related("tenant", "role").get(
            user=practice_staff, tenant=practice
        )


# ---------------------------------------------------------------------------
# The engagement — scoped to entity_a only, tax categories only
# ---------------------------------------------------------------------------


@pytest.fixture
def engagement(practice: Tenant, entity_a: Entity) -> Engagement:
    with platform_scope(reason="test-fixture"):
        return _make_engagement(practice, entity_a)


def _make_engagement(practice: Tenant, entity_a: Entity) -> Engagement:
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
