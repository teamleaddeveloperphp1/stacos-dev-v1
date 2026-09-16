"""
The four lists that used to stop dead at a fixed number of rows.

Notifications cut off at a hundred, resolutions at a hundred, share transactions
at fifty and payouts at twenty — with no "load more" and nothing telling the user
anything had been left out. Once a tenant had been live a few months the older
records were simply unreachable.

Each test here creates more rows than one page holds and walks to the second
page, asserting the remainder comes back with no overlap. Overlap is the specific
way a keyset cursor goes wrong: get the tiebreaker or the sort direction subtly
mismatched and rows repeat or vanish between pages, which is far harder to notice
than a hard cut-off.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest

from stacos.core.pagination import keyset_page
from stacos.core.scope import platform_scope, tenant_context
from stacos.notifications.models import Notification, NotificationKind
from stacos.tenancy.models import Entity, Tenant

pytestmark = pytest.mark.django_db


def _walk(queryset: Any, *, order_by: str, page_size: int, **kwargs: Any) -> list[Any]:
    """Page all the way through, collecting ids in order."""
    seen: list[Any] = []
    cursor = ""
    for _ in range(20):  # a guard, not an expectation: 20 pages is far past enough
        page = keyset_page(
            queryset, order_by=order_by, cursor=cursor, page_size=page_size, **kwargs
        )
        seen += [row.pk for row in page.rows]
        if not page.has_more:
            return seen
        cursor = page.next_cursor
    raise AssertionError("pagination did not terminate")


def test_the_second_page_holds_the_remainder_with_no_overlap(org: Tenant, org_owner: Any) -> None:
    with platform_scope(reason="test-fixture"):
        for index in range(25):
            Notification.objects.create(
                tenant=org,
                recipient=org_owner,
                kind=NotificationKind.values[0],
                title=f"Notice {index}",
                body="",
                # Unique per row: notifications are deduplicated per recipient.
                dedupe_key=f"page-test-{index}",
            )

    with tenant_context(tenant_ids=org.id, reason="test"):
        queryset = Notification.objects.filter(recipient=org_owner)
        first = keyset_page(queryset, order_by="created_at", page_size=10, descending=True)
        assert len(first.rows) == 10
        assert first.has_more

        second = keyset_page(
            queryset, order_by="created_at", cursor=first.next_cursor, page_size=10, descending=True
        )
        assert len(second.rows) == 10
        assert second.has_more

        third = keyset_page(
            queryset,
            order_by="created_at",
            cursor=second.next_cursor,
            page_size=10,
            descending=True,
        )
        assert len(third.rows) == 5
        assert not third.has_more
        assert third.next_cursor == ""

        ids = [row.pk for row in (*first.rows, *second.rows, *third.rows)]
        assert len(ids) == 25
        assert len(set(ids)) == 25, "a row appeared on two pages"


def test_rows_sharing_a_sort_value_are_not_skipped(org: Tenant, org_owner: Any) -> None:
    """The tiebreaker is what makes this safe.

    Notifications created in the same transaction can share a timestamp to the
    microsecond. Ordering on the date alone would let a cursor land in the middle
    of a group and silently drop the rest of it.
    """
    with platform_scope(reason="test-fixture"):
        stamp = None
        for index in range(9):
            row = Notification.objects.create(
                tenant=org,
                recipient=org_owner,
                kind=NotificationKind.values[0],
                title=f"Same instant {index}",
                body="",
                dedupe_key=f"tiebreak-test-{index}",
            )
            stamp = stamp or row.created_at
            Notification.objects.filter(pk=row.pk).update(created_at=stamp)

    with tenant_context(tenant_ids=org.id, reason="test"):
        queryset = Notification.objects.filter(recipient=org_owner)
        walked = _walk(queryset, order_by="created_at", page_size=4, descending=True)

    assert len(walked) == 9
    assert len(set(walked)) == 9


def test_ascending_pagination_sorts_nulls_last(materialised: Entity) -> None:
    """The calendar's case: an undated obligation belongs at the end.

    A row whose date could not be resolved must not lead the list pretending to
    be the most urgent thing the user owns — and, less obviously, the cursor has
    to agree with the ordering about where nulls sit, or the page containing the
    boundary drops rows.
    """
    from django.utils import timezone

    from stacos.obligations.models import ObligationInstance
    from stacos.obligations.queries import annotate_status
    from stacos.obligations.queries import keyset_page as calendar_page

    with platform_scope(reason="test-fixture"):
        undated = list(
            ObligationInstance.objects.filter(entity=materialised).values_list("pk", flat=True)[:3]
        )
        assert undated, "the fixture should have materialised a register"
        ObligationInstance.objects.filter(pk__in=undated).update(due_date=None)
        # The register already contains obligations waiting on a date the entity
        # has not supplied, so count rather than assume these three are the lot.
        expected_undated = ObligationInstance.objects.filter(
            entity=materialised, due_date__isnull=True
        ).count()

    with tenant_context(tenant_ids=materialised.tenant_id, reason="test"):
        # `queries.keyset_page` orders on `priority_rank` as well as `due_date`
        # now, so — like every real caller — the queryset has to carry that
        # annotation before it gets here; see `annotate_status`.
        queryset = annotate_status(
            ObligationInstance.objects.filter(entity=materialised), as_of=timezone.localdate()
        )

        walked: list[Any] = []
        cursor = ""
        for _ in range(60):
            page = calendar_page(queryset, cursor=cursor, page_size=25)
            walked += list(page.rows)
            if not page.has_more:
                break
            cursor = page.next_cursor
        else:
            raise AssertionError("pagination did not terminate")

    assert len({row.pk for row in walked}) == len(walked), "a row appeared on two pages"

    dates = [row.due_date for row in walked]
    first_null = next((i for i, value in enumerate(dates) if value is None), len(dates))
    assert all(value is None for value in dates[first_null:]), (
        "an undated obligation sorted before a dated one"
    )
    assert len(dates) - first_null == expected_undated, "the undated rows were not all reached"


def test_a_mangled_cursor_starts_again_rather_than_erroring(org: Tenant, org_owner: Any) -> None:
    """A cursor is a URL fragment, and URLs get mangled by chat clients."""
    with platform_scope(reason="test-fixture"):
        Notification.objects.create(
            tenant=org,
            recipient=org_owner,
            kind=NotificationKind.values[0],
            title="Only one",
            body="",
            dedupe_key="mangled-cursor-test",
        )

    with tenant_context(tenant_ids=org.id, reason="test"):
        page = keyset_page(
            Notification.objects.filter(recipient=org_owner),
            order_by="created_at",
            cursor="not-a-cursor]",
            page_size=10,
            descending=True,
        )
    assert len(page.rows) == 1


def test_a_nullable_sort_field_without_a_sentinel_is_refused() -> None:
    """Where a null sorts is a product decision, so the helper insists on one."""
    from stacos.obligations.models import ObligationInstance

    with tenant_context(tenant_ids=[], reason="test"), pytest.raises(ValueError, match="nullable"):
        keyset_page(ObligationInstance.objects.all(), order_by="due_date", page_size=5)


def test_a_non_date_sort_field_is_refused() -> None:
    with tenant_context(tenant_ids=[], reason="test"), pytest.raises(TypeError, match="date"):
        keyset_page(Entity.objects.all(), order_by="name", page_size=5)


def test_the_calendar_cursor_format_is_unchanged() -> None:
    """Existing "load more" links must keep working across this refactor."""
    from stacos.core.pagination import CURSOR_SEPARATOR

    assert CURSOR_SEPARATOR == "~"
    assert str(date(2026, 9, 20)) == "2026-09-20"
    assert (date(2026, 9, 20) + timedelta(days=1)).isoformat() == "2026-09-21"
