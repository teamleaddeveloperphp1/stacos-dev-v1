"""
Where a signed-in user with no usable workspace ends up.

STACOS provisions an organisation at sign-up now — see
``tests/test_registration_provisioning.py`` — so "authenticated, verified, and a
member of nothing" is a rare state rather than the ordinary one right after
sign-up. It still has to be answered honestly rather than with a 403 that reads
as a bug, and a brand-new organisation with no entity yet is now the ordinary
state for a few seconds, so that gets its own answer too.

``OrganisationGateMiddleware`` decides all of this once, for every page
including the ones nobody has written yet. Two things have to be true of it at
the same time, and only one of them is about the redirect:

* the user is taken somewhere useful, or told plainly why they cannot be, and
* **nothing has been widened**. A redirect looks exactly the same whether or not
  access leaked, so the tests below assert on the resolved scope and on what the
  scope can reach, not on the status code alone.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.tenancy.models import Entity, Membership, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}

#: Deliberately across several apps. The requirement is that the decision holds
#: everywhere, so testing the dashboard alone would prove the thing that was
#: already easy.
APP_PAGES = [
    "/app/",
    "/app/entities/",
    "/app/compliance/",
    "/app/requests/",
    "/app/documents/",
    "/app/notices/",
    "/app/returns/",
]


@pytest.fixture
def newcomer(db: object) -> User:
    """Verified, signed in, and a member of nothing.

    Not the ordinary path any more — sign-up provisions a tenant — but still
    reachable: an account created outside the ordinary flow, or one whose only
    invitation has not been accepted.
    """
    return User.objects.create_user(
        email="newcomer@example.com",
        password="correct-horse-battery",
        first_name="Asha",
        last_name="Founder",
        phone_e164="+919800000101",
        email_verified=True,
        phone_verified=True,
    )


@pytest.fixture
def stranger(client: Client, newcomer: User) -> Client:
    return sign_in(client, newcomer)


@pytest.fixture
def suspended_user(db: object, org: Tenant, org_owner: User) -> User:
    """A member of one organisation whose membership has been suspended."""
    user = User.objects.create_user(
        email="suspended@example.com",
        password="correct-horse-battery",
        first_name="Ravi",
        last_name="Suspended",
        phone_e164="+919800000102",
        email_verified=True,
        phone_verified=True,
    )
    with platform_scope(reason="test-fixture"):
        existing = Membership.objects_unscoped.filter(user=org_owner).first()
        assert existing is not None
        Membership.objects.create(
            tenant=org,
            user=user,
            role=existing.role,
            status=Membership.Status.SUSPENDED,
        )
    return user


# ---------------------------------------------------------------------------
# No membership at all — told plainly, everywhere
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", APP_PAGES)
def test_every_app_page_tells_a_member_less_user_there_is_no_organisation(
    stranger: Client, path: str
) -> None:
    response = stranger.get(path)

    assert response.status_code == 403, f"{path} did not answer with the no-organisation screen"
    assert "no organisation" in response.content.decode().lower()


def test_a_boosted_click_still_gets_the_full_screen(stranger: Client) -> None:
    """A 403 body swapped into ``#main`` would render the refusal inside a shell

    this session is not entitled to. ``navigate()`` to the same URL is what
    forces a full document reload, the same trick ``_suspended`` uses.
    """
    response = stranger.get("/app/compliance/", headers=HTMX)

    assert response.status_code == 204
    assert response["HX-Redirect"] == "/app/compliance/"
    assert not response.content, "an HX-Redirect response must carry no body to swap"


def test_a_fragment_endpoint_is_gated_too(stranger: Client) -> None:
    """An HTMX fragment URL is a real, directly reachable view."""
    response = stranger.get(reverse("notifications:panel"), headers=HTMX)

    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("notifications:panel")


# ---------------------------------------------------------------------------
# Nothing was widened
# ---------------------------------------------------------------------------


def test_the_scope_is_empty(stranger: Client) -> None:
    """The proof that matters, and a status code alone cannot give it.

    There is no self-service route left for a member-less user to reach, so
    unlike the old setup flow, this grants nothing at all.
    """
    scope = stranger.get("/app/entities/").wsgi_request.access_scope

    assert scope is not None
    assert scope.permissions == frozenset()
    assert scope.principal_tenant_id is None
    assert scope.readable_tenant_ids == frozenset()
    assert scope.writable_tenant_ids == frozenset()


def test_no_organisations_rows_are_reachable(stranger: Client, entity_a: Entity) -> None:
    """A member of nothing reaches nobody's data."""
    detail = stranger.get(reverse("app:entity_detail", args=[entity_a.pk]))
    assert detail.status_code == 403, "the gate fires before the view"

    scope = stranger.get("/app/").wsgi_request.access_scope
    assert scope is not None
    assert not scope.can_read(entity_a.tenant_id)

    with platform_scope(reason="test"):
        assert Entity.objects.filter(pk=entity_a.pk).exists(), "fixture sanity"


# ---------------------------------------------------------------------------
# No loop, and no trap
# ---------------------------------------------------------------------------


