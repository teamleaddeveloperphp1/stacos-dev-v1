"""
Practice management: rates frozen at the date worked, WIP derived, and the
board a client can never see.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope, tenant_context
from stacos.practice.forms import rate_for
from stacos.practice.models import RateCard, TimeEntry, WorkItem, WorkItemState
from stacos.tenancy.models import Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

TODAY = date(2026, 8, 12)


@pytest.fixture
def practice_signed_in(client: Client, practice_staff: User, practice: Tenant) -> Client:
    return sign_in(client, practice_staff, step_up=True)


@pytest.fixture
def work_item(practice: Tenant, org: Tenant, practice_staff: User) -> WorkItem:
    with platform_scope(reason="test-fixture"):
        return WorkItem.objects.create(
            tenant=practice,
            client_tenant=org,
            title="August GST returns",
            due_on=TODAY + timedelta(days=8),
            assigned_to=practice_staff,
            estimated_hours=Decimal("6.00"),
        )


# ===========================================================================
# Rates are frozen at the date worked
# ===========================================================================


def test_the_rate_is_the_one_in_force_on_the_day(practice: Tenant, practice_staff: User) -> None:
    """A rise in April must not silently reprice work done in March."""
    with platform_scope(reason="test"):
        RateCard.objects.create(
            tenant=practice,
            user=practice_staff,
            rate=Decimal("1200"),
            valid_from=date(2025, 4, 1),
            valid_to=date(2026, 3, 31),
        )
        RateCard.objects.create(
            tenant=practice,
            user=practice_staff,
            rate=Decimal("1500"),
            valid_from=date(2026, 4, 1),
        )

        assert rate_for(practice_staff, on=date(2026, 3, 15)) == Decimal("1200")
        assert rate_for(practice_staff, on=date(2026, 8, 12)) == Decimal("1500")


def test_an_unrated_person_has_no_rate(practice_staff: User) -> None:
    with platform_scope(reason="test"):
        assert rate_for(practice_staff, on=TODAY) == Decimal("0")


def test_a_billable_entry_with_no_rate_is_refused(
    practice: Tenant, work_item: WorkItem, practice_staff: User
) -> None:
    """A hole in the WIP number that nobody notices until month end."""
    with platform_scope(reason="test"), pytest.raises(IntegrityError), transaction.atomic():
        TimeEntry.objects.create(
            tenant=practice,
            work_item=work_item,
            user=practice_staff,
            worked_on=TODAY,
            hours=Decimal("2.00"),
            rate=Decimal("0"),
            is_billable=True,
        )


# ===========================================================================
# WIP is derived
# ===========================================================================


def test_wip_is_recomputed_from_the_ledger(
    practice: Tenant, org: Tenant, work_item: WorkItem, practice_staff: User
) -> None:
    """Never a stored balance: rates change and entries get corrected."""
    with platform_scope(reason="test"):
        for hours, billable in ((Decimal("3.00"), True), (Decimal("2.00"), False)):
            TimeEntry.objects.create(
                tenant=practice,
                work_item=work_item,
                client_tenant=org,
                user=practice_staff,
                worked_on=TODAY,
                hours=hours,
                rate=Decimal("1500") if billable else Decimal("0"),
                is_billable=billable,
            )

        assert work_item.hours_logged() == Decimal("5.00")
        assert work_item.wip_value() == Decimal("4500.00")


def test_invoiced_time_leaves_work_in_progress(
    practice: Tenant, work_item: WorkItem, practice_staff: User
) -> None:
    from django.utils import timezone

    with platform_scope(reason="test"):
        entry = TimeEntry.objects.create(
            tenant=practice,
            work_item=work_item,
            user=practice_staff,
            worked_on=TODAY,
            hours=Decimal("4.00"),
            rate=Decimal("1500"),
        )
        assert work_item.wip_value() == Decimal("6000.00")

        entry.invoiced_at = timezone.now()
        entry.save(update_fields=["invoiced_at"])
        assert work_item.wip_value() == Decimal("0")


def test_over_estimate_is_flagged(
    practice: Tenant, work_item: WorkItem, practice_staff: User
) -> None:
    """The number a partner wants to see early, not at the invoice."""
    with platform_scope(reason="test"):
        TimeEntry.objects.create(
            tenant=practice,
            work_item=work_item,
            user=practice_staff,
            worked_on=TODAY,
            hours=Decimal("9.00"),
            rate=Decimal("1500"),
        )
        assert work_item.is_over_estimate


# ===========================================================================
# The board is the practice's, and only the practice's
# ===========================================================================


@pytest.mark.isolation
def test_a_client_cannot_see_the_practices_board(org: Tenant, work_item: WorkItem) -> None:
    """A client seeing its own file's estimate, assignment and margin is a
    commercial problem, and the tenant boundary is what prevents it."""
    with tenant_context(tenant_ids=org.id, reason="test:client"):
        assert WorkItem.objects.count() == 0
        assert TimeEntry.objects.count() == 0


def test_the_practice_sees_its_own_board(practice: Tenant, work_item: WorkItem) -> None:
    with tenant_context(tenant_ids=practice.id, reason="test:practice"):
        assert WorkItem.objects.filter(pk=work_item.pk).exists()


# ===========================================================================
# Over HTTP
# ===========================================================================


def test_the_board_renders_both_ways(practice_signed_in: Client, work_item: WorkItem) -> None:
    page = practice_signed_in.get(reverse("practice:board"))
    fragment = practice_signed_in.get(reverse("practice:board"), headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert work_item.title.encode() in fragment.content


def test_moving_a_card_records_the_time_it_started(
    practice_signed_in: Client, work_item: WorkItem
) -> None:
    response = practice_signed_in.post(
        reverse("practice:work_move", args=[work_item.pk]),
        {"state": WorkItemState.IN_PROGRESS},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    with platform_scope(reason="test"):
        work_item.refresh_from_db()
    assert work_item.state == WorkItemState.IN_PROGRESS
    assert work_item.started_at is not None


def test_logging_time_freezes_the_rate(
    practice_signed_in: Client,
    practice: Tenant,
    work_item: WorkItem,
    practice_staff: User,
) -> None:
    with platform_scope(reason="test"):
        RateCard.objects.create(
            tenant=practice, user=practice_staff, rate=Decimal("900"), valid_from=date(2026, 4, 1)
        )

    response = practice_signed_in.post(
        reverse("practice:time_log", args=[work_item.pk]),
        {
            "worked_on": "2026-08-12",
            "hours": "2.50",
            "narrative": "Prepared GSTR-3B",
            "is_billable": "on",
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    with platform_scope(reason="test"):
        entry = TimeEntry.objects.get(work_item=work_item)
    assert entry.rate == Decimal("900")
    assert entry.value == Decimal("2250.00")


def test_the_board_has_a_bounded_query_count(
    practice_signed_in: Client,
    practice: Tenant,
    org: Tenant,
    work_item: WorkItem,
    django_assert_max_num_queries: object,
) -> None:
    with platform_scope(reason="test"):
        for index in range(25):
            WorkItem.objects.create(tenant=practice, client_tenant=org, title=f"Item {index}")

    practice_signed_in.get(reverse("practice:board"))

    # A practice user's scope resolution walks engagements as well as
    # memberships, so the floor is a query or two above an organisation user's.
    with django_assert_max_num_queries(16):  # type: ignore[operator]
        response = practice_signed_in.get(reverse("practice:board"))
    assert response.status_code == 200
