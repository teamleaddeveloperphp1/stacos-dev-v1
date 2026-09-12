"""
The reminder sweeps.

Everything here follows one shape: a platform-wide *fan-out* task that finds
which tenants have work, and a per-tenant task that does it inside its own
scope. The fan-out is the only thing that sees across tenants, and it dispatches
ids, never objects — a queued task must never inherit the authority of whatever
enqueued it.

The reminder ladder is deliberate and the same everywhere: **seven days, three
days, the day before, the day itself, then every third day once overdue.** Two
reasons for that shape, both learned the hard way by everyone who has built one
of these:

* A daily reminder from day thirty is noise by day five. People filter it, and
  then the one that mattered is filtered too.
* The escalation has to *continue* past the due date. A reminder system that
  goes quiet the moment something becomes overdue has abandoned the user at
  exactly the point the product is supposed to earn its money.

Each rung is its own ``dedupe_key``, so a sweep that runs hourly — or twice,
after a redelivery — sends each rung exactly once.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import structlog
from celery import shared_task
from django.utils import timezone

from stacos.core.scope import platform_scope
from stacos.core.tasks import TenantTask
from stacos.notifications import recipients
from stacos.notifications.models import (
    Notification,
    NotificationKind,
    NotificationPreference,
    Severity,
    SubjectType,
)
from stacos.notifications.services import (
    deliver,
    digest_key,
    raise_notification,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "deliver_notification",
    "send_digests",
    "sweep_billing",
    "sweep_obligation_reminders",
    "sweep_tenants",
]

_TASK_PERMISSIONS = (
    "compliance.obligation.view",
    "billing.view",
)

#: Days before a due date at which a reminder goes out. Descending, because the
#: first rung that matches is the one that fires.
LADDER = (7, 3, 1, 0)

#: Once overdue, every third day. Often enough to stay in view, rare enough not
#: to be filtered.
OVERDUE_INTERVAL = 3


def _rung(days_left: int) -> str | None:
    """Which rung of the ladder today is, or ``None`` for a quiet day."""
    if days_left in LADDER:
        return f"T-{days_left}"
    if days_left < 0 and abs(days_left) % OVERDUE_INTERVAL == 0:
        return f"T+{abs(days_left)}"
    return None


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


@shared_task(name="stacos.notifications.sweep_tenants", ignore_result=True)
def sweep_tenants(as_of: str | None = None) -> dict[str, int]:
    """Dispatch every per-tenant sweep, one message per tenant.

    One message per tenant rather than one task doing everything: a tenant whose
    data trips a bug must not stop every other tenant's reminders, and a queue of
    small messages is what lets a slow tenant be retried on its own.
    """
    from stacos.tenancy.models import Tenant

    day = as_of or timezone.localdate().isoformat()

    # A tenant that is past due still has statutory deadlines, and going silent
    # about them because an invoice is late would be the product punishing the
    # wrong failure. Only suspended and closed tenants stop hearing from us.
    with platform_scope(reason="notifications:sweep"):
        tenant_ids = list(
            Tenant.objects.filter(
                status__in=[Tenant.Status.TRIAL, Tenant.Status.ACTIVE, Tenant.Status.PAST_DUE]
            ).values_list("pk", flat=True)
        )

    for tenant_id in tenant_ids:
        for task in (
            sweep_obligation_reminders,
            sweep_billing,
        ):
            task.apply_async(kwargs={"tenant_id": str(tenant_id), "as_of": day})

    logger.info("notifications.fan_out", tenants=len(tenant_ids), as_of=day)
    return {"tenants": len(tenant_ids)}


# ---------------------------------------------------------------------------
# Obligations
# ---------------------------------------------------------------------------


@shared_task(
    base=TenantTask,
    name="stacos.notifications.sweep_obligation_reminders",
    task_permissions=_TASK_PERMISSIONS,
)
def sweep_obligation_reminders(*, tenant_id: str, as_of: str | None = None) -> dict[str, int]:
    """Remind the responsible people about filings coming up and gone by."""
    from stacos.engine.lifecycle import OPEN_STATES
    from stacos.obligations.models import ObligationInstance
    from stacos.obligations.queries import live

    day = date.fromisoformat(as_of) if as_of else timezone.localdate()
    # The window is bounded on both sides. Without a floor, a register that has
    # been neglected for a year would fan out thousands of reminders the first
    # time this ran, and every one of them would be ignored.
    horizon = day + timedelta(days=max(LADDER))
    floor = day - timedelta(days=90)

    # `live()` excludes superseded and archived rows — it does *not* exclude
    # finished ones, which is exactly the trap here. Reminding somebody that
    # GSTR-3B is due on the 20th when they filed it on the 18th is the fastest
    # way to teach a firm that these messages are noise.
    rows = (
        live(
            ObligationInstance.objects.filter(
                due_date__gte=floor,
                due_date__lte=horizon,
                state__in=[str(state) for state in OPEN_STATES],
            )
        )
        .select_related("entity", "assigned_to")
        .order_by("due_date")
    )

    raised = 0
    for obligation in rows:
        if obligation.due_date is None:
            continue
        days_left = (obligation.due_date - day).days
        rung = _rung(days_left)
        if rung is None:
            continue

        overdue = days_left < 0
        kind = NotificationKind.OBLIGATION_OVERDUE if overdue else NotificationKind.OBLIGATION_DUE
        severity = Severity.URGENT if overdue or days_left <= 1 else Severity.ATTENTION

        audience = recipients.for_entity(
            tenant_id=tenant_id,
            entity=obligation.entity,
            permission="compliance.obligation.view",
            assignee=obligation.assigned_to,
            category=obligation.category or "",
        )

        for user in audience:
            result = raise_notification(
                tenant_id=tenant_id,
                recipient=user,
                kind=kind,
                severity=severity,
                entity=obligation.entity,
                title=(
                    f"{obligation.title} was due {obligation.due_date:%d %b}"
                    if overdue
                    else f"{obligation.title} is due {obligation.due_date:%d %b}"
                ),
                body=_obligation_body(obligation, days_left),
                url=f"/app/compliance/obligations/{obligation.pk}/",
                subject_type=SubjectType.OBLIGATION,
                subject_id=obligation.pk,
                dedupe_key=f"obligation:{obligation.pk}:{rung}",
                context={
                    "entity": obligation.entity.name,
                    "obligation": obligation.title,
                    "date": f"{obligation.due_date:%d %b %Y}",
                },
            )
            raised += int(bool(result))

    return {"raised": raised}


def _obligation_body(obligation: Any, days_left: int) -> str:
    if days_left < 0:
        return (
            f"{obligation.entity.name} — {obligation.period_label or obligation.period_key}. "
            f"This was due {abs(days_left)} day(s) ago and is still open."
        )
    if days_left == 0:
        return f"{obligation.entity.name} — due today."
    return (
        f"{obligation.entity.name} — {obligation.period_label or obligation.period_key}. "
        f"Due in {days_left} day(s)."
    )


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------


@shared_task(
    base=TenantTask,
    name="stacos.notifications.sweep_billing",
    task_permissions=_TASK_PERMISSIONS,
)
def sweep_billing(*, tenant_id: str, as_of: str | None = None) -> dict[str, int]:
    """Move invoices past their date to OVERDUE, and say so once.

    The state change and the notification are in the same task on purpose: an
    invoice that silently became overdue with nobody told is the worst of both —
    a dunning state with no dunning.
    """
    from stacos.billing.models import Invoice, to_major
    from stacos.billing.services import mark_overdue

    day = date.fromisoformat(as_of) if as_of else timezone.localdate()
    moved = mark_overdue(as_of=day)

    rows = Invoice.objects.filter(status=Invoice.Status.OVERDUE).select_related(
        "subscription__plan"
    )

    audience = recipients.for_tenant(tenant_id=tenant_id, permission="billing.view")
    raised = 0

    for invoice in rows:
        if invoice.due_on is None:
            continue
        days_over = (day - invoice.due_on).days
        if days_over < 0 or days_over % OVERDUE_INTERVAL != 0:
            continue

        for user in audience:
            result = raise_notification(
                tenant_id=tenant_id,
                recipient=user,
                kind=NotificationKind.INVOICE_OVERDUE,
                severity=Severity.ATTENTION,
                title=f"Invoice {invoice.number} is overdue",
                body=(
                    f"₹{to_major(invoice.outstanding_minor)} outstanding, "
                    f"due {invoice.due_on:%d %b %Y}."
                ),
                url="/app/billing/",
                subject_type=SubjectType.INVOICE,
                subject_id=invoice.pk,
                dedupe_key=f"invoice:{invoice.pk}:overdue:T+{days_over}",
                context={
                    "reference": invoice.number,
                    "amount": f"₹{to_major(invoice.outstanding_minor)}",
                    "date": f"{invoice.due_on:%d %b %Y}",
                },
            )
            raised += int(bool(result))

    return {"moved_to_overdue": moved, "raised": raised}


# ---------------------------------------------------------------------------
# Delivery and digests
# ---------------------------------------------------------------------------


@shared_task(
    base=TenantTask,
    name="stacos.notifications.deliver_notification",
    task_permissions=_TASK_PERMISSIONS,
)
def deliver_notification(
    *, tenant_id: str, notification_id: str, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Push one notification out over its pending channels."""
    notification = Notification.objects.filter(pk=notification_id).first()
    if notification is None:
        return {"status": "gone"}

    rows = deliver(notification, context=context or {})
    return {"status": "ok", "deliveries": {row.channel: row.state for row in rows}}


