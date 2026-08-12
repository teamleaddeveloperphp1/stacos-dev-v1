"""
Recording meetings, and the one link that matters most.

Marking a meeting held writes an :class:`~stacos.obligations.models.EntityEvent`
and rebuilds the entity's calendar. That is not a convenience — it is the join
that stops a company holding its AGM while AOC-4, MGT-7 and ADT-1 all sit
unscheduled, waiting for a date somebody has already recorded somewhere else in
the product.
"""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.obligations.models import EntityEvent
from stacos.secretarial.models import Meeting, MeetingAttendee, Resolution

logger = structlog.get_logger(__name__)

__all__ = ["MeetingError", "board_meeting_gaps", "mark_held", "pending_mgt14"]

#: A company must hold at least four board meetings a year with no more than this
#: many days between any two. Four meetings crammed into six months satisfies the
#: count and breaches the interval, which is the half everybody misses.
MAX_BOARD_MEETING_GAP_DAYS = 120


class MeetingError(Exception):
    """Something a user did that cannot be done, phrased for a user."""


@transaction.atomic
def mark_held(
    meeting: Meeting,
    *,
    held_on: date,
    actor: Any = None,
    rebuild_calendar: bool = True,
) -> Meeting:
    """Record that a meeting happened, and unblock whatever was waiting on it.

    Quorum is computed from the attendance register rather than typed in, so the
    two cannot disagree — and a meeting recorded without quorum is refused,
    because resolutions passed at it are not valid and letting them into the
    minute book creates a problem that surfaces years later at diligence.
    """
    if meeting.state == Meeting.State.CANCELLED:
        raise MeetingError(_("This meeting was cancelled."))

    attendees = list(meeting.attendees.all())
    present = sum(1 for attendee in attendees if attendee.counts_towards_quorum)

    if attendees and present < meeting.quorum_required:
        raise MeetingError(
            _(
                "Only %(present)d of the required %(required)d attended. Resolutions passed "
                "without quorum are not valid — record the attendance first, or cancel it."
            )
            % {"present": present, "required": meeting.quorum_required}
        )

    meeting.held_on = held_on
    meeting.quorum_present = present if attendees else None
    meeting.state = Meeting.State.HELD
    meeting.save(update_fields=["held_on", "quorum_present", "state", "updated_at"])

    # The join to the compliance engine. Without it a company holds its AGM and
    # its ROC filings stay unscheduled.
    if meeting.event_key:
        EntityEvent.objects.update_or_create(
            entity_id=meeting.entity_id,
            key=meeting.event_key,
            scope_ref="",
            occurred_on=held_on,
            defaults={
                "tenant_id": meeting.tenant_id,
                "note": str(meeting)[:250],
                "recorded_by": actor if getattr(actor, "is_authenticated", False) else None,
            },
        )
        if rebuild_calendar:
            _rebuild(meeting, actor=actor)

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=meeting,
        after={"state": Meeting.State.HELD, "held_on": held_on.isoformat()},
    )
    return meeting


def _rebuild(meeting: Meeting, *, actor: Any) -> None:
    """Re-materialise the entity's calendar after an anchor date changed.

    Failure-tolerant: a meeting was genuinely held whether or not the rebuild
    succeeds, and rolling back the minute book because the planner had a bad day
    would be the wrong trade.
    """
    from stacos.obligations.services import materialise

    try:
        materialise(
            meeting.entity,
            as_of=timezone.localdate(),
            trigger="MANUAL",
            actor=actor,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("secretarial.rebuild_failed", meeting_id=str(meeting.pk), error=str(exc))


@transaction.atomic
def record_attendance(
    meeting: Meeting,
    *,
    name: str,
    role: str = MeetingAttendee.Role.DIRECTOR,
    attendance: str = MeetingAttendee.Attendance.PRESENT,
    din: str = "",
    is_interested: bool = False,
) -> MeetingAttendee:
    attendee, _created = MeetingAttendee.objects.update_or_create(
        meeting=meeting,
        name=name[:200],
        defaults={
            "tenant_id": meeting.tenant_id,
            "entity_id": meeting.entity_id,
            "role": role,
            "attendance": attendance,
            "din": din[:20],
            "is_interested": is_interested,
        },
    )
    return attendee


def board_meeting_gaps(entity_id: Any, *, as_of: date) -> list[dict[str, Any]]:
    """Intervals between consecutive board meetings that breach the limit.

    Computed from what was actually held rather than projected forward. Projecting
    a sequential dependency beyond the next occurrence is speculation, and a
    client shown a projection treats it as a commitment.
    """
    held = list(
        Meeting.objects.filter(
            entity_id=entity_id,
            kind=Meeting.Kind.BOARD,
            state__in=[Meeting.State.HELD, Meeting.State.MINUTED],
            held_on__isnull=False,
        )
        .order_by("held_on")
        .values_list("held_on", flat=True)
    )

    breaches: list[dict[str, Any]] = []
    for earlier, later in itertools.pairwise(held):
        gap = (later - earlier).days
        if gap > MAX_BOARD_MEETING_GAP_DAYS:
            breaches.append({"from": earlier, "to": later, "days": gap})

    # The open-ended gap: time since the last meeting, which is the one a company
    # can still do something about.
    if held:
        since = (as_of - held[-1]).days
        if since > MAX_BOARD_MEETING_GAP_DAYS:
            breaches.append({"from": held[-1], "to": None, "days": since})

    return breaches


def pending_mgt14(entity_id: Any) -> list[Resolution]:
    """Resolutions that need filing with the Registrar and have not been.

    Thirty days from passing, and the filing is easy to forget because the
    resolution itself felt like the end of the task.
    """
    return list(
        Resolution.objects.filter(
            entity_id=entity_id, requires_mgt14=True, mgt14_filed_on__isnull=True
        ).order_by("passed_on")
    )


def next_board_meeting_due(entity_id: Any) -> date | None:
    """When the next board meeting must be held by, or ``None`` if none was held.

    One step ahead only. The interval rule chains, and projecting it further is
    guessing at dates a company has not decided yet.
    """
    last = (
        Meeting.objects.filter(
            entity_id=entity_id,
            kind=Meeting.Kind.BOARD,
            state__in=[Meeting.State.HELD, Meeting.State.MINUTED],
            held_on__isnull=False,
        )
        .order_by("-held_on")
        .values_list("held_on", flat=True)
        .first()
    )
    if last is None:
        return None
    return last + timedelta(days=MAX_BOARD_MEETING_GAP_DAYS)
