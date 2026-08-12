"""
The calendar over HTTP: both render paths, the query budget, and the guards.

The query-count assertions are the point of this file as much as the status
codes. N+1 is why server-rendered applications feel slow, and HTMX makes it more
visible rather than less — a fragment that fires two hundred queries is a
fragment the user waits for on every keystroke.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.engine.lifecycle import State
from stacos.obligations.models import EntityEvent, ObligationInstance
from stacos.tenancy.models import Entity, Tenant

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 8, 12)


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    from stacos.accounts.middleware import SESSION_VERIFIED_KEY

    client.force_login(org_owner)
    session = client.session
    session[SESSION_VERIFIED_KEY] = True
    session.save()
    return client


# ===========================================================================
# Both render paths
# ===========================================================================


def test_calendar_renders_as_a_page_and_as_a_fragment(
    signed_in: Client, materialised: Entity
) -> None:
    """A direct GET returns the shell; an HTMX request returns only the body.

    Because the full page still exists, deep links, the back button and "open in
    a new tab" work with no special handling — the thing most HTMX applications
    get wrong.
    """
    page = signed_in.get(reverse("compliance:calendar"))
    fragment = signed_in.get(reverse("compliance:calendar"), headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert fragment.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b"app-sidebar" in page.content
    assert b"app-sidebar" not in fragment.content
    assert b"Compliance calendar" in fragment.content


def test_month_view_renders_both_ways(signed_in: Client, materialised: Entity) -> None:
    page = signed_in.get(reverse("compliance:month"))
    fragment = signed_in.get(reverse("compliance:month"), headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert fragment.status_code == 200
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b"month-grid" in fragment.content


def test_detail_renders_both_ways(signed_in: Client, an_obligation: ObligationInstance) -> None:
    url = reverse("compliance:detail", args=[an_obligation.pk])
    page = signed_in.get(url)
    fragment = signed_in.get(url, headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert fragment.status_code == 200
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b"Why this applies" in fragment.content


def test_swapped_regions_announce_themselves(signed_in: Client, materialised: Entity) -> None:
    """``aria-live`` on every HTMX swap target.

    A screen reader does not notice a partial update otherwise, so a user who
    filters the calendar hears nothing at all.
    """
    fragment = signed_in.get(reverse("compliance:calendar"), headers={"HX-Request": "true"})
    body = fragment.content.decode()

    assert 'id="obligation-rows"' in body
    rows_block = body.split('id="obligation-rows"')[1][:200]
    assert "aria-live" in rows_block
    assert 'id="calendar-counts"' in body


# ===========================================================================
# Query budget
# ===========================================================================


def test_the_calendar_list_has_a_bounded_query_count(
    signed_in: Client, materialised: Entity, django_assert_max_num_queries: object
) -> None:
    """Fifty rows must not cost fifty extra queries.

    ``select_related`` on entity and assignee is what keeps this flat. An upper
    bound rather than an exact count: adding a legitimate query should not fail
    the build, but an N+1 turns ten into sixty and blows straight through it.
    """
    with platform_scope(reason="test"):
        assert ObligationInstance.objects.filter(entity=materialised).count() > 50

    # Warm the session and scope-resolution caches so the assertion measures the
    # view rather than the sign-in.
    signed_in.get(reverse("compliance:calendar"))

    with django_assert_max_num_queries(14):  # type: ignore[operator]
        response = signed_in.get(reverse("compliance:calendar"))
    assert response.status_code == 200


def test_the_month_view_has_a_bounded_query_count(
    signed_in: Client, materialised: Entity, django_assert_max_num_queries: object
) -> None:
    """One query for the window, grouped in Python — not one per cell.

    Thirty-one cells rendered from one query. A per-cell query would be
    thirty-one round trips to draw one screen.
    """
    signed_in.get(reverse("compliance:month"))

    with django_assert_max_num_queries(13):  # type: ignore[operator]
        response = signed_in.get(reverse("compliance:month"))
    assert response.status_code == 200


# ===========================================================================
# Filters and pagination
# ===========================================================================


def test_filters_narrow_the_list(signed_in: Client, materialised: Entity) -> None:
    """A category filter returns that category and nothing else.

    Asserted on the rows returned rather than on response length: with a page
    size of fifty, two different filters can easily produce pages of near
    identical size while containing completely different rows.
    """
    response = signed_in.get(
        reverse("compliance:calendar"),
        {"category": "CORPORATE_SECRETARIAL", "status": "all"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    shown = _row_ids(response.content.decode())
    assert shown, "the category filter returned nothing at all"

    with platform_scope(reason="test"):
        categories = set(
            ObligationInstance.objects.filter(pk__in=shown).values_list("category", flat=True)
        )
    assert categories == {"CORPORATE_SECRETARIAL"}, f"filter leaked other categories: {categories}"


def _row_ids(body: str) -> list[str]:
    """Primary keys of the obligation rows a response actually rendered."""
    import re

    return re.findall(r'id="obligation-([0-9a-f-]{36})"', body)


def test_an_unknown_cursor_starts_the_list_again(signed_in: Client, materialised: Entity) -> None:
    """A mangled URL is a broken link, not an attack. It must not 500."""
    response = signed_in.get(reverse("compliance:calendar"), {"cursor": "not-a-cursor"})
    assert response.status_code == 200


def test_a_cursor_request_returns_only_rows(signed_in: Client, materialised: Entity) -> None:
    first = signed_in.get(reverse("compliance:calendar"), headers={"HX-Request": "true"})
    body = first.content.decode()
    assert "calendar-load-more" in body, "a full calendar should paginate"

    cursor = body.split("?cursor=")[1].split("&")[0].split('"')[0]
    more = signed_in.get(
        reverse("compliance:calendar"), {"cursor": cursor}, headers={"HX-Request": "true"}
    )
    assert more.status_code == 200
    assert b"data-table__toolbar" not in more.content


# ===========================================================================
# Actions
# ===========================================================================


def test_a_transition_updates_the_panel_and_the_counters(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """One request, several regions, no refetch."""
    response = signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.IN_PREPARATION},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert b"obligation-panel" in response.content
    assert b"calendar-counts" in response.content
    assert "HX-Trigger" in response.headers

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
    assert an_obligation.state == State.IN_PREPARATION


def test_a_transition_missing_its_guard_re_renders_with_the_reason(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """The panel stays put and says what is missing, rather than 500ing."""
    response = signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.DEFERRED},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    assert b"Please say why" in response.content

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
    assert an_obligation.state == State.NOT_STARTED


def test_recording_an_event_schedules_and_rebuilds(signed_in: Client, materialised: Entity) -> None:
    """Answering "tell us your AGM date" produces a date immediately."""
    with platform_scope(reason="test"):
        blocked = ObligationInstance.objects.filter(
            entity=materialised, definition_code="IN-MCA-AOC4"
        ).first()
    assert blocked is not None
    assert blocked.due_date is None

    response = signed_in.post(
        reverse("compliance:record_event", args=[blocked.pk]),
        {"key": "AGM_DATE", "occurred_on": "2026-09-25", "note": "Held at the registered office."},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    with platform_scope(reason="test"):
        blocked.refresh_from_db()
        assert EntityEvent.objects.filter(entity=materialised, key="AGM_DATE").exists()
    assert blocked.due_date == date(2026, 10, 25)


def test_rebuilding_the_calendar_is_safe_to_repeat(signed_in: Client, materialised: Entity) -> None:
    url = reverse("compliance:rebuild", args=[materialised.pk])

    with platform_scope(reason="test"):
        before = ObligationInstance.objects.filter(entity=materialised).count()

    first = signed_in.post(url, headers={"HX-Request": "true"})
    second = signed_in.post(url, headers={"HX-Request": "true"})

    assert first.status_code == 200
    assert second.status_code == 200

    # The outcome is announced through HX-Trigger rather than swapped into the
    # DOM, so a toast never fights the main swap for the same region.
    assert "no changes" in second.headers["HX-Trigger"]

    with platform_scope(reason="test"):
        assert ObligationInstance.objects.filter(entity=materialised).count() == before


def test_the_definition_page_explains_the_law(signed_in: Client, materialised: Entity) -> None:
    response = signed_in.get(
        reverse("compliance:definition", args=["IN-GST-GSTR3B-MONTHLY"]),
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert b"Section 39" in response.content


def test_an_unknown_definition_is_a_404(signed_in: Client, materialised: Entity) -> None:
    assert (
        signed_in.get(reverse("compliance:definition", args=["IN-NOT-A-THING"])).status_code == 404
    )
