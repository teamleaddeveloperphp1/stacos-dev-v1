"""
Raising, sending, answering and closing information requests.

State is derived from the items rather than set by hand wherever possible. A
request whose six items are all answered is answered; one with four is partially
answered. Letting a user set that independently guarantees the two disagree.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.requests.models import (
    InformationRequest,
    RequestEvent,
    RequestItem,
    RequestState,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "RequestError",
    "close_request",
    "due_for_reminder",
    "mark_reminded",
    "record_response",
    "refresh_state",
    "reject_response",
    "send_request",
]


class RequestError(Exception):
    """Something a user did that cannot be done, phrased for a user."""


def _actor_label(actor: Any) -> str:
    if actor is None or not getattr(actor, "is_authenticated", False):
        return "STACOS"
    return (getattr(actor, "audit_label", None) or str(actor))[:200]


def log(
    request: InformationRequest,
    *,
    kind: str,
    actor: Any = None,
    note: str = "",
    **context: Any,
) -> RequestEvent:
    return RequestEvent.objects.create(
        tenant_id=request.tenant_id,
        entity_id=request.entity_id,
        request=request,
        kind=kind,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=note,
        context=context,
    )


@transaction.atomic
def send_request(request: InformationRequest, *, actor: Any = None) -> InformationRequest:
    """Move a draft to sent, and notify whoever owes the answer.

    Refuses an empty request. "Please send me the things" with no list is not a
    request anybody can action, and it is the commonest way a draft escapes.
    """
    if request.state != RequestState.DRAFT:
        raise RequestError(_("This request has already been sent."))
    if not request.items.exists():
        raise RequestError(
            _("Add at least one item before sending — an empty request cannot be answered.")
        )
    if request.assigned_to is None and not request.assigned_email:
        raise RequestError(_("Say who should answer this before sending it."))

    request.state = RequestState.SENT
    request.sent_at = timezone.now()
    request.save(update_fields=["state", "sent_at", "updated_at"])

    log(request, kind=RequestEvent.Kind.SENT, actor=actor)
    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=request,
        before={"state": RequestState.DRAFT},
        after={"state": RequestState.SENT},
    )
    notify(request, reason="sent")
    return request


def notify(request: InformationRequest, *, reason: str) -> None:
    """Tell the recipient there is something waiting.

    Still deliberately thin: this says what happened, and the notifications
    module decides who hears about it, on which channel, and whether they have
    already been told. Imported inside the function because `notifications.tasks`
    reaches back into this module for the reminder ladder.
    """
    from stacos.notifications.models import NotificationKind, Severity, SubjectType
    from stacos.notifications.services import raise_notification

    outstanding = [item.label for item in request.items.all() if not item.is_answered]

    logger.info(
        "rfi.notify",
        request_id=str(request.pk),
        reason=reason,
        recipient=request.assigned_email or str(request.assigned_to_id or ""),
        outstanding=outstanding[:10],
    )

    if request.assigned_to_id is None:
        # An external contact with no account. The email goes out through the
        # request's own delivery path rather than as a notification, because a
        # notification is addressed to a *user* and there is not one here.
        return

    raise_notification(
        tenant_id=request.tenant_id,
        recipient=request.assigned_to,
        kind=NotificationKind.REQUEST_SENT,
        severity=Severity.ATTENTION,
        entity=request.entity,
        title=f"{len(outstanding)} item(s) requested — {request.title}",
        body="\n".join(f"• {label}" for label in outstanding[:10]),
        url=f"/app/requests/{request.pk}/",
        subject_type=SubjectType.REQUEST,
        subject_id=request.pk,
        # Keyed on the reason, so "sent" and a later "reopened" are separate
        # events while a retried send is not.
        dedupe_key=f"request:{request.pk}:{reason}",
        context={
            "entity": request.entity.name if request.entity else "",
            "count": str(len(outstanding)),
            "date": f"{request.due_on:%d %b %Y}" if request.due_on else "",
        },
    )


def mark_reminded(request: InformationRequest, *, on: date) -> None:
    """Record that today's reminder went out.

    Written after the notifications are raised rather than before: a sweep that
    crashed halfway through should chase the remaining people on the next run,
    and a stamp written first would silently suppress that for a day.
    """
    request.last_reminder_at = timezone.now()
    InformationRequest.objects.filter(pk=request.pk).update(
        last_reminder_at=request.last_reminder_at
    )
    logger.info("rfi.reminded", request_id=str(request.pk), on=on.isoformat())


@transaction.atomic
def record_response(
    item: RequestItem,
    *,
    value: str = "",
    actor: Any = None,
) -> RequestItem:
    """Record an answer to one item, then re-derive the request's state."""
    if item.request.state in {RequestState.CLOSED, RequestState.CANCELLED}:
        raise RequestError(_("This request is closed."))

    if item.kind != RequestItem.Kind.DOCUMENT:
        if not value.strip():
            raise RequestError(_("Enter an answer."))
        item.response_value = value.strip()[:500]

    item.responded_at = timezone.now()
    item.responded_by = actor if getattr(actor, "is_authenticated", False) else None
    # Answering clears a previous rejection: the client has had another go, and
    # leaving the reason in place would keep the item permanently unanswered.
    item.rejection_reason = ""
    item.save(
        update_fields=[
            "response_value",
            "responded_at",
            "responded_by",
            "rejection_reason",
            "updated_at",
        ]
    )

    log(
        item.request,
        kind=RequestEvent.Kind.RESPONDED,
        actor=actor,
        note=item.label,
        item_id=str(item.pk),
    )
    refresh_state(item.request, actor=actor)
    return item


