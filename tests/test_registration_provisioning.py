"""
Sign-up, and the organisation it is supposed to leave behind.

Registration used to collect one undivided name, one number, and a password with
nothing to check it against. A typo in the password locked somebody out of the
account they had just made, and they found out at the next sign-in.

STACOS is one identity per user, decided here: the organisation name is
collected at sign-up, and the tenant is provisioned the moment both OTP
channels are proven (``accounts.views._complete_verification``) — there is no
separate "set up an organisation" step afterwards.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.accounts.whatsapp import MemoryWhatsAppProvider
from stacos.core.scope import platform_scope
from stacos.tenancy.models import Membership, Tenant

pytestmark = pytest.mark.django_db

#: The order the registration form must present its fields in.
EXPECTED_FIELD_ORDER = [
    "first_name",
    "last_name",
    "organisation_name",
    "email",
    "phone",
    "password",
    "confirm_password",
]


def payload(**overrides: str) -> dict[str, str]:
    data = {
        "first_name": "Asha",
        "last_name": "Founder",
        "organisation_name": "Founder Textiles",
        "email": "asha@example.com",
        "phone": "9876500002",
        "password": "a-long-enough-password",
        "confirm_password": "a-long-enough-password",
    }
    data.update(overrides)
    return data


def _codes() -> tuple[str, str]:
    """The email code from the outbox, the WhatsApp code from the memory provider.

    The same extraction as ``tests/test_auth_flow._codes``; kept local because
    this module is about what happens *after* the codes are correct.
    """
    assert mail.outbox, "no verification email was sent"
    email_code = re.search(r"\b(\d{4,8})\b", mail.outbox[-1].body)
    assert email_code, f"no code in the email body: {mail.outbox[-1].body!r}"

    assert MemoryWhatsAppProvider.outbox, "no verification WhatsApp message was sent"
    phone_code = MemoryWhatsAppProvider.outbox[-1].params.get("code")
    assert phone_code, "no code in the WhatsApp template parameters"

    return email_code.group(1), phone_code


def _verify(client: Client) -> Any:
    email_code, phone_code = _codes()
    return client.post(
        reverse("accounts:verify"),
        {"email_code": email_code, "phone_code": phone_code},
    )


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------


def test_the_fields_are_in_the_specified_order(client: Client) -> None:
    """Order is part of the requirement, so it is pinned rather than assumed.

    A field added later must not silently land in the middle.
    """
    body = client.get(reverse("accounts:register")).content.decode()

    positions = [body.find(f'name="{field}"') for field in EXPECTED_FIELD_ORDER]

    assert all(position >= 0 for position in positions), dict(
        zip(EXPECTED_FIELD_ORDER, positions, strict=True)
    )
    assert positions == sorted(positions), (
        f"fields are out of order: {dict(zip(EXPECTED_FIELD_ORDER, positions, strict=True))}"
    )


def test_a_mismatched_confirmation_creates_nothing(client: Client) -> None:
    """The failure this field exists to prevent: locked out of a brand-new account."""
    response = client.post(
        reverse("accounts:register"),
        payload(password="a-long-enough-password", confirm_password="a-long-enough-passwordd"),
    )

    assert response.status_code == 200
    assert "do not match" in response.content.decode()
    assert not User.objects.filter(email="asha@example.com").exists()
    assert not mail.outbox, "a verification code went out for a sign-up that failed"


def test_the_name_halves_are_stored_and_composed(client: Client) -> None:
    client.post(reverse("accounts:register"), payload())

    user = User.objects.get(email="asha@example.com")

    assert user.first_name == "Asha"
    assert user.last_name == "Founder"
    assert user.full_name == "Asha Founder"


# ---------------------------------------------------------------------------
# Provisioning happens on verification, not later
# ---------------------------------------------------------------------------


def test_a_missing_organisation_name_creates_nothing(client: Client) -> None:
    response = client.post(reverse("accounts:register"), payload(organisation_name=""))

    assert response.status_code == 200
    assert not User.objects.filter(email="asha@example.com").exists()
    assert not mail.outbox, "a verification code went out for a sign-up that failed"


def test_verification_provisions_the_named_organisation(client: Client) -> None:
    client.post(reverse("accounts:register"), payload())
    response = _verify(client)
    assert response.status_code == 302

    with platform_scope(reason="test"):
        tenant = Tenant.objects.get()
        assert tenant.name == "Founder Textiles"
        assert tenant.type == Tenant.Type.ORGANISATION

        membership = Membership.objects.get()
        assert membership.tenant_id == tenant.id
        assert membership.user == User.objects.get(email="asha@example.com")
        assert membership.status == Membership.Status.ACTIVE
        assert membership.role.code == "org-owner"

    # A tenant now, but no entity yet — so the gate sends them to add one
    # rather than to a "set up an organisation" step that no longer exists.
    # See tests/security/test_organisation_gate.py for the whole of that
    # behaviour.
    response = client.get("/app/")
    assert response.status_code == 302
    assert response["Location"] == reverse("app:entity_create")


def test_provisioning_is_idempotent_on_membership(client: Client) -> None:
    """Guards the idempotency check in ``_provision_signup_tenant``.

    A second call for the same user must not be able to double-provision — the
    guard is "does this user already have a membership", not "has this
    verification been used", so it stays correct even if this handler is ever
    reached twice for the same verified user.
    """
    client.post(reverse("accounts:register"), payload())
    _verify(client)

    from stacos.accounts.models import PendingVerification
    from stacos.accounts.views import _provision_signup_tenant

    user = User.objects.get(email="asha@example.com")
    verification = PendingVerification.objects.filter(user=user).latest("created_at")

    class _FakeRequest:
        session: dict[str, str] = {}

    _provision_signup_tenant(_FakeRequest(), user, verification)  # type: ignore[arg-type]

    with platform_scope(reason="test"):
        assert Tenant.objects.filter(name="Founder Textiles").count() == 1
