"""
Recording notices, moving them along, and closing them out.

The transition rules here are looser than the obligation lifecycle on purpose. A
notice does not follow a predictable path: an authority can reopen a matter after
a response, a hearing can be adjourned twice, and a scrutiny can turn into a
demand. What is enforced is that every move is recorded with a date, and that
responding and closing both require the evidence a later appeal would need.
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
from stacos.notices.models import Notice, NoticeEvent, NoticeState

logger = structlog.get_logger(__name__)

__all__ = ["NoticeError", "close_notice", "record_notice", "record_response", "transition"]

#: Moves that require evidence rather than only a state change.
_TERMINAL = frozenset({NoticeState.CLOSED, NoticeState.ESCALATED})


class NoticeError(Exception):
    """Something a user did that cannot be done, phrased for a user."""


def _actor_label(actor: Any) -> str:
    if actor is None or not getattr(actor, "is_authenticated", False):
        return "STACOS"
    return (getattr(actor, "audit_label", None) or str(actor))[:200]


def log(
    notice: Notice,
    *,
    kind: str,
    actor: Any = None,
    note: str = "",
    occurred_on: date | None = None,
    **context: Any,
) -> NoticeEvent:
    return NoticeEvent.objects.create(
        tenant_id=notice.tenant_id,
        entity_id=notice.entity_id,
        notice=notice,
        kind=kind,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=note,
        occurred_on=occurred_on or timezone.localdate(),
        context=context,
    )


@transaction.atomic
def record_notice(
    *,
    tenant: Any,
    entity: Any,
    authority: Any,
    reference_number: str,
    subject: str,
    received_on: date,
    notice_type: str = "OTHER",
    respond_by: date | None = None,
    actor: Any = None,
    **extra: Any,
) -> Notice:
    """Create a notice and open its timeline.

    ``respond_by`` is left null when the notice does not state one. It is *not*
    defaulted to thirty days: a guessed statutory deadline presented as fact is
    the mistake the calendar engine refuses to make, and this module holds the
    same line. The UI offers the suggestion and makes the user accept it.
    """
    notice = Notice.objects.create(
        tenant=tenant,
        entity=entity,
        authority=authority,
        reference_number=reference_number.strip()[:120],
        subject=subject.strip()[:300],
        received_on=received_on,
        notice_type=notice_type,
        respond_by=respond_by,
        **extra,
    )

    log(
        notice,
        kind=NoticeEvent.Kind.RECEIVED,
        actor=actor,
        occurred_on=received_on,
        note=subject[:250],
    )
    record_event(
        action=AuditAction.CREATE,
        actor=actor,
        obj=notice,
        after={
            "reference_number": notice.reference_number,
            "notice_type": notice.notice_type,
            "respond_by": notice.respond_by.isoformat() if notice.respond_by else None,
        },
    )
    return notice


@transaction.atomic
def transition(
    notice: Notice,
    *,
    target: str,
    actor: Any = None,
    note: str = "",
    occurred_on: date | None = None,
) -> Notice:
    """Move a notice to another state.

    Any open state can reach any other: an authority's behaviour is not a state
    machine we control. The one rule enforced is that leaving a terminal state
    requires saying why, because reopening a closed matter is a claim somebody
    should have to stand behind.
    """
    if target not in NoticeState.values:
        raise NoticeError(_("That is not a known state."))

    previous = notice.state
    if previous == target:
        return notice

    if previous in _TERMINAL and not note.strip():
        raise NoticeError(_("Reopening a closed notice needs a reason."))

    notice.state = target
    fields = ["state", "updated_at"]

    if target == NoticeState.CLOSED:
        notice.closed_at = timezone.now()
        fields.append("closed_at")
    elif previous == NoticeState.CLOSED:
        notice.closed_at = None
        fields.append("closed_at")

    notice.save(update_fields=fields)

    log(
        notice,
        kind=NoticeEvent.Kind.STATE_CHANGED,
        actor=actor,
        note=note,
        occurred_on=occurred_on,
        from_state=previous,
        to_state=target,
    )
    record_event(
        action=AuditAction.TRANSITION,
        actor=actor,
        obj=notice,
        before={"state": previous},
        after={"state": target},
        context={"note": note},
    )
    return notice


@transaction.atomic
def record_response(
    notice: Notice,
    *,
    responded_on: date,
    reference: str,
    actor: Any = None,
    note: str = "",
) -> Notice:
    """Record that the authority has been answered.

    The reference is required. A response with no acknowledgement cannot be
    proved, and "we replied" without one is exactly the claim that collapses at
    appeal.
    """
    if not reference.strip():
        raise NoticeError(
            _(
                "Record the acknowledgement or submission reference — a response that cannot be evidenced is not a response."
            )
        )

    notice.responded_on = responded_on
    notice.response_reference = reference.strip()[:120]
    notice.state = NoticeState.RESPONDED
    notice.save(update_fields=["responded_on", "response_reference", "state", "updated_at"])

    log(
        notice,
        kind=NoticeEvent.Kind.RESPONSE_FILED,
        actor=actor,
        note=note,
        occurred_on=responded_on,
        reference=notice.response_reference,
    )
    record_event(
        action=AuditAction.TRANSITION,
        actor=actor,
        obj=notice,
        after={"state": NoticeState.RESPONDED, "reference": notice.response_reference},
    )
    return notice


@transaction.atomic
def close_notice(
    notice: Notice, *, outcome: str, actor: Any = None, accepted_amount: Any = None
) -> Notice:
    """Close a notice with its outcome.

    The outcome is free text and required. Six months later the only question
    anyone asks about a closed notice is how it ended, and a state of ``CLOSED``
    on its own does not answer it.
    """
    if not outcome.strip():
        raise NoticeError(
            _("Record how this ended — that is the only thing anyone will ask later.")
        )

    notice.outcome = outcome.strip()
    notice.state = NoticeState.CLOSED
    notice.closed_at = timezone.now()
    fields = ["outcome", "state", "closed_at", "updated_at"]

    if accepted_amount is not None:
        notice.accepted_amount = accepted_amount
        fields.append("accepted_amount")

    notice.save(update_fields=fields)

    log(notice, kind=NoticeEvent.Kind.ORDER, actor=actor, note=outcome[:250])
    record_event(
        action=AuditAction.TRANSITION,
        actor=actor,
        obj=notice,
        after={"state": NoticeState.CLOSED, "outcome": outcome[:250]},
    )
    return notice


def ingest_retrieved(retrieved: Any, *, tenant: Any, entity: Any, authority: Any) -> Notice | None:
    """Turn an adapter's or an email parser's output into a notice.

    Deliberately the same path as manual entry: whether a person typed it or a
    machine found it changes only ``source``. Returns ``None`` when the reference
    already exists, because both email and any future adapter will re-present the
    same notice on every run.
    """
    existing = Notice.objects.filter(
        entity=entity, authority=authority, reference_number=retrieved.reference_number
    ).first()
    if existing is not None:
        return None

    return record_notice(
        tenant=tenant,
        entity=entity,
        authority=authority,
        reference_number=retrieved.reference_number,
        subject=retrieved.subject,
        received_on=retrieved.received_on or timezone.localdate(),
        notice_type=retrieved.notice_type,
        respond_by=retrieved.respond_by,
        statutory_reference=retrieved.statutory_reference,
        summary=retrieved.summary,
        period_key=retrieved.period_key,
        issued_on=retrieved.issued_on,
        source=Notice.Source.PORTAL,
    )