@transaction.atomic
def reject_response(item: RequestItem, *, reason: str, actor: Any = None) -> RequestItem:
    """Send an item back with a reason.

    A rejection without a reason produces a client who resubmits the same thing,
    so the reason is required rather than encouraged.
    """
    if not reason.strip():
        raise RequestError(_("Say what was wrong with it — otherwise the same thing comes back."))

    item.rejection_reason = reason.strip()[:250]
    item.responded_at = None
    item.save(update_fields=["rejection_reason", "responded_at", "updated_at"])

    log(
        item.request,
        kind=RequestEvent.Kind.REJECTED,
        actor=actor,
        note=f"{item.label}: {item.rejection_reason}",
        item_id=str(item.pk),
    )

    request = item.request
    request.state = RequestState.CLARIFICATION_NEEDED
    request.save(update_fields=["state", "updated_at"])
    notify(request, reason="rejected")
    return item


@transaction.atomic
def refresh_state(request: InformationRequest, *, actor: Any = None) -> InformationRequest:
    """Re-derive the request's state from its items.

    Only mandatory items count towards "answered". An optional item nobody
    supplied should not hold a filing open, and treating it as blocking is how a
    request sits at 5/6 forever.
    """
    if request.state in {RequestState.DRAFT, RequestState.CLOSED, RequestState.CANCELLED}:
        return request

    items = list(request.items.all())
    mandatory = [item for item in items if item.is_mandatory]
    answered = [item for item in mandatory if item.is_answered]

    previous = request.state
    if mandatory and len(answered) == len(mandatory):
        request.state = RequestState.ANSWERED
        request.answered_at = request.answered_at or timezone.now()
    elif answered:
        request.state = RequestState.PARTIALLY_ANSWERED
    else:
        request.state = RequestState.SENT

    if request.state != previous:
        request.save(update_fields=["state", "answered_at", "updated_at"])
        log(
            request,
            kind=RequestEvent.Kind.STATE_CHANGED,
            actor=actor,
            note=f"{previous} → {request.state}",
        )
    return request


@transaction.atomic
def close_request(
    request: InformationRequest, *, actor: Any = None, note: str = ""
) -> InformationRequest:
    """Close a request, whether or not everything came back.

    Closing an incomplete request is legitimate — the preparer got what they
    needed elsewhere — so it is allowed, and the outstanding items are recorded
    on the timeline rather than silently dropped.
    """
    if request.state in {RequestState.CLOSED, RequestState.CANCELLED}:
        return request

    outstanding = [item.label for item in request.items.all() if not item.is_answered]
    previous = request.state

    request.state = RequestState.CLOSED
    request.closed_at = timezone.now()
    request.save(update_fields=["state", "closed_at", "updated_at"])

    log(
        request,
        kind=RequestEvent.Kind.STATE_CHANGED,
        actor=actor,
        note=note or _("Closed."),
        outstanding=outstanding,
    )
    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=request,
        before={"state": previous},
        after={"state": RequestState.CLOSED},
        context={"outstanding": outstanding},
    )
    return request


def due_for_reminder(*, as_of: date) -> list[InformationRequest]:
    """Open requests whose reminder is due.

    Escalating intervals — one day before the date, then on it, then every third
    day — so a client who is late hears about it more often rather than at a
    constant drip that becomes background noise.
    """
    candidates = (
        InformationRequest.objects.filter(
            state__in=[
                RequestState.SENT,
                RequestState.PARTIALLY_ANSWERED,
                RequestState.CLARIFICATION_NEEDED,
            ],
            archived_at__isnull=True,
            due_on__isnull=False,
        )
        .select_related("entity")
        .prefetch_related("items")
    )

    due: list[InformationRequest] = []
    for request in candidates:
        days_left = (request.due_on - as_of).days if request.due_on else None
        if days_left is None:
            continue
        last = request.last_reminder_at.date() if request.last_reminder_at else None
        if last == as_of:
            continue
        if days_left == 1 or days_left == 0 or (days_left < 0 and abs(days_left) % 3 == 0):
            due.append(request)
    return due
