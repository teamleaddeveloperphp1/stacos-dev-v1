"""
Inviting a colleague into the organisation you already own.

``Membership.Status.INVITED`` and ``Membership.invited_by`` have been on the
model since the beginning and nothing ever created one. They could not: a
membership needs a ``user``, and the colleague being invited usually has no
account yet. So the only way a second person ever got into a workspace was a test
fixture or ``seed_dev``, while the marketing site promised the feature.

The invitation holds the address until there is somebody to attach it to. The
membership is created at acceptance, already active — by which point the invitee
has proven both channels through the ordinary dual-OTP gate, so this adds no new
way to establish an identity and no new way to skip verification.

Distinct from ``engagements.EngagementInvitation``, which is a firm and a client
agreeing to work together across two tenants. This is the far more ordinary
thing, and it is tenant-scoped because the inviting tenant exists by definition.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any

import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from stacos.accounts.models import User
from stacos.core.audit import AuditAction, record_event
from stacos.core.rls import rls_bootstrap
from stacos.core.scope import tenant_context
from stacos.tenancy.models import Membership, Role, Tenant, TenantInvitation

logger = structlog.get_logger(__name__)

__all__ = [
    "INVITATION_DAYS",
    "InvitationError",
    "accept_invitation",
    "invite_colleague",
    "resolve_invitation",
    "revoke_invitation",
    "send_invitation_email",
]

#: How long an invitation stays open. Long enough to survive a holiday, short
#: enough that a forwarded mail is not a standing offer of access.
INVITATION_DAYS = 14


class InvitationError(Exception):
    """Something an administrator tried that cannot be done, phrased for them."""


def _hash(raw: str) -> str:
    """HMAC with the server secret, so a database dump is not a set of live links."""
    return hmac.new(settings.SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()


@transaction.atomic
def invite_colleague(
    tenant: Tenant,
    *,
    email: str,
    role: Role,
    inviter: User,
    message: str = "",
) -> tuple[TenantInvitation, str]:
    """Create an invitation, returning ``(row, raw_token)``.

    Refuses to invite somebody who is already a member. Doing it anyway would
    either hit the membership unique constraint or silently do nothing, and both
    read to the inviter as "it did not work".

    Any earlier open invitation for the same address is revoked first, so
    "resend" leaves exactly one working link rather than two in one inbox.
    """
    address = email.strip().lower()
    if not address:
        raise InvitationError("An email address is required.")

    if Membership.objects.filter(
        tenant=tenant, user__email=address, status=Membership.Status.ACTIVE
    ).exists():
        raise InvitationError(f"{address} is already a member of this organisation.")

    TenantInvitation.objects.filter(
        tenant=tenant, email=address, status=TenantInvitation.Status.SENT
    ).update(status=TenantInvitation.Status.REVOKED, revoked_at=timezone.now())

    raw = secrets.token_urlsafe(32)
    invitation = TenantInvitation.objects.create(
        tenant=tenant,
        email=address,
        role=role,
        message=message[:1000],
        token_hash=_hash(raw),
        expires_at=timezone.now() + timedelta(days=INVITATION_DAYS),
        invited_by=inviter,
    )

    record_event(
        action=AuditAction.CREATE,
        actor=inviter,
        obj=invitation,
        after={"email": address, "role": role.code},
    )
    logger.info("tenancy.invited", tenant_id=str(tenant.id), email=address, role=role.code)
    return invitation, raw


def resolve_invitation(raw: str) -> TenantInvitation | None:
    """The open invitation for this link, or ``None``.

    ``rls_bootstrap`` for the same reason the responder link needs it, and it is
    just as easy to miss: this read happens before any scope exists — the whole
    point is that the reader is *not yet* a member of the tenant — so the policy
    on the invitation table fails closed and a perfectly valid link resolves to
    nothing, with no error anywhere.

    Anchored on the token hash, which nobody can produce without holding the
    link, and which matches at most one row.
    """
    if not raw:
        return None

    with transaction.atomic(), rls_bootstrap():
        invitation = (
            TenantInvitation.objects_unscoped.filter(token_hash=_hash(raw))
            .select_related("tenant", "role", "invited_by")
            .first()
        )
        if invitation is None or not invitation.is_open:
            return None
        return invitation


@transaction.atomic
def accept_invitation(invitation: TenantInvitation, *, user: User) -> Membership:
    """Turn an open invitation into an active membership.

    Created ACTIVE rather than INVITED. That status describes somebody who has
    been asked and has not answered; by the time this runs they have answered,
    and proven both channels doing it.

    Idempotent, because a link gets clicked twice and the second click must not
    be an error page for somebody who is already in.
    """
    with tenant_context(tenant_ids={invitation.tenant_id}, reason="invitation:accept"):
        membership, created = Membership.objects.get_or_create(
            tenant=invitation.tenant,
            user=user,
            defaults={
                "role": invitation.role,
                "status": Membership.Status.ACTIVE,
                "all_entities": True,
                "joined_at": timezone.now(),
                "invited_by": invitation.invited_by,
            },
        )
        if not created and membership.status != Membership.Status.ACTIVE:
            # Somebody previously removed, invited back. Reinstating beats
            # refusing: a refusal would leave the administrator holding an
            # invitation that silently does nothing.
            membership.status = Membership.Status.ACTIVE
            membership.role = invitation.role
            membership.joined_at = membership.joined_at or timezone.now()
            membership.save(update_fields=["status", "role", "joined_at", "updated_at"])

        TenantInvitation.objects.filter(pk=invitation.pk).update(
            status=TenantInvitation.Status.ACCEPTED,
            accepted_by=user,
            accepted_at=timezone.now(),
        )
        record_event(
            action=AuditAction.CREATE,
            actor=user,
            obj=membership,
            after={"tenant": invitation.tenant.name, "role": invitation.role.code},
            context={"invitation_id": str(invitation.pk)},
        )

    logger.info(
        "tenancy.invitation_accepted",
        tenant_id=str(invitation.tenant_id),
        user_id=str(user.pk),
    )
    return membership


@transaction.atomic
def revoke_invitation(invitation: TenantInvitation, *, actor: User) -> None:
    """Withdraw an invitation nobody has accepted."""
    TenantInvitation.objects.filter(pk=invitation.pk).update(
        status=TenantInvitation.Status.REVOKED, revoked_at=timezone.now()
    )
    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=invitation,
        before={"status": TenantInvitation.Status.SENT},
        after={"status": TenantInvitation.Status.REVOKED},
    )


def send_invitation_email(
    invitation: TenantInvitation,
    *,
    raw_token: str,
    request: Any = None,
) -> None:
    """Email the link. The only place the raw token is ever written down."""
    from django.core.mail import send_mail
    from django.urls import reverse

    path = reverse("accounts:accept_invitation", kwargs={"token": raw_token})
    url = request.build_absolute_uri(path) if request is not None else path

    lines = [
        f"{invitation.invited_by} has invited you to join {invitation.tenant.name} on STACOS.",
        "",
    ]
    if invitation.message:
        lines += [invitation.message, ""]
    lines += [
        "Accept here:",
        url,
        "",
        f"The invitation expires in {INVITATION_DAYS} days.",
    ]

    send_mail(
        subject=f"Join {invitation.tenant.name} on STACOS",
        message="\n".join(lines),
        from_email=None,
        recipient_list=[invitation.email],
        fail_silently=False,
    )
    logger.info(
        "tenancy.invitation_sent",
        tenant_id=str(invitation.tenant_id),
        email=invitation.email,
    )
