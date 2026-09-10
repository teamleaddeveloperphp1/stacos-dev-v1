"""
Raising, sending, answering and closing information requests.

State is derived from the items rather than set by hand wherever possible. A
request whose six items are all answered is answered; one with four is partially
answered. Letting a user set that independently guarantees the two disagree.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext as _

from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.core.rls import rls_bootstrap
from stacos.core.scope import tenant_context
from stacos.requests.models import (
    InformationRequest,
    RequestEvent,
    RequestItem,
    RequestState,
    ResponderToken,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "RESPONDER_PERMISSIONS",
    "RESPONDER_TOKEN_DAYS",
    "RequestError",
    "close_request",
    "due_for_reminder",
    "issue_responder_token",
    "mark_reminded",
    "notify_completed",
    "record_document_response",
    "record_responder_use",
    "record_response",
    "refresh_state",
    "reject_response",
    "resolve_responder_token",
    "responder_scope",
    "send_request",
    "send_responder_link",
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


def _audit_item(
    item: RequestItem,
    *,
    actor: Any,
    action: str,
    detail: str = "",
) -> None:
    """Write an item-level change to the tenant audit trail.

    ``RequestEvent`` is the timeline this module renders; ``AuditLog`` is the
    append-only record a business shows a regulator, protected by a database
    trigger that denies UPDATE and DELETE. Creating, sending and closing a
    request already reached it and the two most sensitive actions did not — an
    answer being recorded, and a preparer sending one back — so "who said this
    was received" existed only in a table the same application can rewrite.
    """
    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=item.request,
        after={"item": item.label, "action": action, "detail": detail},
        context={"item_id": str(item.pk), "kind": item.kind},
    )


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
    _audit_item(item, actor=actor, action="responded")
    refresh_state(item.request, actor=actor)
    return item


@transaction.atomic
def record_document_response(item: RequestItem, *, actor: Any = None) -> RequestItem:
    """A file has arrived against an item. Treat it as the answer it is.

    The three kinds of answer used to take two paths and only one of them
    finished the job. A typed answer and a yes/no both go through
    ``record_response`` above, which stamps the item, clears any rejection, logs
    the event and re-derives the request's state. A **file** arrives through the
    vault instead, which updated ``document_count`` and stopped — so a request
    whose items are all documents could have every file uploaded and sit at
    ``SENT`` for ever, and an item that had been rejected could never be
    un-rejected by supplying a better copy, because ``is_answered`` stays false
    while ``rejection_reason`` is set.

    Called from ``vault.services`` on attach and detach, right where the count is
    already being kept honest.

    Detaching the last file is the same operation in reverse: the item is
    unanswered again, and the request has to fall back from ANSWERED. So this
    stamps or clears according to what is actually attached rather than assuming
    an upload.
    """
    if item.request.state in {RequestState.CLOSED, RequestState.CANCELLED}:
        # Nothing to re-derive: a closed request stays closed, and a late file is
        # still worth keeping. `refresh_state` would return early anyway; not
        # calling it keeps the event log free of a transition that did not happen.
        return item

    has_documents = item.document_count > 0

    item.responded_at = timezone.now() if has_documents else None
    item.responded_by = (
        actor if (has_documents and getattr(actor, "is_authenticated", False)) else None
    )
    if has_documents:
        item.rejection_reason = ""
    item.save(update_fields=["responded_at", "responded_by", "rejection_reason", "updated_at"])

    log(
        item.request,
        kind=RequestEvent.Kind.RESPONDED if has_documents else RequestEvent.Kind.NOTE,
        actor=actor,
        note=item.label if has_documents else f"{item.label}: attachment removed",
        item_id=str(item.pk),
    )
    _audit_item(item, actor=actor, action="responded" if has_documents else "unanswered")
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
    _audit_item(item, actor=actor, action="rejected", detail=item.rejection_reason)

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
        # A state the product decided on its own still has to be in the trail. It
        # is the one transition nobody can attest to from memory, which makes it
        # the one most worth recording.
        record_event(
            action=AuditAction.UPDATE,
            actor=actor,
            obj=request,
            before={"state": previous},
            after={"state": request.state},
            context={"derived": "true"},
        )
        if request.state == RequestState.ANSWERED:
            notify_completed(request)

    return request


def notify_completed(request: InformationRequest) -> None:
    """Tell whoever asked that everything they asked for has come back.

    Raised here rather than in the views, because "fully answered" is derived
    from the items and can be reached by any of the three answer types, by an
    outside contact through a responder link, or by a file landing in the vault.
    Anywhere else would be one of those paths, and the other three would stay
    silent — which is the state this was in: the person who raised the request
    found out by going back and looking.

    Only on the transition into ANSWERED, so a partial answer cannot spam, and
    keyed on the request so a re-entry into the same state cannot notify twice.
    """
    from stacos.notifications.models import NotificationKind, Severity, SubjectType
    from stacos.notifications.services import raise_notification

    if request.requested_by_id is None:
        return

    raise_notification(
        tenant_id=request.tenant_id,
        recipient=request.requested_by,
        kind=NotificationKind.REQUEST_ANSWERED,
        severity=Severity.INFO,
        entity=request.entity,
        title=f"Answered — {request.title}",
        body="Everything you asked for has come back.",
        url=f"/app/requests/{request.pk}/",
        subject_type=SubjectType.REQUEST,
        subject_id=request.pk,
        # Not keyed on a reason like `notify()` above: this fires once per
        # request, and a request that is reopened and answered again is the same
        # fact arriving a second time rather than a new one.
        dedupe_key=f"request:{request.pk}:answered",
        context={
            "entity": request.entity.name if request.entity else "",
            "count": str(request.items.count()),
        },
    )


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


# ---------------------------------------------------------------------------
# Answering from outside the tenant
#
# A request can be addressed to an email rather than to a user, because a
# practice deals constantly with a bookkeeper at a client who will never have an
# account. That was modelled and then abandoned: `notify` returns early for such
# a recipient, saying the mail goes out "through the request's own delivery
# path", and no such path was ever built. The address was a dead end.
#
# What follows is the whole of that path. The design constraint is that it must
# not become a second, weaker way into tenant data: it reuses the same scope
# machinery every authenticated request uses, narrowed to one entity and a fixed
# handful of permissions, rather than reaching around it.
# ---------------------------------------------------------------------------

#: How long a responder link stays usable. Long enough that "I will do it at the
#: weekend" works, short enough that a forwarded email is not a standing key.
RESPONDER_TOKEN_DAYS = 21

#: Everything the holder of a responder link may do, stated once. Answering the
#: items of the one request the token names, and attaching files to them. Not
#: viewing the entity, not the vault, not the calendar, and not the other
#: requests of the same client.
RESPONDER_PERMISSIONS = frozenset(
    {
        "rfi.request.view",
        "rfi.request.respond",
        "vault.document.upload",
    }
)


def _hash_token(raw: str) -> str:
    """HMAC with the server secret, so a database dump is not a set of live links."""
    return hmac.new(settings.SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()


@transaction.atomic
def issue_responder_token(
    request: InformationRequest,
    *,
    email: str = "",
    actor: Any = None,
) -> tuple[ResponderToken, str]:
    """Mint a link for the outside contact, returning ``(row, raw_token)``.

    The raw token is returned and never stored — it exists in the emailed link
    and nowhere else. Callers put it in the URL and then forget it.

    Any earlier live link for the same address is revoked first, so re-sending
    the invitation does not leave two working keys behind.
    """
    address = (email or request.assigned_email or "").strip().lower()
    if not address:
        raise RequestError(_("This request is not addressed to an email address."))

    ResponderToken.objects.filter(
        request=request, email=address, status=ResponderToken.Status.ACTIVE
    ).update(status=ResponderToken.Status.REVOKED, revoked_at=timezone.now())

    raw = secrets.token_urlsafe(32)
    token = ResponderToken.objects.create(
        request=request,
        email=address,
        token_hash=_hash_token(raw),
        expires_at=timezone.now() + timedelta(days=RESPONDER_TOKEN_DAYS),
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )

    log(
        request,
        kind=RequestEvent.Kind.NOTE,
        actor=actor,
        note=f"Responder link issued to {address}.",
    )
    record_event(
        action=AuditAction.CREATE,
        actor=actor,
        obj=request,
        after={"responder_link": address},
        context={"token_id": str(token.pk)},
    )
    return token, raw


def resolve_responder_token(raw: str) -> ResponderToken | None:
    """The live token for this link, or ``None``.

    Looked up by hash, so an expired or revoked link is indistinguishable from a
    forged one to the caller — which is the point.

    ``rls_bootstrap`` is required and easy to miss, because the token table
    itself is not protected: ``ResponderToken`` is deliberately not tenant-scoped
    and carries no policy. What is protected is what it joins to. Resolving the
    token means reading the request behind it to find out which tenant and entity
    the scope should cover — and that read happens *before* any scope exists, so
    the policy on ``rfi_informationrequest`` fails closed and the join yields
    nothing. The symptom is a perfectly valid link answering 404, with no error
    anywhere.

    This is the same chicken-and-egg as ``scope_resolver._select_membership``,
    and the same answer: lift the policy for the one anchored read that
    establishes the scope, and close it immediately. The anchor here is the token
    hash, which nobody can produce without holding the link.
    """
    if not raw:
        return None

    # The setting is transaction-local, so there has to be a transaction for it
    # to be local to. A GET arrives outside one.
    with transaction.atomic(), rls_bootstrap():
        token = (
            ResponderToken.objects.filter(token_hash=_hash_token(raw))
            .select_related("request", "request__entity", "request__requested_by")
            .first()
        )
        if token is None or not token.is_live:
            return None
        return token


@contextmanager
def responder_scope(token: ResponderToken) -> Iterator[Any]:
    """Bind exactly what the link holder may reach, for the duration of a request.

    Narrowed twice over: to the one entity the request is about, and to three
    permissions. The tenant is readable but **not writable**, so nothing outside
    the items being answered can be created or changed even if a view were wired
    up carelessly later.

    Built on ``tenant_context`` rather than beside it, so this holder is subject
    to the same scoped managers and the same Row-Level Security policy as
    everybody else. A second access path that bypassed those would be exactly the
    thing this module's isolation tests exist to prevent.
    """
    information_request = token.request
    with tenant_context(
        tenant_ids={information_request.tenant_id},
        writable_tenant_ids={information_request.tenant_id},
        entity_ids={information_request.entity_id},
        reason=f"rfi:responder:{token.pk}",
        permissions=RESPONDER_PERMISSIONS,
    ) as scope:
        yield scope


def record_responder_use(token: ResponderToken) -> None:
    """Stamp the link as used. Not single-use — see the model."""
    ResponderToken.objects.filter(pk=token.pk).update(
        last_used_at=timezone.now(), use_count=F("use_count") + 1
    )


def send_responder_link(
    information_request: InformationRequest,
    *,
    token: ResponderToken,
    raw_token: str,
    request: Any = None,
) -> None:
    """Email the link. The only place the raw token is ever written down.

    Plain text and short. The recipient is a bookkeeper who was told to expect
    this, not a marketing audience, and a link buried in a template renders badly
    in half the clients a small business uses.
    """
    from django.core.mail import send_mail
    from django.urls import reverse

    path = reverse("respond:responder", kwargs={"token": raw_token})
    url = request.build_absolute_uri(path) if request is not None else path

    outstanding = [item.label for item in information_request.items.all() if not item.is_answered]
    lines = [
        f"{information_request.requested_by} has asked you for the following:",
        "",
        *(f"  - {label}" for label in outstanding),
        "",
        "You can answer here — no account needed:",
        url,
        "",
        f"The link works for {RESPONDER_TOKEN_DAYS} days.",
    ]

    send_mail(
        subject=f"Requested: {information_request.title}",
        message="\n".join(lines),
        from_email=None,
        recipient_list=[token.email],
        fail_silently=False,
    )
    logger.info(
        "rfi.responder_link_sent",
        request_id=str(information_request.pk),
        email=token.email,
    )
