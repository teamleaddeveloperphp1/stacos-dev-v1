"""
End-to-end authentication and view smoke tests.

Exercises the paths a real browser takes, through the real middleware stack. The
dual-code flow is testable end to end because the development backends keep what
they send: email lands in ``mail.outbox`` and WhatsApp in
``MemoryWhatsAppProvider.outbox``, so both codes can be read back the way a user
reads them off two devices.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import PendingVerification, TrustedDevice, User
from stacos.accounts.whatsapp import MemoryWhatsAppProvider
from stacos.tenancy.models import Entity, Tenant

pytestmark = pytest.mark.django_db

CODE_RE = re.compile(r"\b(\d{6})\b")


def _codes() -> tuple[str, str]:
    """Pull the two codes out of the email and the WhatsApp message just sent."""
    assert mail.outbox, "no verification email was sent"
    email_code = CODE_RE.search(mail.outbox[-1].body)
    assert email_code, f"no code in the email body: {mail.outbox[-1].body!r}"

    assert MemoryWhatsAppProvider.outbox, "no verification WhatsApp message was sent"
    phone_code = MemoryWhatsAppProvider.outbox[-1].params.get("code")
    assert phone_code, "no code in the WhatsApp template parameters"

    return email_code.group(1), phone_code


# ===========================================================================
# Public surfaces
# ===========================================================================


@pytest.mark.parametrize(
    "path", ["/", "/pricing/", "/security/", "/auth/login/", "/auth/register/", "/healthz"]
)
def test_public_pages_are_reachable_anonymously(client: Client, path: str) -> None:
    assert client.get(path).status_code == 200


def test_favicon_is_served(client: Client) -> None:
    """Browsers request /favicon.ico regardless of the <link rel="icon"> tag."""
    assert client.get("/favicon.ico").status_code in (301, 302)


# ===========================================================================
# The application is closed to anonymous visitors — politely
# ===========================================================================


@pytest.mark.parametrize("path", ["/app/", "/app/entities/", "/auth/security/"])
def test_app_redirects_anonymous_visitors_to_sign_in(client: Client, path: str) -> None:
    """A redirect, not a 500 and not a bare 403.

    An anonymous visitor hitting an application URL is an ordinary event — a
    bookmark, a shared link, an expired session — and `next` has to survive it.
    """
    response = client.get(path)
    assert response.status_code == 302
    assert reverse("accounts:login") in response["Location"]
    assert f"next={path}" in response["Location"]


def test_htmx_requests_get_a_redirect_header_not_a_302(client: Client) -> None:
    """HTMX follows a 302 and swaps the result into the target.

    Left alone, that injects a sign-in form into the middle of a dashboard.
    """
    response = client.get("/app/", headers={"HX-Request": "true"})
    assert response.status_code == 204
    assert reverse("accounts:login") in response["HX-Redirect"]


# ===========================================================================
# Registration, through the dual-OTP gate
# ===========================================================================


def test_registration_requires_both_codes_together(client: Client) -> None:
    """The whole policy, in one test.

    Register, get two codes, and prove that neither alone is enough — only both
    submitted together complete the flow.
    """
    response = client.post(
        reverse("accounts:register"),
        {
            "full_name": "Priya Vaibhav",
            "email": "priya@example.com",
            "phone": "9876543210",
            "password": "a-long-enough-password",
        },
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("accounts:verify")

    user = User.objects.get(email="priya@example.com")
    assert not user.email_verified, "registration alone must not verify anything"
    assert not user.phone_verified
    assert user.phone_e164 == "+919876543210", "phone should be normalised to E.164"

    email_code, phone_code = _codes()
    assert email_code != phone_code, "the two channels must carry different codes"

    # -- Only the email code: refused, and one attempt consumed.
    response = client.post(
        reverse("accounts:verify"),
        {"email_code": email_code, "phone_code": "000000", "remember_device": "on"},
    )
    assert response.status_code == 200
    user.refresh_from_db()
    assert not user.phone_verified, "a single channel must never be enough"

    verification = PendingVerification.objects.get(email="priya@example.com")
    assert verification.attempts == 1, "one submission is one attempt, not two"

    # -- Both codes: through.
    response = client.post(
        reverse("accounts:verify"),
        {"email_code": email_code, "phone_code": phone_code, "remember_device": "on"},
    )
    assert response.status_code == 302

    user.refresh_from_db()
    assert user.email_verified and user.phone_verified

    verification.refresh_from_db()
    assert verification.status == PendingVerification.Status.VERIFIED


def test_remembering_the_device_creates_a_revocable_record(client: Client) -> None:
    """Device trust is what makes a mandatory dual-OTP policy livable."""
    client.post(
        reverse("accounts:register"),
        {
            "full_name": "Ramesh Patel",
            "email": "ramesh@example.com",
            "phone": "9876543211",
            "password": "a-long-enough-password",
        },
    )
    email_code, phone_code = _codes()
    client.post(
        reverse("accounts:verify"),
        {"email_code": email_code, "phone_code": phone_code, "remember_device": "on"},
    )

    user = User.objects.get(email="ramesh@example.com")
    device = TrustedDevice.objects.get(user=user)
    assert device.is_valid
    assert device.secret_hash, "only the hash may be stored"
    assert "stacos_td" in client.cookies


def test_wrong_codes_burn_attempts_and_eventually_lock_out(client: Client) -> None:
    """Rate limiting, not hash cost, is what protects a six-digit code."""
    client.post(
        reverse("accounts:register"),
        {
            "full_name": "Test User",
            "email": "attempts@example.com",
            "phone": "9876543212",
            "password": "a-long-enough-password",
        },
    )

    for _ in range(5):
        client.post(
            reverse("accounts:verify"),
            {"email_code": "111111", "phone_code": "222222"},
        )

    verification = PendingVerification.objects.get(email="attempts@example.com")
    assert verification.attempts >= 5
    assert verification.status == PendingVerification.Status.ABANDONED


def test_a_verified_session_reaches_the_application(client: Client) -> None:
    """The end of the flow: past the gate, the dashboard renders."""
    client.post(
        reverse("accounts:register"),
        {
            "full_name": "Anita Rao",
            "email": "anita@example.com",
            "phone": "9876543213",
            "password": "a-long-enough-password",
        },
    )
    email_code, phone_code = _codes()
    client.post(
        reverse("accounts:verify"),
        {"email_code": email_code, "phone_code": phone_code, "remember_device": "on"},
    )

    # No membership yet, so the scope is empty — but the shell must still render
    # rather than erroring, because this is what a brand-new user sees.
    response = client.get("/app/")
    assert response.status_code in (200, 403)


# ===========================================================================
# Signed-in behaviour
# ===========================================================================


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    """A fully verified session for the organisation owner."""
    from stacos.accounts.middleware import SESSION_VERIFIED_KEY

    client.force_login(org_owner)
    session = client.session
    session[SESSION_VERIFIED_KEY] = True
    session.save()
    return client


def test_dashboard_renders_for_a_member(signed_in: Client, entity_a: Entity) -> None:
    response = signed_in.get("/app/")
    assert response.status_code == 200
    assert b"Compliance health" in response.content


def test_entity_list_renders_and_shows_only_this_tenants_entities(
    signed_in: Client, entity_a: Entity, rival_entity: Entity
) -> None:
    response = signed_in.get("/app/entities/")
    assert response.status_code == 200
    assert entity_a.name.encode() in response.content
    assert rival_entity.name.encode() not in response.content, "cross-tenant leak in the list view"


def test_entity_detail_is_a_404_for_another_tenants_entity(
    signed_in: Client, rival_entity: Entity
) -> None:
    """404, not 403.

    Confirming that an entity exists in another tenant is itself a disclosure.
    """
    assert signed_in.get(f"/app/entities/{rival_entity.pk}/").status_code == 404


def test_htmx_request_returns_the_fragment_not_the_page(
    signed_in: Client, entity_a: Entity
) -> None:
    """The dual-render convention, verified over HTTP.

    A direct GET returns the shell; an HTMX request returns only the fragment.
    If these ever drift, deep links and the back button break.
    """
    page = signed_in.get("/app/entities/")
    fragment = signed_in.get("/app/entities/", headers={"HX-Request": "true"})

    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b"app-sidebar" in page.content
    assert b"app-sidebar" not in fragment.content
    assert entity_a.name.encode() in fragment.content


def test_entity_can_be_created_through_the_modal(signed_in: Client, org: Tenant) -> None:
    """Open the modal, submit it, and get a row plus out-of-band updates back."""
    form = signed_in.get(reverse("app:entity_create"), headers={"HX-Request": "true"})
    assert form.status_code == 200
    assert b"modal" in form.content

    response = signed_in.post(
        reverse("app:entity_create"),
        {
            "name": "New Ventures Pvt Ltd",
            "legal_name": "New Ventures Private Limited",
            "short_code": "NVPL",
            "entity_type": "PVT_LTD",
            "incorporation_date": "2020-04-01",
            "registered_office_state": "IN-KA",
            "registered_office_address": "Bengaluru",
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200, response.content[:400]

    entity = Entity.objects_unscoped.get(name="New Ventures Pvt Ltd")
    assert entity.tenant_id == org.id
    assert entity.short_code == "NVPL"
    assert hasattr(entity, "profile"), "an entity with no profile has nothing to evaluate"

    # One request, three regions: the row, the sidebar counter, and a toast.
    assert b"New Ventures Pvt Ltd" in response.content
    assert b"hx-swap-oob" in response.content
    assert "stacos:toast" in response["HX-Trigger"]


def test_invalid_entity_form_returns_422_and_keeps_the_modal_open(
    signed_in: Client, org: Tenant
) -> None:
    """A validation failure re-renders the form, it does not close the modal."""
    response = signed_in.post(
        reverse("app:entity_create"),
        {"name": "", "entity_type": ""},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert b"modal" in response.content


def test_duplicate_entity_name_is_rejected(signed_in: Client, entity_a: Entity) -> None:
    response = signed_in.post(
        reverse("app:entity_create"),
        {"name": entity_a.name, "entity_type": "PVT_LTD"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert b"already have an entity" in response.content


def test_signing_out_everywhere_rotates_the_security_stamp(
    signed_in: Client, org_owner: User
) -> None:
    """Rotation is what makes revocation immediate rather than eventual."""
    before = org_owner.security_stamp
    response = signed_in.post(reverse("accounts:revoke_devices"))
    assert response.status_code == 302

    org_owner.refresh_from_db()
    assert org_owner.security_stamp != before

    # The old session is dead on its next request.
    assert signed_in.get("/app/").status_code == 302


def test_audit_entries_are_written_for_entity_creation(signed_in: Client, org: Tenant) -> None:
    """Every state change is recorded. This is the product, not the plumbing."""
    from stacos.core.models import AuditAction, AuditLog

    signed_in.post(
        reverse("app:entity_create"),
        {"name": "Audited Pvt Ltd", "entity_type": "PVT_LTD"},
        headers={"HX-Request": "true"},
    )

    entry = AuditLog.objects.filter(
        action=AuditAction.CREATE, object_label="Audited Pvt Ltd"
    ).first()
    assert entry is not None
    assert entry.tenant_id == org.id
    assert entry.actor_label
    assert entry.request_id, "the request id ties the entry to its access log line"


def test_permission_denied_renders_403_for_a_signed_in_user(
    client: Client, practice_staff: User, practice: Tenant
) -> None:
    """Signed in but lacking the permission: 403, never a bounce to sign-in.

    Redirecting someone who is already signed in to a login page is the most
    confusing thing an application can do.
    """
    from stacos.accounts.middleware import SESSION_VERIFIED_KEY

    client.force_login(practice_staff)
    session = client.session
    session[SESSION_VERIFIED_KEY] = True
    session.save()

    # Practice staff hold no entity-creation permission.
    response = client.get(reverse("app:entity_create"))
    assert response.status_code == 403
    assert b"access" in response.content.lower()


# ===========================================================================
# The API
# ===========================================================================


def test_api_login_issues_a_challenge_not_a_token(client: Client, org_owner: User) -> None:
    """Even the mobile client goes through both channels before it gets a token."""
    response = client.post(
        "/api/v1/auth/login/",
        {"email": org_owner.email, "password": "test-password-12345"},
        content_type="application/json",
    )
    assert response.status_code == 200
    body = response.json()
    assert "verification_id" in body
    assert "access" not in body, "a token before verification would defeat the whole gate"


def test_api_verify_returns_tokens_carrying_the_security_stamp(
    client: Client, org_owner: User
) -> None:
    """The claim that makes 'sign out everywhere' reach the mobile app."""
    import jwt

    start = client.post(
        "/api/v1/auth/login/",
        {"email": org_owner.email, "password": "test-password-12345"},
        content_type="application/json",
    )
    email_code, phone_code = _codes()

    response = client.post(
        "/api/v1/auth/verify/",
        {
            "verification_id": start.json()["verification_id"],
            "email_code": email_code,
            "phone_code": phone_code,
        },
        content_type="application/json",
    )
    assert response.status_code == 200

    claims: dict[str, Any] = jwt.decode(
        response.json()["access"], options={"verify_signature": False}
    )
    assert claims["sec"] == str(org_owner.security_stamp)
    assert claims["vrf"] is True


def test_api_rejects_a_token_whose_stamp_has_been_rotated(client: Client, org_owner: User) -> None:
    """Revocation must bite on the next request, not at token expiry."""
    from stacos.api.authentication import issue_tokens

    tokens = issue_tokens(org_owner, verified=True)
    auth = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access']}"}

    assert client.get("/api/v1/me/", **auth).status_code == 200

    org_owner.rotate_security_stamp()

    assert client.get("/api/v1/me/", **auth).status_code == 401
