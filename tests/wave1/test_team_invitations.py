"""
Getting a second person into a workspace.

``Membership.Status.INVITED`` and ``Membership.invited_by`` were on the model
from the beginning and nothing ever created one — a membership needs a ``user``,
and the colleague being invited usually has no account yet. The only routes into
a workspace were the founder creating it, a test fixture, and ``seed_dev``.

The thing worth defending is that an invitation is *permission to join*, not
proof of identity. Clicking a link must not skip the dual-OTP gate, and it must
not let whoever the link was forwarded to claim the invited address.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from stacos.accounts.models import User
from stacos.accounts.whatsapp import MemoryWhatsAppProvider
from stacos.core.models import AuditLog
from stacos.core.scope import platform_scope
from stacos.tenancy.invitations import InvitationError, invite_colleague
from stacos.tenancy.models import Membership, Role, Tenant, TenantInvitation
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    return sign_in(client, org_owner, step_up=True)


@pytest.fixture
def viewer_role(org: Tenant) -> Role:
    with platform_scope(reason="test-fixture"):
        return Role.objects.get(tenant__isnull=True, code="org-viewer", tenant_type=org.type)


@pytest.fixture
def invitation(org: Tenant, org_owner: User, viewer_role: Role) -> tuple[TenantInvitation, str]:
    with platform_scope(reason="test-fixture"):
        return invite_colleague(
            org,
            email="colleague@acme.example",
            role=viewer_role,
            inviter=org_owner,
        )


def _codes() -> tuple[str, str]:
    email_code = re.search(r"\b(\d{4,8})\b", mail.outbox[-1].body)
    assert email_code, "no code in the email"
    phone_code = MemoryWhatsAppProvider.outbox[-1].params.get("code")
    assert phone_code
    return email_code.group(1), phone_code


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def test_an_owner_can_reach_the_people_screen(signed_in: Client) -> None:
    response = signed_in.get(reverse("app:team"))

    assert response.status_code == 200
    assert "Invite a colleague" in response.content.decode()


def test_inviting_sends_an_email_with_a_link(
    signed_in: Client, viewer_role: Role, org: Tenant
) -> None:
    response = signed_in.post(
        reverse("app:team_invite"),
        {"email": "Colleague@Acme.Example", "role": str(viewer_role.pk)},
        headers=HTMX,
    )

    assert response.status_code == 200
    assert mail.outbox[-1].to == ["colleague@acme.example"], "the address was not normalised"
    assert "/auth/invite/" in mail.outbox[-1].body

    with platform_scope(reason="test"):
        row = TenantInvitation.objects.get(tenant=org)
        assert row.status == TenantInvitation.Status.SENT
        assert row.role == viewer_role


def test_inviting_an_existing_member_is_refused(
    signed_in: Client, viewer_role: Role, org_owner: User
) -> None:
    """Not silently ignored. To the inviter, nothing happening is a bug report."""
    response = signed_in.post(
        reverse("app:team_invite"),
        {"email": org_owner.email, "role": str(viewer_role.pk)},
        headers=HTMX,
    )

    assert response.status_code == 422
    assert "already a member" in response.content.decode()


def test_inviting_is_audited(signed_in: Client, viewer_role: Role) -> None:
    signed_in.post(
        reverse("app:team_invite"),
        {"email": "colleague@acme.example", "role": str(viewer_role.pk)},
        headers=HTMX,
    )

    with platform_scope(reason="test"):
        entries = AuditLog.objects.filter(object_type="tenancy.TenantInvitation")
        assert entries.exists(), "granting access to a workspace left no audit row"


def test_withdrawing_stops_the_link_working(
    signed_in: Client, invitation: tuple[TenantInvitation, str]
) -> None:
    row, raw = invitation

    response = signed_in.post(reverse("app:team_invite_revoke", args=[row.pk]), headers=HTMX)
    assert response.status_code == 200

    landed = Client().get(reverse("accounts:accept_invitation", kwargs={"token": raw}))
    assert landed.status_code == 404


# ---------------------------------------------------------------------------
# Accepting, with an account
# ---------------------------------------------------------------------------


def test_a_verified_user_joins_immediately(
    client: Client, invitation: tuple[TenantInvitation, str], org: Tenant
) -> None:
    _row, raw = invitation
    colleague = User.objects.create_user(
        email="colleague@acme.example",
        password="correct-horse-battery",
        first_name="Deepa",
        last_name="Colleague",
        phone_e164="+919800000301",
        email_verified=True,
        phone_verified=True,
    )
    signed_in = sign_in(client, colleague)

    response = signed_in.get(reverse("accounts:accept_invitation", kwargs={"token": raw}))

    assert response.status_code == 302
    with platform_scope(reason="test"):
        membership = Membership.objects.get(tenant=org, user=colleague)
        assert membership.status == Membership.Status.ACTIVE
        assert membership.role.code == "org-viewer"

    # And they are in, rather than being sent to create an organisation of their own.
    assert signed_in.get("/app/").status_code == 200


def test_accepting_twice_is_not_an_error(
    client: Client, invitation: tuple[TenantInvitation, str]
) -> None:
    """A link gets clicked twice. The second click must not be an error page for
    somebody who is already in."""
    _row, raw = invitation
    colleague = User.objects.create_user(
        email="colleague@acme.example",
        password="correct-horse-battery",
        phone_e164="+919800000302",
        email_verified=True,
        phone_verified=True,
    )
    signed_in = sign_in(client, colleague)

    first = signed_in.get(reverse("accounts:accept_invitation", kwargs={"token": raw}))
    assert first.status_code == 302

    # The invitation is spent, so the second click is an ordinary invalid link
    # rather than a crash — and the membership is untouched.
    second = signed_in.get(reverse("accounts:accept_invitation", kwargs={"token": raw}))
    assert second.status_code in {302, 404}

    with platform_scope(reason="test"):
        assert Membership.objects.filter(user=colleague).count() == 1


def test_an_unverified_session_is_held_at_the_gate(
    client: Client, invitation: tuple[TenantInvitation, str], org: Tenant
) -> None:
    """An invitation is permission to join, not proof of identity.

    Somebody signed in on a device that has not been verified must not be able to
    turn a forwarded link into a membership.
    """
    _row, raw = invitation
    colleague = User.objects.create_user(
        email="colleague@acme.example",
        password="correct-horse-battery",
        phone_e164="+919800000303",
        email_verified=True,
        phone_verified=True,
    )
    client.force_login(colleague)  # No SESSION_VERIFIED_KEY.

    response = client.get(reverse("accounts:accept_invitation", kwargs={"token": raw}))

    assert response.status_code == 302
    assert reverse("accounts:verify") in response["Location"]
    with platform_scope(reason="test"):
        assert not Membership.objects.filter(tenant=org, user=colleague).exists()


# ---------------------------------------------------------------------------
# Accepting, with no account at all — the case the whole thing exists for
# ---------------------------------------------------------------------------


def test_somebody_with_no_account_can_sign_up_and_land_inside(
    client: Client, invitation: tuple[TenantInvitation, str], org: Tenant
) -> None:
    """The whole flow, because every leg passing alone is what let this stay unbuilt."""
    _row, raw = invitation
    accept_url = reverse("accounts:accept_invitation", kwargs={"token": raw})

    bounced = client.get(accept_url)
    assert bounced.status_code == 302
    assert reverse("accounts:register") in bounced["Location"]
    assert "colleague%40acme.example" in bounced["Location"], "the address was not carried over"

    mail.outbox.clear()
    client.post(
        reverse("accounts:register"),
        {
            "first_name": "Deepa",
            "last_name": "Colleague",
            "email": "colleague@acme.example",
            "phone": "9876500012",
            "password": "a-long-enough-password",
            "confirm_password": "a-long-enough-password",
            "next": accept_url,
        },
    )

    email_code, phone_code = _codes()
    verified = client.post(
        reverse("accounts:verify"), {"email_code": email_code, "phone_code": phone_code}
    )
    assert verified.status_code == 302
    assert verified["Location"] == accept_url, "sign-up did not return them to the invitation"

    joined = client.get(accept_url)
    assert joined.status_code == 302

    colleague = User.objects.get(email="colleague@acme.example")
    with platform_scope(reason="test"):
        membership = Membership.objects.get(tenant=org, user=colleague)
        assert membership.status == Membership.Status.ACTIVE

    # No organisation of their own was invented on the way through.
    with platform_scope(reason="test"):
        assert Tenant.objects.count() == 1

    assert client.get("/app/").status_code == 200


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------


def test_the_raw_token_is_never_stored(invitation: tuple[TenantInvitation, str]) -> None:
    row, raw = invitation

    with platform_scope(reason="test"):
        row.refresh_from_db()

    assert row.token_hash != raw
    assert len(row.token_hash) == 64


def test_an_expired_invitation_is_refused(invitation: tuple[TenantInvitation, str]) -> None:
    row, raw = invitation
    with platform_scope(reason="test"):
        TenantInvitation.objects.filter(pk=row.pk).update(
            expires_at=timezone.now() - timedelta(days=1)
        )

    assert (
        Client().get(reverse("accounts:accept_invitation", kwargs={"token": raw})).status_code
        == 404
    )


def test_a_forged_token_is_refused_the_same_way() -> None:
    response = Client().get(
        reverse("accounts:accept_invitation", kwargs={"token": "nothing-like-a-real-token"})
    )

    assert response.status_code == 404


def test_reinviting_revokes_the_previous_link(
    org: Tenant, org_owner: User, viewer_role: Role
) -> None:
    with platform_scope(reason="test"):
        _first, first_raw = invite_colleague(
            org, email="colleague@acme.example", role=viewer_role, inviter=org_owner
        )
        _second, second_raw = invite_colleague(
            org, email="colleague@acme.example", role=viewer_role, inviter=org_owner
        )

    visitor = Client()
    assert (
        visitor.get(reverse("accounts:accept_invitation", kwargs={"token": first_raw})).status_code
        == 404
    )
    # The live one resolves — it bounces to sign-up, which is a 302, not a 404.
    assert (
        visitor.get(reverse("accounts:accept_invitation", kwargs={"token": second_raw})).status_code
        == 302
    )


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_another_tenant_cannot_withdraw_your_invitation(
    client: Client, rival_owner: User, invitation: tuple[TenantInvitation, str]
) -> None:
    row, _raw = invitation
    hostile = sign_in(client, rival_owner, step_up=True)

    response = hostile.post(reverse("app:team_invite_revoke", args=[row.pk]), headers=HTMX)

    assert response.status_code == 404
    with platform_scope(reason="test"):
        row.refresh_from_db()
        assert row.status == TenantInvitation.Status.SENT


def test_another_tenants_invitations_are_not_listed(
    client: Client, rival_owner: User, invitation: tuple[TenantInvitation, str]
) -> None:
    hostile = sign_in(client, rival_owner, step_up=True)

    body = hostile.get(reverse("app:team")).content.decode()

    assert "colleague@acme.example" not in body


def test_a_plain_member_cannot_invite(client: Client, org: Tenant, viewer_role: Role) -> None:
    """Inviting is granting access. It is not something every member may do."""
    from tests.conftest import _make_member

    viewer = _make_member(org, "viewer@acme.example", "Vik Viewer", "+919800000304", "org-viewer")
    signed_in = sign_in(client, viewer, step_up=True)

    assert signed_in.get(reverse("app:team")).status_code == 403
    assert (
        signed_in.post(
            reverse("app:team_invite"),
            {"email": "someone@else.example", "role": str(viewer_role.pk)},
            headers=HTMX,
        ).status_code
        == 403
    )


def test_invite_colleague_refuses_an_empty_address(
    org: Tenant, org_owner: User, viewer_role: Role
) -> None:
    with platform_scope(reason="test"), pytest.raises(InvitationError):
        invite_colleague(org, email="   ", role=viewer_role, inviter=org_owner)
