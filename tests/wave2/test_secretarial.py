"""
Secretarial: quorum, the cap-table ledger, and the link back to the calendar.

The most valuable assertion here is the last one — recording an AGM has to fill
in the date AOC-4 was waiting for. That join is the difference between two
modules and one product.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.obligations.models import EntityEvent, ObligationInstance
from stacos.secretarial.models import (
    Meeting,
    MeetingAttendee,
    Resolution,
    Shareholder,
    ShareTransaction,
)
from stacos.secretarial.services import (
    MeetingError,
    board_meeting_gaps,
    mark_held,
    next_board_meeting_due,
    pending_mgt14,
    record_attendance,
)
from stacos.tenancy.models import Entity, Tenant
from tests.conftest import AS_OF, sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    return sign_in(client, org_owner, step_up=True)


def _meeting(org: Tenant, entity: Entity, kind: str, when: date, quorum: int = 2) -> Meeting:
    return Meeting.objects.create(
        tenant=org,
        entity=entity,
        kind=kind,
        scheduled_for=when,
        quorum_required=quorum,
    )


# ===========================================================================
# Quorum
# ===========================================================================


def test_a_meeting_without_quorum_cannot_be_recorded_as_held(org: Tenant, entity_a: Entity) -> None:
    """Resolutions passed without quorum are not valid.

    Letting them into the minute book creates a problem that surfaces years
    later at diligence, when it is expensive and undeniable.
    """
    with platform_scope(reason="test"):
        meeting = _meeting(org, entity_a, Meeting.Kind.BOARD, AS_OF, quorum=3)
        record_attendance(meeting, name="A Director")
        record_attendance(meeting, name="B Director")

        with pytest.raises(MeetingError) as exc:
            mark_held(meeting, held_on=AS_OF)

    assert "quorum" in str(exc.value).lower()


def test_quorum_is_computed_from_attendance(org: Tenant, entity_a: Entity) -> None:
    """Never typed in, so the two cannot disagree."""
    with platform_scope(reason="test"):
        meeting = _meeting(org, entity_a, Meeting.Kind.BOARD, AS_OF, quorum=2)
        record_attendance(meeting, name="A Director")
        record_attendance(meeting, name="B Director", attendance=MeetingAttendee.Attendance.VIDEO)
        record_attendance(meeting, name="C Director", attendance=MeetingAttendee.Attendance.ABSENT)

        mark_held(meeting, held_on=AS_OF)
        meeting.refresh_from_db()

    # Present plus video counts; absent does not.
    assert meeting.quorum_present == 2
    assert meeting.has_quorum


def test_quorum_is_unknown_until_attendance_is_recorded(org: Tenant, entity_a: Entity) -> None:
    """ "We do not know yet" and "quorum was not met" are different facts."""
    with platform_scope(reason="test"):
        meeting = _meeting(org, entity_a, Meeting.Kind.BOARD, AS_OF)

    assert meeting.has_quorum is None


def test_an_attendee_is_recorded_once(org: Tenant, entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        meeting = _meeting(org, entity_a, Meeting.Kind.BOARD, AS_OF)
        record_attendance(meeting, name="A Director")
        record_attendance(meeting, name="A Director", attendance=MeetingAttendee.Attendance.VIDEO)

        assert meeting.attendees.count() == 1
        assert meeting.attendees.get().attendance == MeetingAttendee.Attendance.VIDEO


# ===========================================================================
# The link back to the compliance calendar
# ===========================================================================


def test_recording_an_agm_schedules_the_roc_filings(materialised: Entity) -> None:
    """The join that makes this one product rather than two modules.

    Before: AOC-4 sits with a null due date and a prompt. After: it has a date,
    computed thirty days from the meeting.
    """
    with platform_scope(reason="test"):
        before = ObligationInstance.objects.filter(
            entity=materialised, definition_code="IN-MCA-AOC4"
        ).first()
        assert before is not None
        assert before.due_date is None
        assert before.needs_input == "AGM_DATE"

        meeting = Meeting.objects.create(
            tenant=materialised.tenant,
            entity=materialised,
            kind=Meeting.Kind.AGM,
            scheduled_for=date(2026, 9, 25),
            quorum_required=2,
        )
        record_attendance(meeting, name="A Director")
        record_attendance(meeting, name="B Director")
        mark_held(meeting, held_on=date(2026, 9, 25))

        assert EntityEvent.objects.filter(entity=materialised, key="AGM_DATE").exists()

        before.refresh_from_db()

    assert before.due_date == date(2026, 10, 25)
    assert before.needs_input == ""


def test_a_committee_meeting_anchors_nothing(org: Tenant, entity_a: Entity) -> None:
    """Only meetings the calendar actually keys on write an event."""
    with platform_scope(reason="test"):
        meeting = _meeting(org, entity_a, Meeting.Kind.AUDIT_COMMITTEE, AS_OF)
        record_attendance(meeting, name="A Director")
        record_attendance(meeting, name="B Director")
        mark_held(meeting, held_on=AS_OF, rebuild_calendar=False)

        assert not EntityEvent.objects.filter(entity=entity_a).exists()


# ===========================================================================
# The 120-day board meeting interval
# ===========================================================================


def test_the_interval_rule_is_measured_not_projected(org: Tenant, entity_a: Entity) -> None:
    """Four meetings in six months satisfies the count and breaches the interval."""
    with platform_scope(reason="test"):
        for offset in (0, 200):
            meeting = _meeting(org, entity_a, Meeting.Kind.BOARD, AS_OF - timedelta(days=offset))
            record_attendance(meeting, name="A Director")
            record_attendance(meeting, name="B Director")
            mark_held(meeting, held_on=AS_OF - timedelta(days=offset), rebuild_calendar=False)

        gaps = board_meeting_gaps(entity_a.pk, as_of=AS_OF)

    assert gaps, "a 200-day gap was not reported"
    assert any(gap["days"] == 200 for gap in gaps)


def test_the_open_ended_gap_is_reported_too(org: Tenant, entity_a: Entity) -> None:
    """Time since the last meeting is the gap a company can still act on."""
    with platform_scope(reason="test"):
        meeting = _meeting(org, entity_a, Meeting.Kind.BOARD, AS_OF - timedelta(days=200))
        record_attendance(meeting, name="A Director")
        record_attendance(meeting, name="B Director")
        mark_held(meeting, held_on=AS_OF - timedelta(days=200), rebuild_calendar=False)

        gaps = board_meeting_gaps(entity_a.pk, as_of=AS_OF)
        due = next_board_meeting_due(entity_a.pk)

    assert any(gap["to"] is None for gap in gaps)
    assert due == AS_OF - timedelta(days=200) + timedelta(days=120)


def test_no_meetings_means_no_projection(org: Tenant, entity_a: Entity) -> None:
    """One step ahead only. Projecting a chain further is guessing."""
    with platform_scope(reason="test"):
        assert next_board_meeting_due(entity_a.pk) is None
        assert board_meeting_gaps(entity_a.pk, as_of=AS_OF) == []


# ===========================================================================
# MGT-14
# ===========================================================================


def test_a_special_resolution_awaiting_mgt14_is_surfaced(org: Tenant, entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        resolution = Resolution.objects.create(
            tenant=org,
            entity=entity_a,
            kind=Resolution.Kind.SPECIAL,
            subject="Alteration of the articles",
            passed_on=AS_OF - timedelta(days=40),
            requires_mgt14=True,
        )
        pending = pending_mgt14(entity_a.pk)

    assert resolution in pending
    assert resolution.mgt14_is_overdue


def test_a_filed_resolution_is_not_pending(org: Tenant, entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        Resolution.objects.create(
            tenant=org,
            entity=entity_a,
            kind=Resolution.Kind.SPECIAL,
            subject="Increase in authorised capital",
            passed_on=AS_OF - timedelta(days=40),
            requires_mgt14=True,
            mgt14_filed_on=AS_OF - timedelta(days=20),
            mgt14_srn="AA1234567",
        )
        assert pending_mgt14(entity_a.pk) == []


# ===========================================================================
# The cap table is a ledger
# ===========================================================================


def test_holdings_are_derived_by_replaying_the_ledger(org: Tenant, entity_a: Entity) -> None:
    """Never a stored balance.

    A stored balance and a transaction history disagree eventually, and when they
    do nobody can tell which is right.
    """
    with platform_scope(reason="test"):
        holder = Shareholder.objects.create(tenant=org, entity=entity_a, name="Anita Rao")
        other = Shareholder.objects.create(tenant=org, entity=entity_a, name="Vikram Singh")

        for shareholder, kind, quantity, when in (
            (holder, ShareTransaction.Kind.ALLOTMENT, 10000, date(2020, 4, 1)),
            (holder, ShareTransaction.Kind.TRANSFER_OUT, -2500, date(2023, 7, 15)),
            (other, ShareTransaction.Kind.TRANSFER_IN, 2500, date(2023, 7, 15)),
            (holder, ShareTransaction.Kind.BONUS, 1000, date(2025, 1, 10)),
        ):
            ShareTransaction.objects.create(
                tenant=org,
                entity=entity_a,
                shareholder=shareholder,
                kind=kind,
                quantity=quantity,
                face_value=Decimal("10"),
                executed_on=when,
            )

        assert holder.holding_as_of() == 8500
        assert other.holding_as_of() == 2500
        # And as at an earlier date, which a stored balance simply cannot answer.
        assert holder.holding_as_of(date(2023, 1, 1)) == 10000


def test_a_zero_quantity_transaction_is_refused(org: Tenant, entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        holder = Shareholder.objects.create(tenant=org, entity=entity_a, name="Anita Rao")
        with pytest.raises(IntegrityError), transaction.atomic():
            ShareTransaction.objects.create(
                tenant=org,
                entity=entity_a,
                shareholder=holder,
                kind=ShareTransaction.Kind.ALLOTMENT,
                quantity=0,
                face_value=Decimal("10"),
                executed_on=AS_OF,
            )


def test_the_cap_table_renders_and_totals_to_a_hundred(
    signed_in: Client, org: Tenant, entity_a: Entity
) -> None:
    with platform_scope(reason="test"):
        for name, quantity in (("Anita Rao", 7500), ("Vikram Singh", 2500)):
            holder = Shareholder.objects.create(tenant=org, entity=entity_a, name=name)
            ShareTransaction.objects.create(
                tenant=org,
                entity=entity_a,
                shareholder=holder,
                kind=ShareTransaction.Kind.ALLOTMENT,
                quantity=quantity,
                face_value=Decimal("10"),
                executed_on=date(2020, 4, 1),
            )

    response = signed_in.get(
        reverse("secretarial:cap_table", args=[entity_a.pk]), headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert b"Anita Rao" in response.content
    assert b"75.00" in response.content


# ===========================================================================
# Over HTTP
# ===========================================================================


def test_the_meeting_list_renders_both_ways(
    signed_in: Client, org: Tenant, entity_a: Entity
) -> None:
    with platform_scope(reason="test"):
        _meeting(org, entity_a, Meeting.Kind.BOARD, AS_OF)

    page = signed_in.get(reverse("secretarial:meetings"))
    fragment = signed_in.get(reverse("secretarial:meetings"), headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b"Board meeting" in fragment.content


def test_scheduling_a_meeting_through_the_modal(signed_in: Client, entity_a: Entity) -> None:
    response = signed_in.post(
        reverse("secretarial:meeting_create"),
        {
            "entity": str(entity_a.pk),
            "kind": "BOARD",
            "scheduled_for": "2026-09-30",
            "quorum_required": 2,
            "serial_number": "2nd Board Meeting FY2026-27",
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200, response.content[:300]

    with platform_scope(reason="test"):
        assert Meeting.objects.filter(entity=entity_a, kind="BOARD").exists()
