"""
Event-driven materialisation through the real database.

The engine tests prove the planner's arithmetic without Postgres. These prove the
three things only the database can: that the unique constraint actually permits
two same-day events, that withdrawing one supersedes rather than destroys the
work already done against it, and that recording one rebuilds the calendar there
and then rather than tomorrow morning.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from stacos.core.scope import platform_scope
from stacos.engine.lifecycle import State
from stacos.obligations.models import (
    EntityEvent,
    MaterialisationRun,
    ObligationEvent,
    ObligationInstance,
)
from stacos.obligations.services import materialise
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

APPOINTMENT = "DIRECTOR_APPOINTED"
DIR12 = "IN-MCA-DIR12-APPOINTMENT"


@pytest.fixture
def org_client(client, org_owner):
    return sign_in(client, org_owner)


def _build(manufacturer, trigger=MaterialisationRun.Trigger.MANUAL):
    """Materialise inside a scope, the way the conftest fixtures do."""
    with platform_scope(reason="test"):
        return materialise(manufacturer, as_of=timezone.localdate(), trigger=trigger)


def _record(manufacturer, *, subject: str, ref: str, role: str, when: date) -> EntityEvent:
    with platform_scope(reason="test"):
        return EntityEvent.objects.create(
            tenant=manufacturer.tenant,
            entity=manufacturer,
            key=APPOINTMENT,
            occurred_on=when,
            subject_ref=ref,
            subject_label=subject,
            attributes={"role": role},
        )


def _dir12s(manufacturer) -> list[ObligationInstance]:
    with platform_scope(reason="test"):
        return list(
            ObligationInstance.objects.filter(
                entity=manufacturer, definition_code=DIR12, archived_at__isnull=True
            ).order_by("occurrence")
        )


def test_two_directors_appointed_on_one_day_produce_two_filings(manufacturer) -> None:
    """The case the original unique constraint made an IntegrityError.

    ``(manufacturer, key, scope_ref, occurred_on)`` had no room for a second
    appointment on the same morning, which is an ordinary thing for a board to
    do and a common thing for a new company to do on day one.
    """
    when = timezone.localdate() - timedelta(days=10)
    _record(manufacturer, subject="Ramesh Mehta", ref="DIN-01234567", role="DIRECTOR", when=when)
    _record(manufacturer, subject="Priya Nair", ref="DIN-07654321", role="MD", when=when)

    _build(manufacturer, MaterialisationRun.Trigger.MANUAL)

    filings = _dir12s(manufacturer)
    assert len(filings) == 2
    assert len({filing.occurrence for filing in filings}) == 2
    assert {filing.due_date for filing in filings} == {when + timedelta(days=30)}


def test_only_the_executive_appointment_produces_mr1(manufacturer) -> None:
    """``trigger.when`` filtering, through the real catalog."""
    when = timezone.localdate() - timedelta(days=10)
    _record(manufacturer, subject="Ramesh Mehta", ref="DIN-1", role="DIRECTOR", when=when)
    _record(manufacturer, subject="Priya Nair", ref="DIN-2", role="MD", when=when)

    _build(manufacturer, MaterialisationRun.Trigger.MANUAL)

    with platform_scope(reason="test"):
        mr1 = list(
            ObligationInstance.objects.filter(
                entity=manufacturer, definition_code="IN-MCA-MR1", archived_at__isnull=True
            )
        )
    assert len(mr1) == 1
    assert mr1[0].due_date == when + timedelta(days=60)


def test_materialising_twice_over_events_is_idempotent(manufacturer) -> None:
    when = timezone.localdate() - timedelta(days=10)
    _record(manufacturer, subject="Ramesh Mehta", ref="DIN-1", role="DIRECTOR", when=when)

    _build(manufacturer, MaterialisationRun.Trigger.MANUAL)
    before = _dir12s(manufacturer)
    run = _build(manufacturer, MaterialisationRun.Trigger.MANUAL)

    assert [filing.pk for filing in _dir12s(manufacturer)] == [filing.pk for filing in before]
    assert run.created_count == 0


def test_withdrawing_an_event_supersedes_work_already_done(manufacturer) -> None:
    """Nothing with history is destroyed — not even when the cause was a mistake.

    Somebody recorded the wrong appointment and somebody else had already started
    preparing the DIR-12. Withdrawing the event must take the filing off the live
    calendar without erasing the fact that work happened against it.
    """
    when = timezone.localdate() - timedelta(days=10)
    event = _record(manufacturer, subject="Ramesh Mehta", ref="DIN-1", role="DIRECTOR", when=when)
    _build(manufacturer, MaterialisationRun.Trigger.MANUAL)

    filing = _dir12s(manufacturer)[0]
    with platform_scope(reason="test"):
        filing.state = State.IN_PREPARATION
        filing.save(update_fields=["state"])
        ObligationEvent.objects.create(
            tenant=manufacturer.tenant,
            entity=manufacturer,
            obligation=filing,
            kind="TRANSITION",
            to_state=State.IN_PREPARATION,
        )
        event.superseded_at = timezone.now()
        event.save(update_fields=["superseded_at"])
    _build(manufacturer, MaterialisationRun.Trigger.EVENT_RECORDED)

    with platform_scope(reason="test"):
        filing.refresh_from_db()
        assert ObligationInstance.objects.filter(pk=filing.pk).exists()
    assert filing.superseded_at is not None
    # Retained, not deleted. The audit trail is the product.
    assert filing.state == State.NOT_APPLICABLE


def test_withdrawing_an_untouched_event_removes_its_filing_quietly(manufacturer) -> None:
    when = timezone.localdate() - timedelta(days=10)
    event = _record(manufacturer, subject="Ramesh Mehta", ref="DIN-1", role="DIRECTOR", when=when)
    _build(manufacturer, MaterialisationRun.Trigger.MANUAL)
    assert _dir12s(manufacturer)

    with platform_scope(reason="test"):
        event.superseded_at = timezone.now()
        event.save(update_fields=["superseded_at"])
    _build(manufacturer, MaterialisationRun.Trigger.EVENT_RECORDED)

    assert _dir12s(manufacturer) == []


def test_an_old_event_is_not_swept_away_by_the_horizon(manufacturer) -> None:
    """The deliberate asymmetry, through the register rather than the planner.

    A DIR-12 from eight months ago is due seven months ago — outside the 120-day
    lookback. If the horizon applied, the nightly job would stop desiring it and
    quietly archive the row carrying an uncapped daily penalty.
    """
    long_ago = timezone.localdate() - timedelta(days=240)
    _record(manufacturer, subject="Ramesh Mehta", ref="DIN-1", role="DIRECTOR", when=long_ago)

    _build(manufacturer, MaterialisationRun.Trigger.MANUAL)
    _build(manufacturer, MaterialisationRun.Trigger.NIGHTLY)

    filings = _dir12s(manufacturer)
    assert len(filings) == 1
    assert filings[0].due_date < timezone.localdate() - timedelta(days=120)


def test_recording_an_event_through_the_view_rebuilds_immediately(org_client, manufacturer) -> None:
    """The reason somebody typed the date is that something was waiting on it."""
    when = (timezone.localdate() - timedelta(days=5)).isoformat()

    response = org_client.post(
        reverse("compliance:event_create", args=[manufacturer.pk]),
        {
            "key": APPOINTMENT,
            "occurred_on": when,
            "subject_label": "Ramesh Mehta",
            "subject_ref": "DIN-01234567",
            f"attr_{APPOINTMENT}_role": "DIRECTOR",
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    with platform_scope(reason="test"):
        assert ObligationInstance.objects.filter(
            entity=manufacturer, definition_code=DIR12
        ).exists()
        run = MaterialisationRun.objects.filter(entity=manufacturer).order_by("-created_at").first()
        assert run is not None
        assert run.trigger == MaterialisationRun.Trigger.EVENT_RECORDED


def test_a_forged_event_key_is_refused(org_client, manufacturer) -> None:
    """The key is client-supplied. An unknown one would record an event that
    triggers nothing and is invisible everywhere."""
    response = org_client.post(
        reverse("compliance:event_create", args=[manufacturer.pk]),
        {"key": "NOT_A_REAL_EVENT", "occurred_on": timezone.localdate().isoformat()},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 422
    with platform_scope(reason="test"):
        assert not EntityEvent.objects.filter(key="NOT_A_REAL_EVENT").exists()
