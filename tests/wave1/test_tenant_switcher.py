"""
The tenant switcher must list the organisations the user actually belongs to.

It told every user, however many organisations they belonged to, that they were
"not a member of any organisation yet". The shell passed ``nav_memberships`` and
nothing anywhere set it, so the variable resolved to the empty string and the
``{% empty %}`` branch fired every time.

The list cannot come from the scoped ``Membership`` manager: ``member_tenant_ids``
holds only the tenant currently switched *to*, so that would answer with the one
organisation the user is already looking at — which is not a switcher. It comes
from the bootstrap read the scope resolver has to perform anyway.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.tenancy.models import Membership, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    return sign_in(client, org_owner, step_up=True)


def test_a_member_sees_their_organisation_not_the_empty_state(
    signed_in: Client, org: Tenant
) -> None:
    response = signed_in.get(reverse("app:dashboard"))
    body = response.content.decode()

    assert org.name in body
    assert "You are not a member of any organisation yet." not in body


def test_every_membership_is_offered_not_only_the_current_one(
    client: Client, org_owner: User, org: Tenant, other_org: Tenant
) -> None:
    """The point of a switcher.

    Filtering through the scoped manager would pass the test above and fail this
    one, because the scope names only the tenant being viewed.
    """
    with platform_scope(reason="test-fixture"):
        role = Membership.objects.filter(user=org_owner).first()
        assert role is not None
        Membership.objects.create(
            tenant=other_org,
            user=org_owner,
            role=role.role,
            status=Membership.Status.ACTIVE,
        )

    signed_in = sign_in(client, org_owner, step_up=True)
    body = signed_in.get(reverse("app:dashboard")).content.decode()

    assert org.name in body
    assert other_org.name in body, "the switcher offers only the tenant already in view"


def test_a_suspended_membership_is_not_offered(
    client: Client, org_owner: User, org: Tenant, other_org: Tenant
) -> None:
    with platform_scope(reason="test-fixture"):
        role = Membership.objects.filter(user=org_owner).first()
        assert role is not None
        Membership.objects.create(
            tenant=other_org,
            user=org_owner,
            role=role.role,
            status=Membership.Status.SUSPENDED,
        )

    signed_in = sign_in(client, org_owner, step_up=True)
    body = signed_in.get(reverse("app:dashboard")).content.decode()

    assert org.name in body
    assert other_org.name not in body, "a suspended membership was offered for switching"


def test_the_switcher_costs_no_extra_query(signed_in: Client, django_assert_num_queries) -> None:
    """It reuses the resolver's bootstrap read rather than issuing its own.

    A context processor runs on every render including every fragment, so a query
    added here is a query on every interaction in the product.
    """
    baseline = signed_in.get(reverse("app:dashboard"))
    assert baseline.status_code == 200

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        signed_in.get(reverse("app:dashboard"))

    membership_reads = [
        query
        for query in captured.captured_queries
        if "tenancy_membership" in query["sql"] and "SELECT" in query["sql"]
    ]
    assert len(membership_reads) <= 1, (
        f"the switcher issued its own membership query: {len(membership_reads)} reads"
    )


# ---------------------------------------------------------------------------
# Actually switching
#
# Listing the organisations was only half of it. Choosing one silently failed:
# `switch_tenant` read the target membership through `objects_unscoped`, which
# lifts the ORM's tenant filter and nothing else, while PostgreSQL's Row-Level
# Security was still pointed at the tenant the user was already in. The row for
# the tenant being switched *to* is by definition outside that set, so the lookup
# found nothing and told a genuine member of both that they were not a member of
# the second. `rls_bootstrap` is what the login-time listing has always used.
# ---------------------------------------------------------------------------


def _also_a_member_of(user: User, tenant: Tenant, status: str = Membership.Status.ACTIVE) -> None:
    with platform_scope(reason="test-fixture"):
        existing = Membership.objects_unscoped.filter(user=user).first()
        assert existing is not None
        Membership.objects.create(
            tenant=tenant, user=user, role=existing.role, status=status
        )


def test_switching_to_another_organisation_actually_switches(
    client: Client, org_owner: User, org: Tenant, other_org: Tenant
) -> None:
    _also_a_member_of(org_owner, other_org)
    signed_in = sign_in(client, org_owner)

    # Start in the first organisation, which is the one the resolver falls back to.
    assert signed_in.get(reverse("app:dashboard")).wsgi_request.tenant == org

    response = signed_in.post(reverse("app:switch_tenant"), {"tenant_id": str(other_org.id)})
    assert response.status_code == 302

    landed = signed_in.get(reverse("app:dashboard"))
    assert landed.wsgi_request.tenant == other_org, "the switch bounced back to the current tenant"


def test_switching_over_htmx_tells_the_browser_to_navigate(
    client: Client, org_owner: User, other_org: Tenant
) -> None:
    """A tenant switch changes every part of the shell, so it is a navigation."""
    _also_a_member_of(org_owner, other_org)
    signed_in = sign_in(client, org_owner)

    response = signed_in.post(
        reverse("app:switch_tenant"),
        {"tenant_id": str(other_org.id)},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 204
    assert response["HX-Redirect"] == "/app/"


def test_switching_to_a_suspended_membership_is_refused(
    client: Client, org_owner: User, org: Tenant, other_org: Tenant
) -> None:
    """The bootstrap lifts RLS; it does not lift the status filter."""
    _also_a_member_of(org_owner, other_org, status=Membership.Status.SUSPENDED)
    signed_in = sign_in(client, org_owner)

    signed_in.post(reverse("app:switch_tenant"), {"tenant_id": str(other_org.id)})

    assert signed_in.get(reverse("app:dashboard")).wsgi_request.tenant == org


def test_switching_to_a_tenant_you_are_not_in_is_refused(
    client: Client, org_owner: User, org: Tenant, other_org: Tenant
) -> None:
    """A forged id in the form. The lookup is anchored on `user`, so it finds nothing."""
    signed_in = sign_in(client, org_owner)

    signed_in.post(reverse("app:switch_tenant"), {"tenant_id": str(other_org.id)})

    assert signed_in.get(reverse("app:dashboard")).wsgi_request.tenant == org
