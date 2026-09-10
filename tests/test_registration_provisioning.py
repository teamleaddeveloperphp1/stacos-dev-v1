"""
Sign-up, and the workspace it is supposed to leave behind.

Registration used to collect one undivided name, one number, and a password with
nothing to check it against. A typo in the password locked somebody out of the
account they had just made, and they found out at the next sign-in. There was
also no way to say "I am here on behalf of a company", so every self-service
customer arrived with a bare personal login and waited for somebody to build them
a workspace by hand.

The tenant is created **after** both channels are proven, not at form submit. An
abandoned or throttled sign-up must leave no organisation behind: such a row is
indistinguishable from a real customer's and nothing would ever clean it up.

A tenant is never created without a jurisdiction pack. Entity types, tax
identifier validators, fiscal-year boundaries and every due date are read from
it, so a tenant without one is a workspace where nothing can be computed — and
the symptom surfaces days later as an empty calendar rather than here as an
error.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.core import mail
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from stacos.accounts.models import PendingVerification, User
from stacos.accounts.whatsapp import MemoryWhatsAppProvider
from stacos.core.scope import platform_scope
from stacos.jurisdictions.models import JurisdictionPack
from stacos.tenancy.models import Entity, Membership, Tenant

pytestmark = pytest.mark.django_db

#: The order the registration form must present its fields in.
EXPECTED_FIELD_ORDER = [
    "first_name",
    "last_name",
    "email",
    "mobile",
    "organisation_name",
    "phone",
    "password",
    "confirm_password",
]


def payload(**overrides: str) -> dict[str, str]:
    data = {
        "first_name": "Asha",
        "last_name": "Founder",
        "email": "asha@example.com",
        "mobile": "9876500001",
        "organisation_name": "",
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

    The WhatsApp number in particular sits immediately below the organisation
    name, and a field added later must not silently land in the middle.
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


def test_the_two_numbers_are_kept_apart(client: Client) -> None:
    client.post(reverse("accounts:register"), payload())

    user = User.objects.get(email="asha@example.com")

    assert user.phone_e164 == "+919876500002", "the WhatsApp channel"
    assert user.mobile_e164 == "+919876500001", "the contact number"


def test_the_name_halves_are_stored_and_composed(client: Client) -> None:
    client.post(reverse("accounts:register"), payload())

    user = User.objects.get(email="asha@example.com")

    assert user.first_name == "Asha"
    assert user.last_name == "Founder"
    assert user.full_name == "Asha Founder"


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def test_an_organisation_name_produces_a_working_workspace(client: Client) -> None:
    client.post(reverse("accounts:register"), payload(organisation_name="Nimbus Software"))
    response = _verify(client)
    assert response.status_code == 302

    user = User.objects.get(email="asha@example.com")
    with platform_scope(reason="test"):
        tenant = Tenant.objects.get(name="Nimbus Software")
        assert tenant.type == Tenant.Type.ORGANISATION

        membership = Membership.objects.get(tenant=tenant, user=user)
        assert membership.status == Membership.Status.ACTIVE
        assert membership.role.code == "org-owner"
        assert membership.joined_at is not None

    # And the point of all of it: they are in, not looking at a refusal.
    assert client.get("/app/").status_code == 200


def test_a_provisioned_tenant_always_has_a_jurisdiction_pack(client: Client) -> None:
    client.post(reverse("accounts:register"), payload(organisation_name="Nimbus Software"))
    _verify(client)

    with platform_scope(reason="test"):
        tenant = Tenant.objects.get(name="Nimbus Software")

        assert tenant.jurisdiction_pack is not None, (
            "a tenant with no pack cannot compute a single due date"
        )
        assert tenant.jurisdiction_pack == JurisdictionPack.objects.get(country="IN")


def test_no_organisation_name_means_no_tenant(client: Client) -> None:
    client.post(reverse("accounts:register"), payload())
    _verify(client)

    with platform_scope(reason="test"):
        assert Tenant.objects.count() == 0

    # They are sent to setup rather than refused. See
    # tests/security/test_organisation_gate.py for the whole of that behaviour.
    response = client.get("/app/")
    assert response.status_code == 302
    assert response["Location"] == reverse("onboarding:identity")


def test_an_abandoned_sign_up_leaves_no_organisation(client: Client) -> None:
    """The reason provisioning waits for the codes.

    Somebody who names an organisation and then never opens the verification
    message has created nothing. A tenant row here would be indistinguishable
    from a real customer's and nothing would ever remove it.
    """
    client.post(reverse("accounts:register"), payload(organisation_name="Never Finished Ltd"))

    with platform_scope(reason="test"):
        assert Tenant.objects.count() == 0
        assert Membership.objects.count() == 0

    verification = PendingVerification.objects.get(email="asha@example.com")
    assert verification.organisation_name == "Never Finished Ltd", (
        "the name has to survive until verification, including across a resend"
    )


def test_a_second_organisation_is_not_minted_for_an_existing_member(
    client: Client, org: Tenant, org_owner: User
) -> None:
    """Replaying a verification must not hand somebody a spare workspace."""
    from stacos.accounts.views import _provision_named_organisation

    verification = PendingVerification.objects.create(
        purpose=PendingVerification.Purpose.REGISTRATION,
        user=org_owner,
        email=org_owner.email,
        phone_e164=org_owner.phone_e164,
        email_code_hash="x",
        phone_code_hash="y",
        organisation_name="Sneaky Second Ltd",
        expires_at=timezone.now(),
    )

    request = RequestFactory().get("/auth/verify/")
    request.session = client.session  # type: ignore[attr-defined]

    _provision_named_organisation(request, verification, org_owner)

    with platform_scope(reason="test"):
        assert not Tenant.objects.filter(name="Sneaky Second Ltd").exists()


# ---------------------------------------------------------------------------
# The wizard, afterwards
# ---------------------------------------------------------------------------


def test_the_wizard_adds_an_entity_to_the_organisation_the_user_already_owns(
    client: Client,
) -> None:
    """Not a second organisation.

    Somebody who named their company at sign-up owns a tenant. Running the setup
    wizard is them describing their first *entity*, and creating a whole second
    organisation for it would leave them switching between two workspaces, one of
    them empty.
    """
    client.post(reverse("accounts:register"), payload(organisation_name="Nimbus Software"))
    _verify(client)

    client.post(reverse("onboarding:identity"), {"pan": "AABCU9603R"})
    client.post(
        reverse("onboarding:profile"),
        {
            "name": "Nimbus Software",
            "entity_type": "PVT_LTD",
            "registered_office_state": "IN-KA",
        },
    )
    client.post(reverse("onboarding:finish"))

    with platform_scope(reason="test"):
        assert Tenant.objects.count() == 1, "the wizard created a second organisation"
        tenant = Tenant.objects.get()
        assert Entity.objects.filter(tenant=tenant).count() == 1
