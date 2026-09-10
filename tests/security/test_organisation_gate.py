"""
Where a signed-in user who belongs to no organisation ends up.

Somebody who has just signed up is authenticated, fully verified, and a member
of nothing. Every page under ``/app/`` requires a permission only a member holds,
so the first thing the product said to a brand-new customer was that they were
not allowed in — a 403, on the dashboard, with the way out buried in a sidebar
dropdown nobody was pointed at.

``OrganisationGateMiddleware`` decides this once, for every page including the
ones nobody has written yet. Two things have to be true of it at the same time,
and only one of them is about the redirect:

* the user is taken somewhere useful rather than refused, and
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
    """Verified, signed in, and a member of nothing."""
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
# No membership at all — the setup flow, everywhere
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", APP_PAGES)
def test_every_app_page_sends_a_member_less_user_to_setup(stranger: Client, path: str) -> None:
    response = stranger.get(path)

    assert response.status_code == 302, f"{path} did not redirect"
    assert response["Location"] == reverse("onboarding:identity")


def test_a_boosted_click_tells_the_browser_to_navigate(stranger: Client) -> None:
    """A 302 here is followed by HTMX and swapped into #main.

    The user would see the setup page's markup appear inside the shell of the
    page they were refused from, or — for a fragment endpoint — nothing at all.
    ``HX-Redirect`` is the instruction the browser acts on.
    """
    response = stranger.get("/app/compliance/", headers=HTMX)

    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("onboarding:identity")
    assert not response.content, "an HX-Redirect response must carry no body to swap"


def test_a_fragment_endpoint_is_gated_too(stranger: Client) -> None:
    """An HTMX fragment URL is a real, directly reachable view."""
    response = stranger.get(reverse("notifications:panel"), headers=HTMX)

    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("onboarding:identity")


# ---------------------------------------------------------------------------
# Nothing was widened
# ---------------------------------------------------------------------------


def test_the_scope_is_still_exactly_one_permission(stranger: Client) -> None:
    """The proof that matters, and the one a redirect cannot give.

    ``/app/entities/`` answers with a 302 whether the caller holds one permission
    or every permission in the registry. What must not have changed is the scope
    behind it.
    """
    scope = stranger.get("/app/entities/").wsgi_request.access_scope

    assert scope is not None
    assert scope.permissions == frozenset({"tenancy.onboarding.start"})
    assert scope.principal_tenant_id is None
    assert scope.readable_tenant_ids == frozenset()
    assert scope.writable_tenant_ids == frozenset()


def test_no_organisations_rows_are_reachable(stranger: Client, entity_a: Entity) -> None:
    """A member of nothing reaches nobody's data."""
    detail = stranger.get(reverse("app:entity_detail", args=[entity_a.pk]))
    assert detail.status_code == 302, "the gate fires before the view"

    scope = stranger.get("/app/").wsgi_request.access_scope
    assert scope is not None
    assert not scope.can_read(entity_a.tenant_id)

    with platform_scope(reason="test"):
        assert Entity.objects.filter(pk=entity_a.pk).exists(), "fixture sanity"


# ---------------------------------------------------------------------------
# No loop, and no trap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["onboarding:identity", "onboarding:profile", "onboarding:preview"],
)
def test_the_setup_flow_itself_stays_reachable(stranger: Client, name: str) -> None:
    """The one carve-out inside /app/. Without it the redirect is a loop."""
    response = stranger.get(reverse(name))

    assert response.status_code in {200, 302}
    if response.status_code == 302:
        # `profile` and `preview` bounce back within the wizard when the draft is
        # empty. What they must never do is bounce to the gate's destination,
        # which is what a loop looks like.
        assert response["Location"].startswith("/app/start/")


def test_signing_out_is_not_blocked_by_the_gate(stranger: Client) -> None:
    """Being sent to setup must not trap somebody in it."""
    response = stranger.post(reverse("accounts:logout"))

    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:login")


def test_marketing_pages_are_untouched(stranger: Client) -> None:
    assert stranger.get("/").status_code == 200


# ---------------------------------------------------------------------------
# A member is unaffected
# ---------------------------------------------------------------------------


def test_a_member_still_lands_on_the_dashboard(client: Client, org_owner: User) -> None:
    signed_in = sign_in(client, org_owner)

    response = signed_in.get("/app/")

    assert response.status_code == 200


def test_a_member_can_still_start_a_second_organisation(client: Client, org_owner: User) -> None:
    """The sidebar dropdown's route, which must keep working exactly as it did.

    The gate cannot fire for them — they have a tenant — and `org-owner` carries
    `tenancy.onboarding.start` for precisely this.
    """
    signed_in = sign_in(client, org_owner)

    assert signed_in.get(reverse("onboarding:identity")).status_code == 200


# ---------------------------------------------------------------------------
# Suspended: a different answer, on purpose
# ---------------------------------------------------------------------------


def test_a_suspended_member_is_told_so_rather_than_sent_to_setup(
    client: Client, suspended_user: User, org: Tenant
) -> None:
    """Suspension is a decision somebody took. It is not an invitation to start again.

    Sent into setup, this user would create a fresh organisation and carry on —
    which would make a suspension a suggestion.
    """
    signed_in = sign_in(client, suspended_user)

    response = signed_in.get("/app/")
    body = response.content.decode()

    assert response.status_code == 403
    assert "suspended" in body.lower()
    assert org.name in body
    assert reverse("onboarding:identity") not in body, "an escape route into setup"


def test_a_suspended_member_can_still_sign_out(client: Client, suspended_user: User) -> None:
    signed_in = sign_in(client, suspended_user)

    assert signed_in.post(reverse("accounts:logout")).status_code == 302


def test_a_suspended_member_cannot_create_an_organisation(
    client: Client, suspended_user: User
) -> None:
    """The escape hatch, closed twice over.

    ``/app/start/`` is exempt from the gate's redirect so that a member-less user
    can reach it — so the exemption is checked *after* suspension, and the scope
    resolver withholds ``tenancy.onboarding.start`` as well. Either alone would
    hold today; both is what makes it survive somebody rearranging the
    middleware.
    """
    signed_in = sign_in(client, suspended_user)

    response = signed_in.get(reverse("onboarding:identity"))

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

    assert response.status_code == 200
    assert response.wsgi_request.tenant == other_org


def test_an_unaccepted_invitation_goes_to_setup_not_the_refusal(
    client: Client, org: Tenant, org_owner: User
) -> None:
    """An invitation nobody has accepted is not a revocation.

    Such a user has no active membership either, and lumping the two together
    would show somebody who has done nothing wrong a screen telling them their
    access was suspended.
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

    assert response.status_code == 302
    assert response["Location"] == reverse("onboarding:identity")
