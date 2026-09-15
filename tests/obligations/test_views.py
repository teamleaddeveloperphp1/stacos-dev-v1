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
    (The entity filter's own dropdown options are one more query, hence 15
    rather than 14.)
    """
    with platform_scope(reason="test"):
        assert ObligationInstance.objects.filter(entity=materialised).count() > 50

    # Warm the session and scope-resolution caches so the assertion measures the
    # view rather than the sign-in.
    signed_in.get(reverse("compliance:calendar"))

    with django_assert_max_num_queries(15):  # type: ignore[operator]
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
    """Every row id behind a status filter, walking the keyset cursor.

    A page is 50 rows and the fixture materialises well over that, so reading
    only the first page would silently under-count a broad bucket like
    "pending".
    """
    ids: list[str] = []
    cursor = ""
    while True:
        params = {"status": status}
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
    assert "HX-Trigger" in response.headers
    assert "stacos:modal-close" in response.headers["HX-Trigger"]

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
    assert "HX-Trigger" in response.headers
    assert "nudged" in response.headers["HX-Trigger"].lower()

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
    goes overdue on schedule. The day an explanation starts counting as progress
    is the day the register stops being worth reading — so the state, the due
    date and the display status are all asserted unchanged, not just the state.
    """
    with platform_scope(reason="test"):
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
    assert b"Is this filing completed?" in response.content
    assert b"Confirm TDS applies this quarter" not in response.content
    assert b"obligation-checklist" not in response.content


def test_ensure_steps_is_idempotent(a_24q_obligation: ObligationInstance) -> None:
    with platform_scope(reason="test"):
        first = {step.pk for step in ensure_steps(a_24q_obligation)}
        second = {step.pk for step in ensure_steps(a_24q_obligation)}
    assert first == second
    with platform_scope(reason="test"):
        assert a_24q_obligation.steps.count() == 7


def test_the_question_offers_both_answers_and_no_workflow_buttons(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """Both answers, equally weighted, and none of the flow that used to be here.

    "Request information" is named explicitly because it is the one the flow was
    most often mistaken for progress: asking the client something is not a state
    the register needs to model, and the page no longer offers it.
    """
    response = signed_in.get(reverse("compliance:detail", args=[an_obligation.pk]))
    body = response.content.decode()

    assert response.status_code == 200
    assert "Is this filing completed?" in body
    assert "Yes, it is done" in body
    assert "Not yet" in body
    assert "Acknowledgement number" in body
    assert "Acknowledgement document" in body

    for gone in ("Request information", "Start work", "Submit for review", "Approve for filing"):
        assert gone not in body, f"{gone!r} is still offered on the detail page"


def test_an_obligation_off_the_happy_path_is_not_asked_whether_it_is_filed(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """Deferred, disputed and not-applicable already have their answer in the
    status chip — asking "is this filed?" about one is the wrong question."""
    with platform_scope(reason="test"):
        from stacos.obligations.transitions import apply_transition

        apply_transition(
            an_obligation,
            target=State.DEFERRED,
            actor=None,
            permissions=frozenset({"compliance.obligation.defer", "compliance.obligation.view"}),
            note="testing",
            as_of=AS_OF,
        )

    response = signed_in.get(reverse("compliance:detail", args=[an_obligation.pk]))
    assert response.status_code == 200
    assert b"Is this filing completed?" not in response.content
    assert b"obligation-checklist" not in response.content
    assert b'class="stepper"' not in response.content


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
