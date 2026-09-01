"""
Responses that have to make the browser navigate.

HTMX follows a 302 itself and swaps the destination into whatever region the
caller targeted. For an ordinary page that is the point; for anything that ends
or changes the session it is a trap, because the browser never leaves. Signing
out returned a plain redirect, so the session ended on the server while the app
stayed on screen looking signed in — sensitive content still visible behind a
session that was already gone.

``HX-Redirect`` is the instruction the browser acts on. These tests pin both
halves of the contract: an HTMX caller gets the instruction, an ordinary caller
still gets an ordinary redirect.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.middleware import SESSION_VERIFIED_KEY
from stacos.accounts.models import User
from stacos.tenancy.models import Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    return sign_in(client, org_owner, step_up=True)


# ---------------------------------------------------------------------------
# Signing out
# ---------------------------------------------------------------------------


def test_signing_out_over_htmx_instructs_the_browser_to_navigate(signed_in: Client) -> None:
    response = signed_in.post(reverse("accounts:logout"), headers=HTMX)

    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("accounts:login")
    assert not response.content, "an HX-Redirect response must carry no body to swap"


def test_signing_out_without_htmx_still_redirects_normally(signed_in: Client) -> None:
    response = signed_in.post(reverse("accounts:logout"))

    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:login")


def test_signing_out_really_ends_the_session(signed_in: Client) -> None:
    signed_in.post(reverse("accounts:logout"), headers=HTMX)

    after = signed_in.get(reverse("app:dashboard"))
    assert after.status_code in (302, 401, 403), "the session survived a sign-out"


def test_signing_out_is_not_reachable_by_get(signed_in: Client) -> None:
    """A sign-out on GET is triggerable by any image tag on any page."""
    assert signed_in.get(reverse("accounts:logout")).status_code == 405


def test_sign_out_everywhere_instructs_the_browser_to_navigate(signed_in: Client) -> None:
    response = signed_in.post(reverse("accounts:revoke_devices"), headers=HTMX)

    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("accounts:login")


def test_sign_out_everywhere_without_htmx_still_redirects_normally(signed_in: Client) -> None:
    response = signed_in.post(reverse("accounts:revoke_devices"))

    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:login")


# ---------------------------------------------------------------------------
# An expired session
# ---------------------------------------------------------------------------


def test_an_unverified_htmx_request_is_told_to_navigate(client: Client, org_owner: User) -> None:
    """The verification gate's HX-Redirect branch was unreachable.

    ``HtmxMiddleware`` was registered *after* the gate, so ``request.htmx`` did
    not exist when the gate ran, the branch tested false, and an HTMX caller got
    a 302 whose destination was swapped into the page and then discarded — the
    user saw nothing happen at all.
    """
    client.force_login(org_owner)
    session = client.session
    session[SESSION_VERIFIED_KEY] = False
    session.save()

    response = client.get(reverse("app:dashboard"), headers=HTMX)

    assert response.status_code == 204
    assert reverse("accounts:verify") in response["HX-Redirect"]


def test_an_unverified_ordinary_request_still_redirects(client: Client, org_owner: User) -> None:
    client.force_login(org_owner)
    session = client.session
    session[SESSION_VERIFIED_KEY] = False
    session.save()

    response = client.get(reverse("app:dashboard"))

    assert response.status_code == 302
    assert reverse("accounts:verify") in response["Location"]


def test_a_rotated_security_stamp_ejects_an_htmx_caller(signed_in: Client, org_owner: User) -> None:
    """ "Sign out everywhere" takes effect on the very next request, in the UI too.

    The stamp is part of the session auth hash, so rotating it is caught by
    Django's own check first and the caller arrives at the authorisation
    middleware as anonymous. Either way the requirement is the same and it is the
    one that was broken: an HTMX caller must be told to *navigate*, not handed a
    redirect it will follow and swap invisibly.
    """
    from stacos.core.scope import platform_scope

    with platform_scope(reason="test"):
        org_owner.rotate_security_stamp()

    response = signed_in.get(reverse("app:dashboard"), headers=HTMX)

    assert response.status_code == 204
    assert response["HX-Redirect"].startswith(reverse("accounts:login"))


def test_the_security_stamp_gate_itself_answers_an_htmx_caller_with_a_redirect(
    org_owner: User,
) -> None:
    """The middleware in isolation, since the session hash usually fires first.

    ``SecurityStampMiddleware`` is the second line of defence — for a session
    whose auth hash still validates but whose stored stamp is stale. It returned
    a plain redirect, and being listed above ``HtmxMiddleware`` it could not have
    detected an HTMX caller even if it had tried.
    """
    from django.contrib.sessions.backends.db import SessionStore
    from django.http import HttpResponse
    from django.test import RequestFactory

    from stacos.accounts.middleware import SESSION_STAMP_KEY, SecurityStampMiddleware

    middleware = SecurityStampMiddleware(lambda _request: HttpResponse("should not be reached"))

    def stale_request(*, htmx: bool) -> object:
        request = RequestFactory().get("/app/")
        session = SessionStore()
        session[SESSION_STAMP_KEY] = "a-stale-stamp"
        session.create()
        request.session = session  # type: ignore[attr-defined]
        request.user = org_owner  # type: ignore[assignment]
        if htmx:
            request.htmx = True  # type: ignore[attr-defined]
        return request

    response = middleware(stale_request(htmx=True))

    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("accounts:login")
    assert response["X-Stacos-Signout-Reason"] == "credentials-changed"

    # The same rejection, without HTMX, stays an ordinary redirect.
    plain = middleware(stale_request(htmx=False))
    assert plain.status_code == 302
    assert plain["Location"] == reverse("accounts:login")


# ---------------------------------------------------------------------------
# The security page
# ---------------------------------------------------------------------------


def test_the_security_page_renders_both_ways(signed_in: Client) -> None:
    """It answered every request with the whole document, HTMX or not.

    Boosted navigation then morphed an entire second application shell into
    ``#main`` — duplicate ``#main``, ``#toast-stack``, ``#modal-container`` and
    sidebar ids, and a re-executed ``Alpine.start()``. The visible symptom was
    that the page did not appear and the sidebar stopped responding until a
    manual refresh.
    """
    page = signed_in.get(reverse("accounts:security"))
    fragment = signed_in.get(reverse("accounts:security"), headers=HTMX)

    assert page.status_code == 200
    assert fragment.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower(), (
        "the security page returned a whole document to an HTMX caller"
    )
    assert b'class="app-shell"' not in fragment.content, (
        "a second application shell would be swapped into #main"
    )
    assert b"Trusted devices" in fragment.content
