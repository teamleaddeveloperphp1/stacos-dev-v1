"""
The calendar over HTTP: both render paths, the query budget, and the guards.

The query-count assertions are the point of this file as much as the status
codes. N+1 is why server-rendered applications feel slow, and HTMX makes it more
visible rather than less — a fragment that fires two hundred queries is a
fragment the user waits for on every keystroke.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.engine.lifecycle import State
from stacos.obligations.models import (
    EntityEvent,
    ObligationEvent,
    ObligationInstance,
    ObligationStep,
)
from stacos.obligations.transitions import (
    TransitionError,
    ensure_steps,
    record_completion,
    record_pending,
)
from stacos.tenancy.models import Entity, Tenant

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 8, 12)


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
    (The entity filter's own dropdown options are one more query, and the
    "Total" figure above the table is a second — a single ``COUNT(*)`` on the
    first screen of a filter combination, never repeated on "load more" — 16
    rather than 14. The workload forecast adds two aggregates
    (``weekly_workload``, ``overdue_aging``) and the overdue penalty exposure
    adds two more — one for the overdue rows' own dates, one batched lookup of
    every distinct definition version's penalty rules — hence 20. All four are
    skipped on "load more" the same way ``total`` is, see ``calendar_list``.)
    """
    with platform_scope(reason="test"):
        assert ObligationInstance.objects.filter(entity=materialised).count() > 50

    # Warm the session and scope-resolution caches so the assertion measures the
    # view rather than the sign-in.
    signed_in.get(reverse("compliance:calendar"))

    with django_assert_max_num_queries(20):  # type: ignore[operator]
        response = signed_in.get(reverse("compliance:calendar"))
    assert response.status_code == 200