@shared_task(name="stacos.notifications.send_digests", ignore_result=True)
def send_digests(as_of: str | None = None) -> dict[str, int]:
    """Collect everything a digest subscriber has not been told about, and tell them.

    Runs hourly and selects by the subscriber's own ``digest_hour``, because a
    digest is only useful if it lands before somebody starts work, and "before
    work" is a local decision.
    """
    now = timezone.localtime()
    day = date.fromisoformat(as_of) if as_of else now.date()

    with platform_scope(reason="notifications:digest"):
        subscribers = list(
            NotificationPreference.objects.filter(
                digest__in=["DAILY", "WEEKLY"], digest_hour=now.hour
            ).values_list("tenant_id", "user_id")
        )

    sent = 0
    for tenant_id, user_id in subscribers:
        send_digest.apply_async(
            kwargs={
                "tenant_id": str(tenant_id),
                "user_id": str(user_id),
                "as_of": day.isoformat(),
            }
        )
        sent += 1

    return {"queued": sent}


@shared_task(
    base=TenantTask,
    name="stacos.notifications.send_digest",
    task_permissions=_TASK_PERMISSIONS,
)
def send_digest(
    *,
    tenant_id: str,
    user_id: str,
    as_of: str | None = None,
) -> dict[str, Any]:
    """One person's summary of everything since their last one."""
    from django.contrib.auth import get_user_model

    day = date.fromisoformat(as_of) if as_of else timezone.localdate()
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        return {"status": "gone"}

    preference = NotificationPreference.objects.filter(tenant_id=tenant_id, user=user).first()
    if preference is None:
        return {"status": "no_preference"}

    window = timedelta(days=7 if preference.digest == "WEEKLY" else 1)
    pending = list(
        Notification.objects.filter(
            recipient=user,
            digested_at__isnull=True,
            read_at__isnull=True,
            created_at__gte=timezone.now() - window,
        )
        .exclude(kind=NotificationKind.DIGEST)
        .order_by("-severity", "created_at")[:50]
    )

    if not pending:
        # No digest at all rather than an empty one. "Nothing to report" mail is
        # the fastest way to teach somebody to ignore the sender.
        return {"status": "empty"}

    lines = [f"• {item.title}" for item in pending]
    result = raise_notification(
        tenant_id=tenant_id,
        recipient=user,
        kind=NotificationKind.DIGEST,
        severity=Severity.INFO,
        title=f"{len(pending)} thing(s) need your attention",
        body="\n".join(lines),
        url="/app/notifications/",
        dedupe_key=digest_key(user_id=user_id, on=day, frequency=preference.digest),
    )

    if result.created:
        Notification.objects.filter(pk__in=[item.pk for item in pending]).update(
            digested_at=timezone.now()
        )

    return {"status": "sent" if result.created else "duplicate", "items": len(pending)}
