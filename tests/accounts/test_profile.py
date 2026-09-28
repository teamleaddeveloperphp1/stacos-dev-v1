"""
The profile page, and editing your own name and timezone from it.

The page is a control centre in three bands — identity, overview, then "My
profile" apart from "My organisation" — and these tests pin the decisions that
shape it rather than its markup: the destructive control lives in the security
card, contact details are masked where they are restated, organisation actions
follow permissions, and an edit is audited.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.models import AuditAction, AuditLog
from stacos.core.scope import platform_scope, tenant_context
from stacos.tenancy.models import Membership, Role, Tenant

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


def _body(client: Client, **headers: str) -> str:
    response = client.get(reverse("accounts:profile"), headers=headers)
    assert response.status_code == 200
    return response.content.decode()


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_profile_renders_both_ways(signed_in: Client) -> None:
    page = _body(signed_in)
    fragment = _body(signed_in, **HTMX)

    assert "<html" in page
    assert "<html" not in fragment
    for body in (page, fragment):
        assert "Account overview" in body
        assert "My profile" in body
        assert "My organisation" in body


def test_sign_out_everywhere_lives_in_the_security_card(signed_in: Client) -> None:
    """Not beside the name, where it used to be one mis-click from "Edit"."""
    body = _body(signed_in, **HTMX)
    revoke = reverse("accounts:revoke_devices")

    assert body.count(f'action="{revoke}"') == 1
    assert body.index("card--security") < body.index(revoke)
    assert body.index("profile-header") < body.index("Account overview") < body.index(revoke)


def test_the_security_card_masks_what_it_restates(signed_in: Client, org_owner: User) -> None:
    body = _body(signed_in, **HTMX)
    security = body[body.index("card--security") :]

    assert org_owner.email not in security
    assert org_owner.phone_e164 not in security
    assert "o***r@acme.example" in security
    assert "+91 ***** 00001" in security


def test_the_role_is_named_once(signed_in: Client, org_owner: User, org: Tenant) -> None:
    body = _body(signed_in, **HTMX)
    with tenant_context(tenant_ids={org.id}, reason="test"):
        role_name = Membership.objects.get(user=org_owner).role.name

    assert body.count(role_name) == 1


def test_an_owner_is_offered_the_organisation_actions(signed_in: Client) -> None:
    body = _body(signed_in, **HTMX)

    assert reverse("app:workspace_rename") in body
    assert reverse("app:team") in body


def test_a_member_without_the_grants_is_not_offered_them(client: Client, org: Tenant) -> None:
    """The links follow the same permissions the views behind them enforce."""
    from tests.conftest import sign_in

    with platform_scope(reason="test-fixture"):
        role = Role.objects.create(
            tenant=org,
            code="viewer",
            name="Viewer",
            tenant_type=org.type,
            permissions=["accounts.profile.view", "accounts.security.manage"],
        )
        user = User.objects.create_user(
            email="viewer@acme.example",
            password="test-password-12345",
            full_name="Ravi Iyer",
            phone_e164="+919800000009",
            email_verified=True,
            phone_verified=True,
        )
        Membership.objects.create(tenant=org, user=user, role=role, status=Membership.Status.ACTIVE)
    sign_in(client, user)

    body = _body(client, **HTMX)

    assert reverse("app:workspace_rename") not in body
    assert reverse("app:team") not in body
    assert reverse("billing:overview") not in body
    assert reverse("app:entity_list") not in body
    # Their own profile and security remain theirs to act on.
    assert reverse("accounts:profile_edit") in body
    assert reverse("accounts:revoke_devices") in body


def test_a_complete_profile_shows_no_setup_prompt(signed_in: Client) -> None:
    assert "Finish setting up your profile" not in _body(signed_in, **HTMX)


def test_a_nameless_account_is_asked_for_its_name(signed_in: Client, org_owner: User) -> None:
    User.objects.filter(pk=org_owner.pk).update(full_name="", first_name="", last_name="")

    body = _body(signed_in, **HTMX)

    assert "Finish setting up your profile" in body
    assert "Add your name" in body


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def _payload(**overrides: str) -> dict[str, str]:
    return {
        "first_name": "Anita",
        "last_name": "Rao",
        "display_name": "",
        "timezone": "Asia/Kolkata",
        **overrides,
    }


def test_the_edit_modal_splits_a_legacy_full_name(signed_in: Client) -> None:
    """Fixture users carry only `full_name`; the form must not arrive blank."""
    body = signed_in.get(reverse("accounts:profile_edit"), headers=HTMX).content.decode()

    assert 'value="Anita"' in body
    assert 'value="Rao"' in body
    assert 'data-bs-backdrop="static"' in body


def test_saving_updates_the_profile_and_closes_the_modal(
    signed_in: Client, org_owner: User
) -> None:
    response = signed_in.post(
        reverse("accounts:profile_edit"),
        _payload(first_name="Anitha", display_name="Anu", timezone="Asia/Dubai"),
        headers=HTMX,
    )

    assert response.status_code == 200
    assert "stacos:modal-close" in response["HX-Trigger"]
    org_owner.refresh_from_db()
    assert org_owner.full_name == "Anitha Rao"
    assert org_owner.display_name == "Anu"
    assert org_owner.timezone == "Asia/Dubai"


def test_an_edit_is_audited_with_before_and_after(signed_in: Client, org_owner: User) -> None:
    signed_in.post(
        reverse("accounts:profile_edit"), _payload(timezone="Europe/London"), headers=HTMX
    )

    with platform_scope(reason="test"):
        entry = AuditLog.objects.filter(
            action=AuditAction.UPDATE, object_id=str(org_owner.pk)
        ).first()

    assert entry is not None, "no audit row was written for the edit"
    assert entry.before["timezone"] == "Asia/Kolkata"
    assert entry.after["timezone"] == "Europe/London"
    assert "email" not in entry.after


def test_an_edit_that_changes_nothing_records_nothing(signed_in: Client, org_owner: User) -> None:
    User.objects.filter(pk=org_owner.pk).update(first_name="Anita", last_name="Rao")

    signed_in.post(reverse("accounts:profile_edit"), _payload(), headers=HTMX)

    with platform_scope(reason="test"):
        assert not AuditLog.objects.filter(
            action=AuditAction.UPDATE, object_id=str(org_owner.pk)
        ).exists()


def test_an_invalid_edit_keeps_the_form_open(signed_in: Client, org_owner: User) -> None:
    response = signed_in.post(
        reverse("accounts:profile_edit"),
        _payload(first_name="", timezone="Mars/Olympus"),
        headers=HTMX,
    )

    assert response.status_code == 422
    assert 'data-bs-backdrop="static"' in response.content.decode()
    org_owner.refresh_from_db()
    assert org_owner.timezone == "Asia/Kolkata"


def test_the_edit_cannot_touch_the_sign_in_channels(signed_in: Client, org_owner: User) -> None:
    """Email and WhatsApp are verification flows, not form fields."""
    signed_in.post(
        reverse("accounts:profile_edit"),
        _payload(email="attacker@evil.example", phone_e164="+10000000000"),
        headers=HTMX,
    )

    org_owner.refresh_from_db()
    assert org_owner.email == "owner@acme.example"
    assert org_owner.phone_e164 == "+919800000001"