def test_the_detail_view_has_a_bounded_query_count(
    signed_in: Client, an_obligation: ObligationInstance, django_assert_max_num_queries: object
) -> None:
    """The strip, the question card, the discussion split and the registrations
    card must not turn one obligation into a query per widget."""
    url = reverse("compliance:detail", args=[an_obligation.pk])
    signed_in.get(url)

    with django_assert_max_num_queries(16):  # type: ignore[operator]
        response = signed_in.get(url)
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
    # TAX_INDIRECT (GST), not CORPORATE_SECRETARIAL: the catalog loaded by the
    # `materialised` fixture only carries GST/income-tax/TDS definitions —
    # MCA and other categories were dropped from this fork's catalog and no
    # longer materialise anything to filter on.
    response = signed_in.get(
        reverse("compliance:calendar"),
        {"category": "TAX_INDIRECT", "status": "all"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    shown = _row_ids(response.content.decode())
    assert shown, "the category filter returned nothing at all"

    with platform_scope(reason="test"):
        categories = set(
            ObligationInstance.objects.filter(pk__in=shown).values_list("category", flat=True)
        )
    assert categories == {"TAX_INDIRECT"}, f"filter leaked other categories: {categories}"


def test_pending_and_completed_filters_agree_with_status_counts(
    signed_in: Client, materialised: Entity
) -> None:
    """The dashboard's "Pending" and "Completed" tiles link straight into
    these two filters — the count promised on the tile must equal the rows
    the filter actually returns, or the click lands on a list that disagrees
    with the number that sent the user there.
    """
    from django.utils import timezone

    from stacos.obligations.queries import status_counts

    with platform_scope(reason="test"):
        expected = status_counts(as_of=timezone.localdate())
    expected_pending = expected["open"] - expected["overdue"] - expected["due_soon"]

    for status, expected_count, disallowed_states in (
        ("pending", expected_pending, {"FILED", "CLOSED", "NOT_APPLICABLE"}),
        ("completed", expected["completed"], set(State) - {"FILED", "CLOSED", "NOT_APPLICABLE"}),
    ):
        shown = _collect_all_rows(signed_in, status)
        assert len(shown) == expected_count, f"status={status!r}"

        with platform_scope(reason="test"):
            states = set(
                ObligationInstance.objects.filter(pk__in=shown).values_list("state", flat=True)
            )
        assert states.isdisjoint(disallowed_states), f"status={status!r} leaked {states}"


def _collect_all_rows(client: Client, status: str) -> list[str]:
    """Every row id behind a status filter, walking the keyset cursor."""
    return _collect_all_rows_with(client, {"status": status})


def _collect_all_rows_with(client: Client, base_params: dict[str, str]) -> list[str]:
    """Every row id behind arbitrary querystring params, walking the keyset cursor.

    A page is 50 rows and the fixture materialises well over that, so reading
    only the first page would silently under-count a broad bucket like
    "pending".
    """
    ids: list[str] = []
    cursor = ""
    while True:
        params = dict(base_params)
        if cursor:
            params["cursor"] = cursor
        response = client.get(
            reverse("compliance:calendar"), params, headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        ids.extend(_row_ids(response.content.decode()))
        page = response.context["page"]
        if not page.has_more:
            return ids
        cursor = page.next_cursor


def test_due_30_filter_matches_status_counts_and_stays_in_window(
    signed_in: Client, materialised: Entity
) -> None:
    """The "Due in 30 days" tile promises a count; the filter behind it must
    return exactly that many rows, every one open and due inside the window —
    not the narrower 7-day "due soon" window it sits next to on the toolbar.
    """
    from stacos.obligations.queries import status_counts

    as_of = timezone.localdate()
    with platform_scope(reason="test"):
        expected = status_counts(as_of=as_of)["due_30"]

    shown = _collect_all_rows(signed_in, "due_30")
    assert len(shown) == expected

    with platform_scope(reason="test"):
        rows = ObligationInstance.objects.filter(pk__in=shown)
        states = set(rows.values_list("state", flat=True))
        assert states.isdisjoint({"FILED", "CLOSED", "NOT_APPLICABLE"}), states
        assert all(as_of <= row.due_date <= as_of + timedelta(days=30) for row in rows)


def test_due_90_filter_matches_status_counts_and_stays_in_window(
    signed_in: Client, materialised: Entity
) -> None:
    """Same contract as the 30-day filter, one window wider."""
    from stacos.obligations.queries import status_counts

    as_of = timezone.localdate()
    with platform_scope(reason="test"):
        expected = status_counts(as_of=as_of)["due_90"]

    shown = _collect_all_rows(signed_in, "due_90")
    assert len(shown) == expected

    with platform_scope(reason="test"):
        rows = ObligationInstance.objects.filter(pk__in=shown)
        states = set(rows.values_list("state", flat=True))
        assert states.isdisjoint({"FILED", "CLOSED", "NOT_APPLICABLE"}), states
        assert all(as_of <= row.due_date <= as_of + timedelta(days=90) for row in rows)


def test_no_status_param_defaults_to_latest(signed_in: Client, materialised: Entity) -> None:
    """A first visit to the calendar — no ``status`` in the querystring at all —
    must open on "Due now" (overdue, of any age, plus the next 90 days), not
    "Everything open".

    An explicit ``status=`` (the user picking "Everything open" from the
    dropdown) is a different request and must still fall through to the old
    behaviour, so the two are asserted against each other rather than only
    against ``status_counts``.
    """
    from stacos.obligations.queries import status_counts

    as_of = timezone.localdate()
    with platform_scope(reason="test"):
        expected_latest = status_counts(as_of=as_of)["latest"]
        expected_open = status_counts(as_of=as_of)["open"]
    assert expected_open > expected_latest, "fixture must have open rows outside the 90-day window"

    first_page = signed_in.get(reverse("compliance:calendar"), headers={"HX-Request": "true"})
    assert first_page.status_code == 200
    assert first_page.context["status"] == "latest"
    assert b'<option value="latest" selected>' in first_page.content

    shown = _collect_all_rows_with(signed_in, {})
    assert len(shown) == expected_latest

    with platform_scope(reason="test"):
        rows = ObligationInstance.objects.filter(pk__in=shown)
        assert all(
            row.due_date is not None and row.due_date <= as_of + timedelta(days=90) for row in rows
        )

    everything_open = signed_in.get(
        reverse("compliance:calendar"), {"status": ""}, headers={"HX-Request": "true"}
    )
    assert everything_open.status_code == 200
    assert everything_open.context["status"] == ""
    assert len(_collect_all_rows_with(signed_in, {"status": ""})) == expected_open


def test_latest_status_filter_includes_the_backlog(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """ "Due now" is the calendar's default landing view, and a window that
    silently drops the backlog is worse than no window at all: it must show
    overdue rows regardless of age, unlike the explicit "Due in 90 days"
    filter, which stays upcoming-only.
    """
    as_of = timezone.localdate()
    with platform_scope(reason="test"):
        an_obligation.due_date = as_of - timedelta(days=400)
        an_obligation.save(update_fields=["due_date"])

    shown_default = _collect_all_rows_with(signed_in, {})
    assert str(an_obligation.pk) in shown_default

    shown_due_90 = _collect_all_rows(signed_in, "due_90")
    assert str(an_obligation.pk) not in shown_due_90, "due_90 must stay upcoming-only"


def test_the_defaulted_filter_stays_visibly_marked_without_focus(
    signed_in: Client, materialised: Entity
) -> None:
    """The default is silent otherwise: nothing in the URL says a filter is
    doing the narrowing, so a short list on arrival reads as "nothing is
    due" rather than "due in 30 days" — see `form-select--filtered` in
    `assets/scss/components/_forms.scss`. "Everything open" — a filter that
    narrows nothing — must not carry the same marker.
    """
    defaulted = signed_in.get(reverse("compliance:calendar"), headers={"HX-Request": "true"})
    assert b"form-select--filtered" in defaulted.content

    everything_open = signed_in.get(
        reverse("compliance:calendar"), {"status": ""}, headers={"HX-Request": "true"}
    )
    assert b"form-select--filtered" not in everything_open.content


def test_the_entity_filter_narrows_the_list(
    signed_in: Client, materialised: Entity, org: Tenant
) -> None:
    """Picking one entity from the toolbar dropdown must not leak another
    entity's rows — the same ``?entity=`` param the onboarding "finish" step
    already links to, now exposed as a filter a user can actually reach.
    """
    from stacos.obligations.services import materialise
    from stacos.tenancy.models import EntityRegistration

    with platform_scope(reason="test"):
        other = Entity.objects.create(
            tenant=org,
            name="Second Co",
            entity_type="PVT_LTD",
            country="IN",
            registered_office_state="IN-KA",
        )
        EntityRegistration.objects.create(tenant=org, entity=other, type="PAN", value="AAACS1234C")
        materialise(other, as_of=AS_OF, trigger="ONBOARDING")

    response = signed_in.get(
        reverse("compliance:calendar"),
        {"entity": str(materialised.id), "status": "all"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    shown = _row_ids(response.content.decode())
    assert shown, "the entity filter returned nothing at all"

    with platform_scope(reason="test"):
        entities_shown = set(
            ObligationInstance.objects.filter(pk__in=shown).values_list("entity_id", flat=True)
        )
    assert entities_shown == {materialised.id}


def test_the_entity_dropdown_lists_every_entity(signed_in: Client, materialised: Entity) -> None:
    page = signed_in.get(reverse("compliance:calendar"))
    assert page.status_code == 200
    assert b'aria-label="Filter by entity"' in page.content
    assert materialised.name.encode() in page.content


def test_a_bad_entity_id_is_ignored_rather_than_erroring(
    signed_in: Client, materialised: Entity
) -> None:
    """Filtering a UUID field with a non-UUID string raises ``ValidationError``
    — a stale bookmark or a hand-edited URL must fall back to "every entity"
    instead of a 500, the same fix already made on the dashboard.
    """
    response = signed_in.get(reverse("compliance:calendar"), {"entity": "not-a-uuid-at-all"})
    assert response.status_code == 200


def _row_ids(body: str) -> list[str]:
    """Primary keys of the obligation rows a response actually rendered."""
    import re

    return re.findall(r'id="obligation-([0-9a-f-]{36})"', body)


def test_an_unknown_cursor_starts_the_list_again(signed_in: Client, materialised: Entity) -> None:
    """A mangled URL is a broken link, not an attack. It must not 500."""
    response = signed_in.get(reverse("compliance:calendar"), {"cursor": "not-a-cursor"})
    assert response.status_code == 200


def test_a_cursor_request_returns_only_rows(signed_in: Client, materialised: Entity) -> None:
    # "all" rather than the default "due in 30 days" — pagination mechanics are
    # under test here, and the default's narrower window has fewer rows than a
    # page, which would exercise nothing.
    first = signed_in.get(
        reverse("compliance:calendar"), {"status": "all"}, headers={"HX-Request": "true"}
    )
    body = first.content.decode()
    assert "calendar-load-more" in body, "a full calendar should paginate"

    cursor = body.split("?cursor=")[1].split("&")[0].split('"')[0]
    more = signed_in.get(
        reverse("compliance:calendar"),
        {"status": "all", "cursor": cursor},
        headers={"HX-Request": "true"},
    )
    assert more.status_code == 200
    assert b"data-table__toolbar" not in more.content


# ===========================================================================
# Actions
# ===========================================================================


def test_a_transition_updates_the_panel(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """The panel comes back re-rendered, with no dangling swap for a region
    this page never has.

    This form only ever posts from the standalone obligation page, which has
    no `#calendar-counts` in its DOM — that strip belongs to the calendar list.
    An OOB fragment aimed at it used to ride along anyway, costing a query and
    logging `htmx:oobErrorNoTarget` on every transition for a swap that could
    never land; the bulk actions on the calendar itself still carry it, because
    there it does.
    """
    response = signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.IN_PREPARATION},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert b"obligation-panel" in response.content
    assert b"calendar-counts" not in response.content
    # `obligation-audit-trail` rides along OOB on every transition (a new row
    # in the audit trail is not optional), which is exactly why this uses
    # `HX-Trigger-After-Swap` rather than the immediate header — see `oob()`'s
    # own docstring on why an OOB swap changes which one is safe.
    assert b"obligation-audit-trail" in response.content
    assert "HX-Trigger-After-Swap" in response.headers

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


def test_assign_modal_lists_only_this_tenants_active_members(
    signed_in: Client, an_obligation: ObligationInstance, org: Tenant, rival_owner: User
) -> None:
    """The picker is narrowed to the caller's own tenant.

    ``rival_owner`` belongs to a completely different organisation — the fixture
    built for exactly this question elsewhere in the suite — and must not appear
    in a list an owner uses to hand out their own team's work.
    """
    from tests.conftest import _make_member

    colleague = _make_member(
        org, "ramesh@acme.example", "Ramesh Patel", "+919800000099", "org-owner"
    )

    response = signed_in.get(
        reverse("compliance:assign", args=[an_obligation.pk]), headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert colleague.full_name in body
    assert rival_owner.full_name not in body


def test_assigning_hands_the_obligation_to_a_colleague(
    signed_in: Client, an_obligation: ObligationInstance, org: Tenant
) -> None:
    """One request updates the row, the detail panel, and the record itself."""
    from tests.conftest import _make_member

    colleague = _make_member(
        org, "ramesh@acme.example", "Ramesh Patel", "+919800000099", "org-owner"
    )

    response = signed_in.post(
        reverse("compliance:assign", args=[an_obligation.pk]),
        {"assigned_to": colleague.pk},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert f"obligation-{an_obligation.pk}".encode() in response.content
    assert b"obligation-panel" in response.content
    assert "HX-Trigger-After-Swap" in response.headers
    assert "stacos:modal-close" in response.headers["HX-Trigger-After-Swap"]

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.assigned_to_id == colleague.id
        assert an_obligation.events.filter(kind="ASSIGNED").exists()


def test_assigning_to_someone_outside_the_tenant_is_rejected(
    signed_in: Client, an_obligation: ObligationInstance, rival_owner: User
) -> None:
    """A forged assignee id is re-checked against the caller's own scope.

    ``rival_owner`` never resolves in ``org_owner``'s picker, so the field's own
    ``clean()`` — not a bespoke check in the view — is what stops this.
    """
    response = signed_in.post(
        reverse("compliance:assign", args=[an_obligation.pk]),
        {"assigned_to": rival_owner.pk},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
    assert an_obligation.assigned_to_id is None


def test_clearing_an_assignment(
    signed_in: Client, an_obligation: ObligationInstance, org_owner: User
) -> None:
    """Assigning nobody is a real choice, not an error."""
    from stacos.obligations.transitions import assign as assign_transition

    with platform_scope(reason="test"):
        assign_transition(an_obligation, assignee=org_owner, actor=org_owner)

    response = signed_in.post(
        reverse("compliance:assign", args=[an_obligation.pk]),
        {"assigned_to": ""},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
    assert an_obligation.assigned_to_id is None


def test_posting_a_comment_adds_it_to_the_discussion(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """A remark writes a `Kind.NOTE` event and appears in the re-rendered feed."""
    response = signed_in.post(
        reverse("compliance:comment", args=[an_obligation.pk]),
        {"note": "Waiting on the client for this one."},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert b"Waiting on the client for this one." in response.content

    with platform_scope(reason="test"):
        event = an_obligation.events.get(kind=ObligationEvent.Kind.NOTE)
    assert event.note == "Waiting on the client for this one."
    assert event.actor_label


def test_an_empty_comment_is_rejected(signed_in: Client, an_obligation: ObligationInstance) -> None:
    response = signed_in.post(
        reverse("compliance:comment", args=[an_obligation.pk]),
        {"note": ""},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    with platform_scope(reason="test"):
        assert not an_obligation.events.filter(kind=ObligationEvent.Kind.NOTE).exists()


def test_nudging_notifies_the_assignee_and_logs_it(
    signed_in: Client, an_obligation: ObligationInstance, org: Tenant
) -> None:
    from tests.conftest import _make_member

    colleague = _make_member(
        org, "ramesh@acme.example", "Ramesh Patel", "+919800000099", "org-owner"
    )
    with platform_scope(reason="test"):
        an_obligation.assigned_to = colleague
        an_obligation.save(update_fields=["assigned_to"])

    response = signed_in.post(
        reverse("compliance:nudge", args=[an_obligation.pk]), headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    assert "HX-Trigger-After-Swap" in response.headers
    assert "nudged" in response.headers["HX-Trigger-After-Swap"].lower()

    with platform_scope(reason="test"):
        from stacos.notifications.models import Notification

        assert Notification.objects.filter(recipient=colleague, tenant=org).exists()
        assert an_obligation.events.filter(
            kind=ObligationEvent.Kind.NOTE, note__icontains="Nudged"
        ).exists()


def test_nudging_an_unassigned_obligation_is_a_no_op(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    response = signed_in.post(
        reverse("compliance:nudge", args=[an_obligation.pk]), headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    assert "Nobody is assigned" in response.headers["HX-Trigger"]
    with platform_scope(reason="test"):
        assert not an_obligation.events.filter(kind=ObligationEvent.Kind.NOTE).exists()


# ===========================================================================
# "Is this filing completed?" — the two answers the detail page asks for
# ===========================================================================


def _status(obligation: ObligationInstance) -> str:
    return reverse("compliance:status", args=[obligation.pk])


def test_answering_yes_records_the_date_the_number_and_the_document(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """One post from ``NOT_STARTED`` to a filed, evidenced row.

    The starting state matters. The old table could only reach ``FILED`` from
    ``READY_TO_FILE``, which meant answering "yes" on an untouched obligation —
    the overwhelmingly common case, because the work happens on a government
    portal and not in this product — was impossible without first clicking
    through three stages of work nobody did.
    """
    with platform_scope(reason="test"):
        assert an_obligation.state == State.NOT_STARTED

    response = signed_in.post(
        _status(an_obligation),
        {
            "answer": "yes",
            "filed_on": "2026-08-10",
            "filing_reference": "AA240810123456X",
            "acknowledgement": SimpleUploadedFile(
                "ack.pdf", b"%PDF-1.4 acknowledgement", content_type="application/pdf"
            ),
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.state == State.FILED
        assert an_obligation.filed_on == date(2026, 8, 10)
        assert an_obligation.filing_reference == "AA240810123456X"
        assert an_obligation.acknowledgement_name == "ack.pdf"
        # Stored under a name of our own making, not the browser's.
        assert "ack.pdf" not in an_obligation.acknowledgement.name
        assert an_obligation.acknowledgement.name.startswith("acknowledgements/")


def test_the_acknowledgement_is_optional_but_the_number_is_not(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """A filing done last week and recorded from memory is still worth having.

    The acknowledgement *number* is not negotiable — it is the row an assessment
    is defended with — but demanding the PDF alongside it would push people to
    record nothing at all, which is the failure this product exists to prevent.
    """
    no_number = signed_in.post(
        _status(an_obligation),
        {"answer": "yes", "filed_on": "2026-08-10", "filing_reference": ""},
        headers={"HX-Request": "true"},
    )
    assert no_number.status_code == 422
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.state == State.NOT_STARTED

    no_document = signed_in.post(
        _status(an_obligation),
        {"answer": "yes", "filed_on": "2026-08-10", "filing_reference": "AA240810123456X"},
        headers={"HX-Request": "true"},
    )
    assert no_document.status_code == 200
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.state == State.FILED
        assert not an_obligation.acknowledgement


def test_a_filing_cannot_have_happened_tomorrow(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """A future date sails straight past ``filed_late``, which compares two dates
    and asks no questions about either — and quietly reports a late filing as on
    time."""
    response = signed_in.post(
        _status(an_obligation),
        {
            "answer": "yes",
            "filed_on": (timezone.localdate() + timedelta(days=1)).isoformat(),
            "filing_reference": "AA240810123456X",
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert b"in the future" in response.content
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.filed_on is None


def test_only_a_pdf_or_a_photo_is_accepted_as_an_acknowledgement(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    response = signed_in.post(
        _status(an_obligation),
        {
            "answer": "yes",
            "filed_on": "2026-08-10",
            "filing_reference": "AA240810123456X",
            "acknowledgement": SimpleUploadedFile(
                "payload.svg", b"<svg onload=alert(1)>", content_type="image/svg+xml"
            ),
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.state == State.NOT_STARTED


def test_answering_no_records_the_reason_and_leaves_the_state_alone(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """The load-bearing assertion in this file.

    An obligation somebody has explained is still owed, still dated and still
    goes overdue on schedule — the day an explanation starts counting as
    progress is the day the register stops being worth reading. ``state``, the
    due date and ``filed_on`` are asserted unchanged for exactly that reason.

    ``display_status`` is a different matter, and does change: from
    ``not-started`` (nothing said yet) to ``pending`` (something has been
    said). That word change is not progress either — overdue and due-soon
    both still override it, see
    ``stacos.engine.lifecycle.derive_display_status`` — it is only the
    difference between an obligation nobody has looked at and one somebody
    has already given an account of. Pushed into the future first so this is
    unambiguously not overdue or due soon, which would otherwise legitimately
    keep outranking ``pending``.
    """
    with platform_scope(reason="test"):
        an_obligation.due_date = date.today() + timedelta(days=180)
        an_obligation.save(update_fields=["due_date"])
        due_before = an_obligation.due_date

    response = signed_in.post(
        _status(an_obligation),
        {
            "answer": "no",
            "pending_reason": "Client has not sent the purchase register.",
            "expected_completion_date": "2026-09-30",
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.state == State.NOT_STARTED
        assert an_obligation.due_date == due_before
        assert an_obligation.filed_on is None
        assert an_obligation.pending_reason == "Client has not sent the purchase register."
        assert an_obligation.expected_completion_date == date(2026, 9, 30)
        assert an_obligation.pending_reported_at is not None
        # And it is on the timeline, where the next person to open this reads it.
        assert an_obligation.events.filter(
            kind=ObligationEvent.Kind.NOTE, note__contains="purchase register"
        ).exists()

    body = response.content.decode()
    assert "status-chip--pending" in body
    # The colour and the word must move together — a chip that turned pending
    # by class while still reading "Not started" is exactly the bug this
    # closes (the chip used to be handed `obligation.get_state_display`,
    # which reads the stored `state` and never moved). Scoped to the chip's
    # own `aria-label`, not the header at large: the header now *also* shows
    # the raw work status next to it on purpose ("Work status: Not started"),
    # precisely so that fact is not lost the moment the chip's word changes —
    # see the manual-tracking status model's own note on keeping the two
    # concepts separate rather than one variable pretending to be both.
    assert 'aria-label="Status: Pending"' in body


def test_an_untouched_obligation_shows_not_started_not_on_track(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """Nothing recorded yet is an attention state, not a quiet green one — the
    gap this closes: ``derive_display_status``'s old fallback read every
    untouched, not-yet-due obligation as ``on-track``, the same word and
    colour the rest of the product uses for "comfortably ahead".

    Pushed well into the future so this is unambiguously "not due soon"
    rather than "overdue" or "due soon" — either of which would legitimately
    take priority over ``not-started``, and the fixture's own due date is not
    guaranteed to land outside that window on every day this test runs.
    """
    with platform_scope(reason="test"):
        an_obligation.due_date = date.today() + timedelta(days=180)
        an_obligation.save(update_fields=["due_date"])
        assert an_obligation.pending_reason == ""

    response = signed_in.get(
        reverse("compliance:detail", args=[an_obligation.pk]), headers={"HX-Request": "true"}
    )

    assert response.status_code == 200
    assert "status-chip--not-started" in response.content.decode()


def test_answering_no_needs_an_actual_reason(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """ "Pending" on its own is what the register already knew."""
    response = signed_in.post(
        _status(an_obligation),
        {"answer": "no", "pending_reason": "   ", "expected_completion_date": "2026-09-30"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.pending_reason == ""


def test_answering_yes_clears_a_stale_pending_answer(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """Otherwise "waiting on the client's bank statement" sits underneath a
    completed filing for the rest of the obligation's life."""
    signed_in.post(
        _status(an_obligation),
        {
            "answer": "no",
            "pending_reason": "Waiting on the bank statement.",
            "expected_completion_date": "2026-09-30",
        },
        headers={"HX-Request": "true"},
    )
    signed_in.post(
        _status(an_obligation),
        {"answer": "yes", "filed_on": "2026-08-10", "filing_reference": "AA240810123456X"},
        headers={"HX-Request": "true"},
    )

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.state == State.FILED
        assert an_obligation.pending_reason == ""
        assert an_obligation.expected_completion_date is None


def test_neither_answer_can_be_given_without_the_permission_behind_it(
    an_obligation: ObligationInstance,
) -> None:
    """The endpoint declares only ``view``; which answer you may give is checked
    underneath it — ``file`` for "yes", ``prepare`` for "no".

    Asserted against the services rather than through a signed-in client because
    that is where the check lives, and a view test would only prove that one of
    this endpoint's two branches reaches it.
    """
    view_only = frozenset({"compliance.obligation.view"})

    with platform_scope(reason="test"):
        with pytest.raises(TransitionError):
            record_completion(
                an_obligation,
                actor=None,
                permissions=view_only,
                filed_on=date(2026, 8, 10),
                filing_reference="AA240810123456X",
                as_of=AS_OF,
            )
        with pytest.raises(TransitionError):
            record_pending(
                an_obligation,
                actor=None,
                permissions=view_only,
                reason="Client has not sent the register.",
                expected_on=date(2026, 9, 30),
            )

        an_obligation.refresh_from_db()
        assert an_obligation.state == State.NOT_STARTED
        assert an_obligation.pending_reason == ""


def test_attaching_the_document_later_applies_the_same_rules(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """The second upload path — the number was recorded from memory, the PDF
    turned up afterwards — must not be the lax one."""
    signed_in.post(
        _status(an_obligation),
        {"answer": "yes", "filed_on": "2026-08-10", "filing_reference": "AA240810123456X"},
        headers={"HX-Request": "true"},
    )
    url = reverse("compliance:acknowledgement", args=[an_obligation.pk])

    refused = signed_in.post(
        url,
        {
            "acknowledgement": SimpleUploadedFile(
                "payload.svg", b"<svg onload=alert(1)>", content_type="image/svg+xml"
            )
        },
        headers={"HX-Request": "true"},
    )
    assert refused.status_code == 422
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert not an_obligation.acknowledgement

    accepted = signed_in.post(
        url,
        {
            "acknowledgement": SimpleUploadedFile(
                "ack.pdf", b"%PDF-1.4 acknowledgement", content_type="application/pdf"
            )
        },
        headers={"HX-Request": "true"},
    )
    assert accepted.status_code == 200
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.acknowledgement_name == "ack.pdf"


def test_the_acknowledgement_downloads_only_through_a_scoped_view(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """The bytes are a client's statutory evidence, so reaching them costs a
    scope check — ``MEDIA_ROOT`` is not served at all outside development."""
    url = reverse("compliance:acknowledgement", args=[an_obligation.pk])
    assert signed_in.get(url).status_code == 404

    signed_in.post(
        _status(an_obligation),
        {
            "answer": "yes",
            "filed_on": "2026-08-10",
            "filing_reference": "AA240810123456X",
            "acknowledgement": SimpleUploadedFile(
                "ack.pdf", b"%PDF-1.4 acknowledgement", content_type="application/pdf"
            ),
        },
        headers={"HX-Request": "true"},
    )

    response = signed_in.get(url)
    assert response.status_code == 200
    assert response["X-Content-Type-Options"] == "nosniff"
    assert b"acknowledgement" in b"".join(response.streaming_content)


# ===========================================================================
# The checklist — only Form 24Q has a `workflow_steps` template in the test
# catalog, so these all key off `IN-TDS-24Q` rather than `an_obligation`.
# ===========================================================================


@pytest.fixture
def a_24q_obligation(materialised: Entity) -> ObligationInstance:
    with platform_scope(reason="test-fixture"):
        obligation = (
            ObligationInstance.objects.filter(entity=materialised, definition_code="IN-TDS-24Q")
            .order_by("due_date")
            .first()
        )
    assert obligation is not None, "fixture entity must have a materialised 24Q obligation"
    return obligation


def _confirm_step(obligation: ObligationInstance) -> ObligationStep:
    with platform_scope(reason="test-fixture"):
        return ensure_steps(obligation)[0]


def test_the_detail_page_asks_one_question_rather_than_rendering_a_step_rail(
    signed_in: Client, a_24q_obligation: ObligationInstance
) -> None:
    """24Q has the richest ``workflow_steps`` template in the test catalog, and
    none of it reaches the page any more.

    The step rows are still a model with endpoints of their own — the tests
    below exercise them — but the detail page asks "is this filing completed?"
    and nothing else. Asserting the absence matters as much as the presence: a
    step rail rendered beside the question is two places to say a filing is
    done, and they would disagree within a week.
    """
    response = signed_in.get(reverse("compliance:detail", args=[a_24q_obligation.pk]))
    assert response.status_code == 200
    assert b"Has this been submitted?" in response.content
    assert b"Confirm TDS applies this quarter" not in response.content
    assert b"obligation-checklist" not in response.content


def test_ensure_steps_is_idempotent(a_24q_obligation: ObligationInstance) -> None:
    with platform_scope(reason="test"):
        first = {step.pk for step in ensure_steps(a_24q_obligation)}
        second = {step.pk for step in ensure_steps(a_24q_obligation)}
    assert first == second
    with platform_scope(reason="test"):
        assert a_24q_obligation.steps.count() == 7


def test_the_question_offers_both_answers_and_start_compliance_but_no_maker_checker(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """Both answers, equally weighted, and a clear, named next step beside
    them — but none of the review-chain moves nobody manually walks through.

    "Start compliance" is genuinely offered here, not tucked behind a second
    click: a solo user should see it the moment they land on a fresh
    obligation, not go looking for it. "Request information", "Submit for
    review" and "Approve for filing" are not offered anywhere on this page —
    that part of the old maker-checker table stays exactly as absent as it
    already was.
    """
    response = signed_in.get(reverse("compliance:detail", args=[an_obligation.pk]))
    body = response.content.decode()

    assert response.status_code == 200
    assert "Has this been submitted?" in body
    assert "Yes, submitted" in body
    assert "Not yet" in body
    assert "Acknowledgement number" in body
    assert "Acknowledgement document" in body
    assert "Start compliance" in body

    for gone in ("Request information", "Submit for review", "Approve for filing"):
        assert gone not in body, f"{gone!r} should not be offered to a solo tracker"


def test_start_compliance_moves_to_in_progress_and_is_reflected_on_reload(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """The exact loop the manual tracker exists for: take the named action,
    see it stick — both in the response and on a fresh load of the page."""
    with platform_scope(reason="test"):
        assert an_obligation.state == State.NOT_STARTED

    response = signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.IN_PREPARATION},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "Work status: In progress" in response.content.decode()

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.state == State.IN_PREPARATION

    reloaded = signed_in.get(reverse("compliance:detail", args=[an_obligation.pk]))
    assert "Work status: In progress" in reloaded.content.decode()
    # The one correction "Start compliance" needs: undoing a mistaken click is
    # a typo, not a judgement call, so it is offered right on the page.
    assert "Move back to not started" in reloaded.content.decode()


def test_complete_modal_shows_the_submission_summary_and_evidence(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """ "Mark as completed" is a review, not a bare click: what was submitted,
    when, under what reference, and which evidence this definition asked
    for — reachable at all only once that evidence is actually on file (see
    ``test_completing_is_refused_without_the_required_evidence`` for the
    other half of that guard)."""
    signed_in.post(
        reverse("compliance:status", args=[an_obligation.pk]),
        {
            "answer": "yes",
            "filed_on": "2026-08-10",
            "filing_reference": "AA240810123456X",
            "acknowledgement": SimpleUploadedFile(
                "ack.pdf", b"%PDF-1.4", content_type="application/pdf"
            ),
        },
        headers={"HX-Request": "true"},
    )

    response = signed_in.get(
        reverse("compliance:complete", args=[an_obligation.pk]), headers={"HX-Request": "true"}
    )
    body = response.content.decode()
    assert response.status_code == 200
    assert "AA240810123456X" in body
    assert "10 Aug 2026" in body
    # GSTR-3B marks two evidence items mandatory, and the one document
    # attached above satisfies both — see `outstanding_mandatory_evidence`.
    assert "Attached" in body
    assert "Confirm completion" in body


def test_completing_is_refused_without_the_required_evidence(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """The modal cannot be reached at all once ``Close`` is not on offer —
    see ``test_closing_is_refused_without_the_required_evidence`` in
    ``test_transitions.py`` for the guard this relies on."""
    signed_in.post(
        reverse("compliance:status", args=[an_obligation.pk]),
        {"answer": "yes", "filed_on": "2026-08-10", "filing_reference": "AA240810123456X"},
        headers={"HX-Request": "true"},
    )

    response = signed_in.get(
        reverse("compliance:complete", args=[an_obligation.pk]), headers={"HX-Request": "true"}
    )
    assert response.status_code == 404

    detail = signed_in.get(reverse("compliance:detail", args=[an_obligation.pk]))
    assert "Mark as completed" not in detail.content.decode()


def test_reopen_requires_a_reason(signed_in: Client, an_obligation: ObligationInstance) -> None:
    """ "Reopen" is the one transition that has always needed a note — the
    modal cannot be bypassed into skipping it."""
    signed_in.post(
        reverse("compliance:status", args=[an_obligation.pk]),
        {"answer": "yes", "filed_on": "2026-08-10", "filing_reference": "AA240810123456X"},
        headers={"HX-Request": "true"},
    )
    signed_in.post(
        reverse("compliance:acknowledgement", args=[an_obligation.pk]),
        {
            "acknowledgement": SimpleUploadedFile(
                "ack.pdf", b"%PDF-1.4", content_type="application/pdf"
            )
        },
        headers={"HX-Request": "true"},
    )
    signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.CLOSED},
        headers={"HX-Request": "true"},
    )
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
        assert an_obligation.state == State.CLOSED

    modal = signed_in.get(
        reverse("compliance:reopen", args=[an_obligation.pk]), headers={"HX-Request": "true"}
    )
    assert modal.status_code == 200
    assert "Why?" in modal.content.decode()

    rejected = signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.FILED},
        headers={"HX-Request": "true"},
    )
    assert rejected.status_code == 422
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
    assert an_obligation.state == State.CLOSED, "a reopen with no reason must not go through"

    accepted = signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.FILED, "note": "Submission rejected by the authority."},
        headers={"HX-Request": "true"},
    )
    assert accepted.status_code == 200
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
    assert an_obligation.state == State.FILED


def test_a_mistaken_in_progress_can_be_moved_back_without_a_reason(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """The one correction the old maker-checker table never needed: nobody
    reviewed this, so undoing it is not a judgement call worth recording a
    reason for — unlike deferring, disputing or dismissing it outright."""
    signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.IN_PREPARATION},
        headers={"HX-Request": "true"},
    )

    response = signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.NOT_STARTED},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
    assert an_obligation.state == State.NOT_STARTED


def test_the_timeline_names_the_status_change_plainly(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.IN_PREPARATION},
        headers={"HX-Request": "true"},
    )

    response = signed_in.get(reverse("compliance:detail", args=[an_obligation.pk]))
    assert "Changed status: Not started" in response.content.decode()


def test_a_status_change_on_the_detail_page_is_reflected_on_the_calendar(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """One source of truth: the calendar reads the same row the detail page
    just changed, not a cached or separately-tracked copy of its status.

    The due date is pushed safely into the future first so the calendar's own
    "worst true thing" ordering (overdue and due-soon both outrank a plain
    work status) cannot mask the very word this test is checking for.
    """
    with platform_scope(reason="test"):
        an_obligation.due_date = date.today() + timedelta(days=180)
        an_obligation.save(update_fields=["due_date"])

    signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.IN_PREPARATION},
        headers={"HX-Request": "true"},
    )

    # `status=pending` is the "open, but neither overdue nor due soon" tile —
    # the one the 180-day-out due date above actually falls into. The default
    # landing scope (`status=latest`) is bounded to 90 days and would just
    # drop the row, which is a fact about that scope, not about whether the
    # calendar is showing this obligation's current status correctly.
    response = signed_in.get(reverse("compliance:calendar"), {"status": "pending"})
    body = response.content.decode()
    row = body.split(f'id="obligation-{an_obligation.pk}"', 1)[1].split("</tr>", 1)[0]
    assert "status-chip--in-progress" in row


def test_an_obligation_off_the_happy_path_is_not_asked_whether_it_is_submitted(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """Deferred, disputed and not-applicable already have their answer in the
    status chip — asking "has this been submitted?" about one is the wrong
    question. Instead it gets the one card that explains *why* it is off the
    ladder, with the reason on it and a clear way back."""
    with platform_scope(reason="test"):
        from stacos.obligations.transitions import apply_transition

        apply_transition(
            an_obligation,
            target=State.DEFERRED,
            actor=None,
            permissions=frozenset({"compliance.obligation.defer", "compliance.obligation.view"}),
            note="Waiting on the client's bank statement.",
            as_of=AS_OF,
        )

    response = signed_in.get(reverse("compliance:detail", args=[an_obligation.pk]))
    body = response.content.decode()
    assert response.status_code == 200
    assert "Has this been submitted?" not in body
    assert "obligation-checklist" not in body
    assert 'class="stepper"' not in body
    assert "Waiting on the client" in body
    assert "Resume" in body


def test_toggling_a_step_marks_it_done_then_reopens_it(
    signed_in: Client, a_24q_obligation: ObligationInstance
) -> None:
    step = _confirm_step(a_24q_obligation)
    url = reverse("compliance:step_toggle", args=[step.pk])

    response = signed_in.post(url, headers={"HX-Request": "true"})
    assert response.status_code == 200
    with platform_scope(reason="test"):
        step.refresh_from_db()
    assert step.state == ObligationStep.State.DONE
    assert step.completed_at is not None
    assert step.completed_by_id is not None

    response = signed_in.post(url, headers={"HX-Request": "true"})
    assert response.status_code == 200
    with platform_scope(reason="test"):
        step.refresh_from_db()
    assert step.state == ObligationStep.State.PENDING
    assert step.completed_at is None


def test_blocking_a_step_requires_a_reason(
    signed_in: Client, a_24q_obligation: ObligationInstance
) -> None:
    step = _confirm_step(a_24q_obligation)
    url = reverse("compliance:step_block", args=[step.pk])

    response = signed_in.post(url, {"reason": ""}, headers={"HX-Request": "true"})
    assert response.status_code == 422
    with platform_scope(reason="test"):
        step.refresh_from_db()
    assert step.state == ObligationStep.State.PENDING

    response = signed_in.post(
        url, {"reason": "Waiting on payroll export"}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    with platform_scope(reason="test"):
        step.refresh_from_db()
    assert step.state == ObligationStep.State.BLOCKED
    assert step.blocked_reason == "Waiting on payroll export"

    # Posting again clears the block rather than asking for a second reason.
    response = signed_in.post(url, headers={"HX-Request": "true"})
    assert response.status_code == 200
    with platform_scope(reason="test"):
        step.refresh_from_db()
    assert step.state == ObligationStep.State.PENDING
    assert step.blocked_reason == ""


def test_assigning_a_step_is_independent_of_the_obligations_own_assignee(
    signed_in: Client, a_24q_obligation: ObligationInstance, org: Tenant
) -> None:
    from tests.conftest import _make_member

    colleague = _make_member(
        org, "ramesh@acme.example", "Ramesh Patel", "+919800000099", "org-owner"
    )
    step = _confirm_step(a_24q_obligation)

    response = signed_in.post(
        reverse("compliance:step_assign", args=[step.pk]),
        {"assigned_to": colleague.pk},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    with platform_scope(reason="test"):
        step.refresh_from_db()
        a_24q_obligation.refresh_from_db()
    assert step.assigned_to_id == colleague.id
    assert a_24q_obligation.assigned_to_id is None


def test_nudging_a_steps_assignee_notifies_them_and_logs_it(
    signed_in: Client, a_24q_obligation: ObligationInstance, org: Tenant
) -> None:
    from tests.conftest import _make_member

    colleague = _make_member(
        org, "ramesh@acme.example", "Ramesh Patel", "+919800000099", "org-owner"
    )
    step = _confirm_step(a_24q_obligation)
    with platform_scope(reason="test"):
        step.assigned_to = colleague
        step.save(update_fields=["assigned_to"])

    response = signed_in.post(
        reverse("compliance:step_nudge", args=[step.pk]), headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert "nudged" in response.headers["HX-Trigger"].lower()

    with platform_scope(reason="test"):
        from stacos.notifications.models import Notification

        assert Notification.objects.filter(recipient=colleague, tenant=org).exists()
        assert a_24q_obligation.events.filter(
            kind=ObligationEvent.Kind.NOTE, note__icontains="Nudged"
        ).exists()


# ===========================================================================
# Richer step templates: default assignee, "Waiting on", evidence, exposure
# ===========================================================================


def test_default_owner_role_resolves_to_the_earliest_active_member(
    a_24q_obligation: ObligationInstance, org: Tenant
) -> None:
    """Every 24Q step names ``org-owner`` as its default owner — whoever
    holds that role in this tenant should end up assigned automatically."""
    from tests.conftest import _make_member

    finance = _make_member(
        org, "fiona.finance@acme.example", "Fiona Finance", "+919800000066", "org-owner"
    )
    with platform_scope(reason="test"):
        steps = ensure_steps(a_24q_obligation)
    assert steps
    assert all(step.assigned_to_id == finance.id for step in steps)


def test_an_unresolvable_default_owner_role_leaves_the_step_unassigned(
    materialised: Entity,
) -> None:
    """A role nobody holds — or none named at all — degrades to "unassigned",
    never an error."""
    from stacos.obligations.transitions import _resolve_default_assignee

    with platform_scope(reason="test"):
        assert _resolve_default_assignee(materialised, "org-does-not-exist") is None
        assert _resolve_default_assignee(materialised, "") is None


def test_completing_a_step_that_requires_evidence_needs_a_note(
    signed_in: Client, a_24q_obligation: ObligationInstance
) -> None:
    with platform_scope(reason="test"):
        step = obligation_steps_by_key(a_24q_obligation)["generate_fvu"]
    assert step.requires_evidence is True

    url = reverse("compliance:step_toggle", args=[step.pk])
    rejected = signed_in.post(url, headers={"HX-Request": "true"})
    assert rejected.status_code == 422
    with platform_scope(reason="test"):
        step.refresh_from_db()
    assert step.state == ObligationStep.State.PENDING

    accepted = signed_in.post(
        url, {"evidence_note": "FVU validated, token captured"}, headers={"HX-Request": "true"}
    )
    assert accepted.status_code == 200
    with platform_scope(reason="test"):
        step.refresh_from_db()
    assert step.state == ObligationStep.State.DONE
    assert step.evidence_note == "FVU validated, token captured"


def obligation_steps_by_key(obligation: ObligationInstance) -> dict[str, ObligationStep]:
    return {step.key: step for step in ensure_steps(obligation)}


def test_exposure_card_shows_a_computed_total_when_the_cap_is_known(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """GSTR-3B's ₹50/day capped at ₹5,000 is fully computable."""
    response = signed_in.get(reverse("compliance:detail", args=[an_obligation.pk]))
    assert response.status_code == 200
    assert b"Section 47 CGST Act" in response.content


def test_exposure_card_shows_rate_only_when_the_cap_is_unmodeled(
    signed_in: Client, a_24q_obligation: ObligationInstance
) -> None:
    """24Q's 234E rate is capped at "the TDS amount" — not a number STACOS
    tracks, so no total is shown for it, only the rate and reference."""
    response = signed_in.get(reverse("compliance:detail", args=[a_24q_obligation.pk]))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Section 234E" in body
    assert "Section 271H" in body


@pytest.mark.skip(
    reason=(
        "IN-MCA-AOC4 — the only catalog definition with an event-triggered "
        "due date — was dropped from this fork's catalog (only GST/income-tax/"
        "TDS definitions remain); nothing currently materialises to exercise "
        "the record-event-then-reschedule flow this test covers."
    )
)
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


def test_a_forged_event_key_is_refused(signed_in: Client, materialised: Entity) -> None:
    """The key is client-supplied. An unknown one would record an event that
    triggers nothing and is invisible everywhere.

    Relocated from ``test_event_materialisation.py`` (removed along with the
    other MCA director-appointment tests it existed to support) — this one
    check is catalog-independent and worth keeping.
    """
    response = signed_in.post(
        reverse("compliance:event_create", args=[materialised.pk]),
        {"key": "NOT_A_REAL_EVENT", "occurred_on": timezone.localdate().isoformat()},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    with platform_scope(reason="test"):
        assert not EntityEvent.objects.filter(key="NOT_A_REAL_EVENT").exists()


def test_rebuilding_the_calendar_is_safe_to_repeat(
    signed_in: Client, materialised: Entity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rebuilding twice with nothing changed produces no changes the second time.

    Pinned to the fixture's own ``AS_OF`` rather than the real date: the
    planning window slides with ``as_of``, so a rebuild run today and one run
    34 real days from now legitimately pick up different periods — that is
    the planner working, not a break in idempotency. Idempotency only holds
    for two rebuilds judged against the same instant.
    """
    from stacos.obligations import views as obligation_views

    monkeypatch.setattr(obligation_views, "_today", lambda: AS_OF)

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
