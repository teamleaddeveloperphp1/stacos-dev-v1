"""
Raising and delivering notifications.

``raise_notification`` is the only entry point other modules use. It takes what
happened, not how to say it: the recipient's preferences decide the channels,
and the ``dedupe_key`` decides whether anything happens at all.

The delivery functions below are deliberately dull. Every interesting decision —
who to tell, whether they have already been told, whether this waits for the
digest — is made before a channel is touched, so a channel is only ever handed a
message it is certain it should send.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import structlog
from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone

from stacos.accounts.whatsapp import WhatsAppMessage, get_whatsapp_provider
from stacos.notifications.models import (
    Channel,
    Delivery,
    DeliveryState,
    Notification,
    NotificationKind,
    NotificationPreference,
    Severity,
    SubjectType,
)
from stacos.notifications.templates_registry import template_key_for

logger = structlog.get_logger(__name__)

__all__ = [
    "deliver",
    "mark_all_read",
    "preferences_for",
    "raise_notification",
    "unread_count",
]


def _as_uuid(value: UUID | str) -> UUID:
    """Normalise a tenant id before it reaches a model constructor.

    Tasks carry ids as strings — they travel through JSON — and assigning one
    straight to ``tenant_id`` leaves the instance holding a `str` until it is
    reloaded. The cross-tenant write guard compares against a set of `UUID`s, so
    a string silently fails that comparison and the write is refused with a
    message that reads like a tenancy bug rather than a type one.
    """
    return value if isinstance(value, UUID) else UUID(str(value))


@dataclass(frozen=True, slots=True)
class Raised:
    """What `raise_notification` did, for a caller that wants to know."""

    notification: Notification | None
    created: bool

    def __bool__(self) -> bool:
        return self.created


def preferences_for(*, tenant_id: UUID | str, user: Any) -> NotificationPreference:
    """This user's preferences in this tenant, creating the defaults if absent.

    Created on demand rather than at invitation time: a row per user per tenant
    written eagerly is a migration and a backfill for something most users never
    change, and the defaults are the right answer until they do.
    """
    preference, _created = NotificationPreference.objects.get_or_create(
        tenant_id=_as_uuid(tenant_id), user=user
    )
    return preference


@transaction.atomic
def raise_notification(
    *,
    tenant_id: UUID | str,
    recipient: Any,
    kind: str,
    title: str,
    dedupe_key: str,
    body: str = "",
    url: str = "",
    severity: str = Severity.INFO,
    entity: Any = None,
    subject_type: str = SubjectType.NONE,
    subject_id: UUID | str | None = None,
    context: dict[str, Any] | None = None,
    send_now: bool = True,
) -> Raised:
    """Tell one person one thing, at most once.

    ``dedupe_key`` is what makes a reminder sweep safe to run on a schedule and
    safe to re-run after a failure. It is enforced by a unique constraint rather
    than by a preceding SELECT, because two workers processing a redelivered
    message would both pass the check and both send.

    Returns ``created=False`` when this person has already been told, which is a
    normal outcome and not an error.
    """
    if recipient is None or not getattr(recipient, "is_active", False):
        # A removed or suspended user is not somebody to notify, and creating the
        # row anyway leaves an unread badge nobody will ever clear.
        return Raised(notification=None, created=False)

    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                tenant_id=_as_uuid(tenant_id),
                recipient=recipient,
                entity=entity,
                kind=kind,
                severity=severity,
                title=title[:200],
                body=body,
                url=url[:400],
                subject_type=subject_type,
                subject_id=subject_id,
                dedupe_key=dedupe_key[:200],
            )
    except IntegrityError:
        # Already raised. The nested atomic above exists so this rollback does
        # not poison the caller's transaction.
        logger.debug("notifications.duplicate", dedupe_key=dedupe_key)
        return Raised(notification=None, created=False)

    preference = preferences_for(tenant_id=tenant_id, user=recipient)
    _plan_deliveries(notification, preference, context=context or {}, send_now=send_now)
    return Raised(notification=notification, created=True)


def _plan_deliveries(
    notification: Notification,
    preference: NotificationPreference,
    *,
    context: dict[str, Any],
    send_now: bool,
) -> None:
    """Write a delivery row per channel, and dispatch the ones that go now.

    In-app is marked sent immediately — the notification row *is* the delivery.
    The rest are queued, because sending inside the caller's transaction would
    make an SMTP timeout roll back the state change that prompted it.
    """
    Delivery.objects.create(
        tenant_id=notification.tenant_id,
        notification=notification,
        channel=Channel.IN_APP,
        state=DeliveryState.SENT,
        sent_at=timezone.now(),
    )

    if preference.defers_to_digest(severity=notification.severity):
        # Left for the digest to collect. No delivery rows: the digest raises its
        # own notification with its own deliveries, and pre-writing PENDING rows
        # here would look forever like a queue that never drained.
        return

    outbound: list[Delivery] = []
    for channel in (Channel.EMAIL, Channel.WHATSAPP):
        if not preference.allows(
            kind=notification.kind, severity=notification.severity, channel=channel
        ):
            outbound.append(
                Delivery(
                    tenant_id=notification.tenant_id,
                    notification=notification,
                    channel=channel,
                    state=DeliveryState.SUPPRESSED,
                    detail="preference",
                )
            )
            continue
        if channel == Channel.WHATSAPP and template_key_for(notification.kind) is None:
            # No approved template for this kind, so this channel cannot carry it.
            # Recorded rather than skipped, so "why did they not get a WhatsApp"
            # has an answer that is not "read the source".
            outbound.append(
                Delivery(
                    tenant_id=notification.tenant_id,
                    notification=notification,
                    channel=channel,
                    state=DeliveryState.SUPPRESSED,
                    detail="no approved template for this kind",
                )
            )
            continue

        outbound.append(
            Delivery(
                tenant_id=notification.tenant_id,
                notification=notification,
                channel=channel,
                state=DeliveryState.PENDING,
            )
        )

    Delivery.objects.bulk_create(outbound)

    if send_now:
        from stacos.notifications.tasks import deliver_notification

        tenant_id = str(notification.tenant_id)
        notification_id = str(notification.pk)
        transaction.on_commit(
            lambda: deliver_notification.apply_async(
                kwargs={
                    "tenant_id": tenant_id,
                    "notification_id": notification_id,
                    "context": context,
                }
            )
        )


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def deliver(notification: Notification, *, context: dict[str, Any] | None = None) -> list[Delivery]:
    """Attempt every pending delivery for one notification.

    Each channel is attempted independently and records its own outcome. A failed
    email must not stop the WhatsApp message: the two exist precisely so that one
    of them getting through is enough.
    """
    context = context or {}
    results: list[Delivery] = []

    for row in notification.deliveries.filter(state=DeliveryState.PENDING):
        row.attempts += 1
        try:
            if row.channel == Channel.EMAIL:
                _send_email(notification, row)
            elif row.channel == Channel.WHATSAPP:
                _send_whatsapp(notification, row, context)
        # One channel's failure is not the others'. A broad catch is the point:
        # whatever SMTP or an HTTP client raises, the WhatsApp message still goes.
        except Exception as exc:
            row.state = DeliveryState.FAILED
            row.detail = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning(
                "notifications.delivery_failed",
                notification_id=str(notification.pk),
                channel=row.channel,
                error=str(exc),
            )
        row.save(update_fields=["state", "detail", "destination", "sent_at", "attempts"])
        results.append(row)

    return results


def _send_email(notification: Notification, row: Delivery) -> None:
    address = (notification.recipient.email or "").strip()
    if not address:
        row.state = DeliveryState.NOT_REACHABLE
        row.detail = "no email address on the account"
        return

    body = render_to_string(
        "notifications/email/notification.txt",
        {
            "notification": notification,
            "recipient": notification.recipient,
            "base_url": str(getattr(settings, "BASE_URL", "")).rstrip("/"),
        },
    )
    send_mail(
        subject=notification.title,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[address],
        fail_silently=False,
    )
    row.state = DeliveryState.SENT
    row.destination = address[:200]
    row.sent_at = timezone.now()


def _send_whatsapp(notification: Notification, row: Delivery, context: dict[str, Any]) -> None:
    phone = (getattr(notification.recipient, "phone_e164", "") or "").strip()
    if not phone:
        row.state = DeliveryState.NOT_REACHABLE
        row.detail = "no phone number on the account"
        return

    key = template_key_for(notification.kind)
    if key is None:  # pragma: no cover - planned away in _plan_deliveries
        row.state = DeliveryState.SUPPRESSED
        row.detail = "no approved template"
        return

    provider = get_whatsapp_provider()
    result = provider.send(
        WhatsAppMessage(
            to_e164=phone,
            template_key=key,
            params={str(name): str(value) for name, value in context.items()},
        )
    )

    row.destination = phone[:200]
    if result.not_on_whatsapp:
        # A number with no WhatsApp account. Not a failure and never worth
        # retrying — the distinction the provider goes out of its way to report.
        row.state = DeliveryState.NOT_REACHABLE
        row.detail = "the number has no WhatsApp account"
        return
    if not result.accepted:
        row.state = DeliveryState.FAILED
        row.detail = (result.error or "rejected by the provider")[:300]
        return

    row.state = DeliveryState.SENT
    row.sent_at = timezone.now()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def unread_count(*, user: Any) -> int:
    """How many unread notifications this user has in the bound scope."""
    return Notification.objects.filter(recipient=user, read_at__isnull=True).count()


def mark_all_read(*, user: Any) -> int:
    return Notification.objects.filter(recipient=user, read_at__isnull=True).update(
        read_at=timezone.now()
    )


def digest_key(*, user_id: UUID | str, on: date, frequency: str) -> str:
    """The digest's own dedupe key, so a re-run does not send it twice."""
    return f"digest:{frequency.lower()}:{user_id}:{on.isoformat()}"


def kind_label(kind: str) -> str:
    return str(NotificationKind(kind).label)
