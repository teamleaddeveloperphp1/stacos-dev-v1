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
    """Load reference data once, after the test database is created.

    System roles, the India jurisdiction pack and the whole compliance catalog.
    All three are platform-owned reference data that every test can read and none
    should be writing, so loading them once at setup — before the per-test
    transaction opens — is both faster and more honest than a fixture that
    rebuilds them.

    Testing against the **live** catalog rather than a hand-built miniature is
    deliberate for the materialisation suite: a definition whose rule stops
    matching any real entity is a bug this catches and a fixture would not.
    """
    with django_db_blocker.unblock():
        call_command("sync_system_roles", verbosity=0)
        call_command("loadpack", "IN", verbosity=0)
        call_command("loadcatalog", verbosity=0)


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
    """The India pack, as loaded from ``catalog/packs/IN.yaml``.

    Fetched rather than constructed, so tests run against the fiscal year,
    weekend rule and holiday calendar the product actually ships. A hand-built
    pack here would let the real one drift without anything noticing — and
    ``country`` is unique, so constructing a second one would fail anyway.

    Self-healing for transactional tests, which truncate the tables the
    session-scoped setup populated.
    """
    pack = JurisdictionPack.objects.filter(country="IN").first()
    if pack is None:
        call_command("loadpack", "IN", verbosity=0)
        pack = JurisdictionPack.objects.get(country="IN")
    return pack


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
# The obligation register
#
# Shared with the security suite as well as the obligations suite, which is why
# these live here rather than in a directory-local conftest: "can this tenant
# reach that tenant's calendar" is an isolation question, and it needs the same
# fixture the feature tests use rather than an approximation of it.
# ---------------------------------------------------------------------------

#: Fixed. A calendar test whose expectations move with the wall clock is a
#: calendar test that gets switched off in March.
AS_OF = date(2026, 8, 12)


@pytest.fixture
def manufacturer(org: Tenant) -> Entity:
    """A Gujarat manufacturer with GST in two states and one factory.

    Deliberately realistic rather than minimal: enough profile that a meaningful
    slice of the live catalog applies, and two GSTINs so the per-registration
    fan-out is actually exercised. A one-state fixture would pass every test in
    the suite while proving nothing about the behaviour that matters most.
    """
    from stacos.tenancy.models import EntityPremises, EntityRegistration

    with platform_scope(reason="test-fixture"):
        entity = Entity.objects.create(
            tenant=org,
            name="Shreeji Textiles Pvt Ltd",
            short_code="STPL",
            entity_type="PVT_LTD",
            country="IN",
            incorporation_date=date(2011, 6, 14),
            registered_office_state="IN-GJ",
        )
        EntityProfile.objects.create(
            tenant=org,
            entity=entity,
            aggregate_turnover=Decimal("800000000"),
            employee_count=200,
            contractor_count=45,
            paid_up_capital=Decimal("50000000"),
            net_worth=Decimal("350000000"),
            states_of_operation=["IN-GJ", "IN-MH"],
            facts={
                "gst_scheme": "REGULAR",
                "qrmp_opted": False,
                "women_employees_count": 60,
                "has_boiler": True,
                "has_msme_vendors": True,
                "is_listed": False,
                "has_foreign_shareholding": False,
                "has_ecommerce_sales": False,
                "has_export_import": False,
                "deals_in_hazardous_material": False,
                "is_dormant": False,
                "sector": "Textiles",
            },
        )

        for kind, where in (
            ("PAN", ""),
            ("TAN", ""),
            ("CIN", ""),
            ("GST", "IN-GJ"),
            ("GST", "IN-MH"),
            ("PF", ""),
            ("ESIC", ""),
            ("PT_RC", "IN-GJ"),
            ("PT_EC", "IN-GJ"),
            ("FACTORY_LICENCE", "IN-GJ"),
            ("PCB_CONSENT", "IN-GJ"),
        ):
            EntityRegistration.objects.create(
                tenant=org,
                entity=entity,
                type=kind,
                jurisdiction=where,
                value=f"{kind}{where or 'XX'}TEST",
                label=f"{kind} {where}".strip(),
            )

        EntityPremises.objects.create(
            tenant=org,
            entity=entity,
            name="Ahmedabad Plant",
            type="FACTORY",
            jurisdiction="IN-GJ",
        )
        return entity


@pytest.fixture
def materialised(manufacturer: Entity) -> Entity:
    """The manufacturer with its calendar already built."""
    from stacos.obligations.services import materialise

    with platform_scope(reason="test-fixture"):
        materialise(manufacturer, as_of=AS_OF, trigger="ONBOARDING")
    return manufacturer


@pytest.fixture
def an_obligation(materialised: Entity) -> Any:
    """One GSTR-3B, for any test that needs a concrete row to act on."""
    from stacos.obligations.models import ObligationInstance

    with platform_scope(reason="test-fixture"):
        return (
            ObligationInstance.objects.filter(
                entity=materialised, definition_code="IN-GST-GSTR3B-MONTHLY"
            )
            .order_by("due_date")
            .first()
        )


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


def sign_in(client: Any, user: User, *, step_up: bool = False) -> Any:
    """A verified session for ``user``.

    ``step_up=True`` also marks re-authentication fresh. Sensitive permissions —
    recording a filing, responding to a notice, approving a return — demand it,
    and a test that omits it gets a redirect rather than the action, which is the
    correct behaviour and a confusing failure. Asking for it explicitly keeps the
    step-up requirement visible in the test rather than silently satisfied for
    everything.
    """
    from django.utils import timezone as django_timezone

    from stacos.accounts.middleware import SESSION_VERIFIED_KEY
    from stacos.accounts.stepup import SESSION_KEY as STEP_UP_KEY

    client.force_login(user)
    session = client.session
    session[SESSION_VERIFIED_KEY] = True
    if step_up:
        session[STEP_UP_KEY] = django_timezone.now().isoformat()
    session.save()
    return client


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
