"""
The notice tracker: deadlines that are never guessed, and responses that can be
evidenced.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.jurisdictions.models import Authority
from stacos.notices.models import Notice, NoticeState
from stacos.notices.portals import available_adapters
from stacos.notices.services import (
    NoticeError,
    close_notice,
    record_notice,
    record_response,
    transition,
)
from stacos.tenancy.models import Entity, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

TODAY = date(2026, 8, 12)


@pytest.fixture
def cbdt(db: None) -> Authority:
    with platform_scope(reason="test-fixture"):
        return Authority.objects.get(code="CBDT")


@pytest.fixture
def a_notice(org: Tenant, entity_a: Entity, cbdt: Authority) -> Notice:
    with platform_scope(reason="test-fixture"):
        return record_notice(
            tenant=org,
            entity=entity_a,
            authority=cbdt,
            reference_number="DIN2026081200123",
            subject="Scrutiny under Section 143(2) for AY 2025-26",
            received_on=TODAY - timedelta(days=3),
            notice_type="SCRUTINY",
            respond_by=TODAY + timedelta(days=12),
            statutory_reference="Section 143(2) Income-tax Act",
            demand_amount=None,
        )


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    """Signed in with step-up fresh.

    The tracker's sensitive actions — recording a response, closing a notice —
    demand re-authentication, so a fixture without it would test the redirect
    rather than the action.
    """
    return sign_in(client, org_owner, step_up=True)


# ===========================================================================
# Deadlines are never invented
# ===========================================================================


def test_a_notice_with_no_stated_deadline_keeps_a_null_one(
    org: Tenant, entity_a: Entity, cbdt: Authority
) -> None:
    """The thirty-day convention is a suggestion, never a stored fact.

    An officer who gave fifteen days did not give thirty, and a calendar that
    quietly says otherwise is worse than one that admits it does not know.
    """
    with platform_scope(reason="test"):
        notice = record_notice(
            tenant=org,
            entity=entity_a,
            authority=cbdt,
            reference_number="DIN-NO-DATE",
            subject="Intimation",
            received_on=TODAY,
        )

    assert notice.respond_by is None
    assert notice.days_remaining is None
    assert notice.suggested_response_deadline() == TODAY + timedelta(days=30)


def test_days_remaining_and_overdue(a_notice: Notice) -> None:
    with platform_scope(reason="test"):
        assert a_notice.days_remaining is not None
        assert not a_notice.is_overdue

        a_notice.respond_by = TODAY - timedelta(days=1)
        a_notice.save(update_fields=["respond_by"])
        assert a_notice.is_overdue


def test_a_deadline_before_receipt_is_refused(
    signed_in: Client, entity_a: Entity, cbdt: Authority
) -> None:
    response = signed_in.post(
        reverse("notices:create"),
        {
            "entity": str(entity_a.pk),
            "authority": str(cbdt.pk),
            "reference_number": "DIN-BACKWARDS",
            "notice_type": "DEMAND",
            "subject": "Demand",
            "received_on": "2026-08-12",
            "respond_by": "2026-08-01",
            "risk": "MEDIUM",
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert b"cannot be before the notice arrived" in response.content


# ===========================================================================
# Responses have to be evidenceable
# ===========================================================================


def test_a_response_without_a_reference_is_refused(a_notice: Notice) -> None:
    """ "We replied" with no acknowledgement is the claim that collapses at appeal."""
    with platform_scope(reason="test"), pytest.raises(NoticeError):
        record_response(a_notice, responded_on=TODAY, reference="  ")


def test_recording_a_response_moves_the_state(a_notice: Notice) -> None:
    with platform_scope(reason="test"):
        record_response(a_notice, responded_on=TODAY, reference="ACK/2026/9912")
        a_notice.refresh_from_db()

    assert a_notice.state == NoticeState.RESPONDED
    assert a_notice.response_reference == "ACK/2026/9912"


def test_closing_needs_an_outcome(a_notice: Notice) -> None:
    """Six months later the only question is how it ended."""
    with platform_scope(reason="test"), pytest.raises(NoticeError):
        close_notice(a_notice, outcome="")


def test_closing_records_the_outcome_and_the_disputed_figure(
    org: Tenant, entity_a: Entity, cbdt: Authority
) -> None:
    with platform_scope(reason="test"):
        notice = record_notice(
            tenant=org,
            entity=entity_a,
            authority=cbdt,
            reference_number="DIN-DEMAND",
            subject="Demand under Section 156",
            received_on=TODAY,
            notice_type="DEMAND",
            demand_amount=1000000,
        )
        close_notice(notice, outcome="Reduced on rectification.", accepted_amount=250000)
        notice.refresh_from_db()

    assert notice.state == NoticeState.CLOSED
    assert notice.outcome
    assert notice.disputed_amount == 750000


def test_reopening_a_closed_notice_needs_a_reason(a_notice: Notice) -> None:
    with platform_scope(reason="test"):
        close_notice(a_notice, outcome="Dropped.")
        with pytest.raises(NoticeError):
            transition(a_notice, target=NoticeState.UNDER_REVIEW)

        transition(
            a_notice,
            target=NoticeState.UNDER_REVIEW,
            note="Authority issued a further letter on 20 September.",
        )
        a_notice.refresh_from_db()

    assert a_notice.state == NoticeState.UNDER_REVIEW
    assert a_notice.closed_at is None


def test_every_move_lands_on_the_timeline(a_notice: Notice) -> None:
    with platform_scope(reason="test"):
        transition(a_notice, target=NoticeState.UNDER_REVIEW)
        transition(a_notice, target=NoticeState.DRAFTING)
        kinds = list(a_notice.events.values_list("kind", flat=True))

    assert "RECEIVED" in kinds
    assert kinds.count("STATE_CHANGED") == 2


def test_the_same_reference_cannot_be_recorded_twice(
    org: Tenant, entity_a: Entity, cbdt: Authority, a_notice: Notice
) -> None:
    """Both email ingestion and any future adapter re-present the same notice."""
    from django.db import IntegrityError

    with platform_scope(reason="test"), pytest.raises(IntegrityError):
        record_notice(
            tenant=org,
            entity=entity_a,
            authority=cbdt,
            reference_number=a_notice.reference_number,
            subject="Duplicate",
            received_on=TODAY,
        )


# ===========================================================================
# No portal adapters, deliberately
# ===========================================================================


def test_no_portal_adapters_are_registered() -> None:
    """The interface exists and is empty, and that is the decision.

    Automated retrieval would mean holding client portal credentials and driving
    a session against terms of service that do not contemplate it. If an adapter
    ever appears, it should be because somebody wrote the legal position down —
    so this test failing is a prompt to check that they did.
    """
    assert available_adapters() == ()


def test_the_list_says_so_rather_than_offering_a_dead_button(
    signed_in: Client, a_notice: Notice
) -> None:
    response = signed_in.get(reverse("notices:list"), headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert b"We do not sign in to government portals on your behalf" in response.content


# ===========================================================================
# Over HTTP
# ===========================================================================


def test_the_list_renders_both_ways(signed_in: Client, a_notice: Notice) -> None:
    page = signed_in.get(reverse("notices:list"))
    fragment = signed_in.get(reverse("notices:list"), headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert a_notice.reference_number.encode() in fragment.content


def test_detail_renders_and_records_a_response(signed_in: Client, a_notice: Notice) -> None:
    page = signed_in.get(reverse("notices:detail", args=[a_notice.pk]))
    assert page.status_code == 200

    response = signed_in.post(
        reverse("notices:respond", args=[a_notice.pk]),
        {"responded_on": "2026-08-12", "reference": "ACK/2026/1", "note": "Filed online."},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert b"notice-panel" in response.content

    with platform_scope(reason="test"):
        a_notice.refresh_from_db()
    assert a_notice.state == NoticeState.RESPONDED


def test_an_undated_notice_is_surfaced_not_hidden(
    signed_in: Client, org: Tenant, entity_a: Entity, cbdt: Authority
) -> None:
    with platform_scope(reason="test"):
        record_notice(
            tenant=org,
            entity=entity_a,
            authority=cbdt,
            reference_number="DIN-UNDATED",
            subject="Letter",
            received_on=TODAY,
        )

    response = signed_in.get(
        reverse("notices:list"), {"status": "undated"}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert b"DIN-UNDATED" in response.content