def test_signing_out_is_not_blocked_by_the_gate(stranger: Client) -> None:
    """Being told there is no organisation must not trap somebody in it."""
    response = stranger.post(reverse("accounts:logout"))

    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:login")


def test_marketing_pages_are_untouched(stranger: Client) -> None:
    assert stranger.get("/").status_code == 200


# ---------------------------------------------------------------------------
# A tenant with no entity yet — sent to add one, everywhere, with one
# carve-out for the page being redirected to
# ---------------------------------------------------------------------------


def test_a_member_with_no_entity_yet_is_sent_to_add_one(client: Client, org_owner: User) -> None:
    signed_in = sign_in(client, org_owner)

    response = signed_in.get("/app/")

    assert response.status_code == 302
    assert response["Location"] == reverse("app:entity_create")


def test_the_entity_create_page_itself_stays_reachable(client: Client, org_owner: User) -> None:
    """The one carve-out once a tenant exists. Without it the redirect is a loop."""
    signed_in = sign_in(client, org_owner)

    response = signed_in.get(reverse("app:entity_create"))

    assert response.status_code == 200


def test_a_member_with_an_entity_lands_on_the_dashboard(
    client: Client, org_owner: User, entity_a: Entity
) -> None:
    signed_in = sign_in(client, org_owner)

    response = signed_in.get("/app/")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Suspended: a different answer, on purpose
# ---------------------------------------------------------------------------


def test_a_suspended_member_is_told_so_rather_than_sent_anywhere_else(
    client: Client, suspended_user: User, org: Tenant
) -> None:
    """Suspension is a decision somebody took. It is not an invitation to add an entity."""
    signed_in = sign_in(client, suspended_user)

    response = signed_in.get("/app/")
    body = response.content.decode()

    assert response.status_code == 403
    assert "suspended" in body.lower()
    assert org.name in body
    assert reverse("app:entity_create") not in body, "an escape route past the suspension"


def test_a_suspended_member_can_still_sign_out(client: Client, suspended_user: User) -> None:
    signed_in = sign_in(client, suspended_user)

    assert signed_in.post(reverse("accounts:logout")).status_code == 302


def test_a_suspended_member_cannot_reach_entity_create_either(
    client: Client, suspended_user: User
) -> None:
    """The carve-out that exists for a healthy, entity-less tenant does not apply
    to a suspended one — suspension is checked first, unconditionally."""
    signed_in = sign_in(client, suspended_user)

    response = signed_in.get(reverse("app:entity_create"))

    assert response.status_code == 403
    scope = response.wsgi_request.access_scope
    assert scope is not None
    assert scope.permissions == frozenset(), "a suspended user was handed a permission"


def test_a_suspended_member_reaches_no_data(client: Client, suspended_user: User) -> None:
    signed_in = sign_in(client, suspended_user)

    scope = signed_in.get("/app/").wsgi_request.access_scope

    assert scope is not None
    assert scope.permissions == frozenset()
    assert scope.principal_tenant_id is None
    assert scope.readable_tenant_ids == frozenset()


def test_suspension_in_one_organisation_does_not_affect_another(
    client: Client, suspended_user: User, other_org: Tenant, org_owner: User
) -> None:
    """A user suspended from one organisation and active in another is simply a
    member of the second. Revoking their access there would be a bug of its own."""
    with platform_scope(reason="test-fixture"):
        existing = Membership.objects_unscoped.filter(user=org_owner).first()
        assert existing is not None
        Membership.objects.create(
            tenant=other_org,
            user=suspended_user,
            role=existing.role,
            status=Membership.Status.ACTIVE,
        )

    signed_in = sign_in(client, suspended_user)
    response = signed_in.get("/app/")

    # `other_org` owns no entity in this test, so the second organisation's own
    # gate — not a refusal — is what fires next.
    assert response.status_code == 302
    assert response["Location"] == reverse("app:entity_create")
    assert response.wsgi_request.tenant == other_org


def test_an_unaccepted_invitation_is_not_told_it_is_suspended(
    client: Client, org: Tenant, org_owner: User
) -> None:
    """An invitation nobody has accepted is not a revocation.

    Such a user has no active membership either, and lumping the two together
    would show somebody who has done nothing wrong the suspension screen.
    """
    invitee = User.objects.create_user(
        email="invited@example.com",
        password="correct-horse-battery",
        first_name="Meera",
        last_name="Invited",
        phone_e164="+919800000103",
        email_verified=True,
        phone_verified=True,
    )
    with platform_scope(reason="test-fixture"):
        existing = Membership.objects_unscoped.filter(user=org_owner).first()
        assert existing is not None
        Membership.objects.create(
            tenant=org,
            user=invitee,
            role=existing.role,
            status=Membership.Status.INVITED,
        )

    signed_in = sign_in(client, invitee)
    response = signed_in.get("/app/")
    body = response.content.decode()

    assert response.status_code == 403
    assert "no organisation" in body.lower()
    assert "suspended" not in body.lower()
