"""
The dashboard over HTTP: the redesigned content area, and its query budget.

The numbers on the tiles, the donut, the category bars, the weekly workload
and the overdue-ageing bars must all trace back to the same
``status_counts``/``category_counts``/``weekly_workload``/``overdue_aging``
snapshot — this file checks that parity rather than hard-coding figures that
would drift the moment a catalog definition changes.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.obligations.queries import (
    category_counts,
    overdue_aging,
    status_counts,
    weekly_workload,
)
from stacos.tenancy.models import Entity, Tenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    from stacos.accounts.middleware import SESSION_VERIFIED_KEY

    client.force_login(org_owner)
    session = client.session
    session[SESSION_VERIFIED_KEY] = True
    session.save()
    return client


def test_dashboard_renders_as_a_page_and_as_a_fragment(
    signed_in: Client, materialised: Entity
) -> None:
    page = signed_in.get(reverse("app:dashboard"))
    fragment = signed_in.get(reverse("app:dashboard"), headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert fragment.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b"Total compliances" in fragment.content
    assert b"Compliance health" in fragment.content
    assert b"Compliances by category" in fragment.content
    assert b"Upcoming workload" in fragment.content
    assert b"Overdue ageing" in fragment.content


def test_stat_tiles_agree_with_status_counts(signed_in: Client, materialised: Entity) -> None:
    """The tiles, the donut and the activity feed all read one snapshot.

    They must never be able to disagree with each other — that is the whole
    point of computing them from one aggregate rather than five ``.count()``
    calls.
    """
    with platform_scope(reason="test"):
        expected = status_counts(as_of=timezone.localdate())

    response = signed_in.get(reverse("app:dashboard"))

    counts = response.context["counts"]
    assert counts["total"] == expected["total"]
    assert counts["overdue"] == expected["overdue"]
    assert counts["due_soon"] == expected["due_soon"]
    assert counts["completed"] == expected["completed"]

    pending = response.context["pending"]
    assert pending == counts["open"] - counts["overdue"] - counts["due_soon"]
    assert pending >= 0

    health_total = sum(segment["value"] for segment in response.context["health_segments"])
    assert health_total == counts["overdue"] + counts["due_soon"] + pending + counts["completed"]


def test_category_bars_sum_to_open_obligations(signed_in: Client, materialised: Entity) -> None:
    with platform_scope(reason="test"):
        expected = category_counts()

    response = signed_in.get(reverse("app:dashboard"))
    categories = response.context["categories"]

    assert {c["label"] for c in categories} == {e["label"] for e in expected}
    assert sum(c["count"] for c in categories) == sum(c["count"] for c in expected)
    assert sum(c["count"] for c in categories) == response.context["counts"]["open"]
    # Every bar is scaled against the busiest category, so the widest one
    # always reaches full width.
    assert max(c["pct"] for c in categories) == 100.0


def test_selecting_one_entity_scopes_every_breakdown_to_it(
    signed_in: Client, materialised: Entity, org: Tenant
) -> None:
    """Two entities in one tenant must not blend into one number.

    Every stat, chart and list on the page traces back to the same
    ``entity_ids`` filter — this checks the filter actually narrows things,
    not just that the query functions accept the parameter.
    """
    from stacos.obligations.services import materialise
    from stacos.tenancy.models import EntityRegistration

    with platform_scope(reason="test"):
        second = Entity.objects.create(
            tenant=org,
            name="Second Co",
            entity_type="PVT_LTD",
            country="IN",
            registered_office_state="IN-KA",
        )
        # A bare PAN is enough to make a handful of income-tax obligations
        # apply — otherwise this entity contributes nothing and the sanity
        # check below (that the combined total actually moved) is vacuous.
        EntityRegistration.objects.create(tenant=org, entity=second, type="PAN", value="AAACS1234C")
        materialise(second, as_of=timezone.localdate(), trigger="ONBOARDING")
        expected = status_counts(as_of=timezone.localdate(), entity_ids=[materialised.id])
        combined = status_counts(as_of=timezone.localdate())

    # Sanity check: the second entity must actually have moved the combined
    # total, or this test would pass even with the filter silently ignored.
    assert combined["total"] > expected["total"]

    scoped = signed_in.get(reverse("app:dashboard"), {"entity": str(materialised.id)})
    assert scoped.context["selected_entity"].id == materialised.id
    assert scoped.context["counts"]["total"] == expected["total"]
    assert [e.id for e in scoped.context["entities"]] == [materialised.id]

    unfiltered = signed_in.get(reverse("app:dashboard"))
    assert unfiltered.context["selected_entity"] is None
    assert unfiltered.context["counts"]["total"] == combined["total"]

    # Rendered, not just present in context: `entity_options` used to chain
    # `.only("id", "name")` onto a queryset that already carried
    # `select_related("tenant")`, which Django refuses (`FieldError`) — and
    # `{% if %}` swallows exceptions while resolving its condition, so the
    # dropdown silently never appeared instead of the page erroring.
    assert b'id="dashboard-entity-filter"' in unfiltered.content
    assert b"Second Co" in unfiltered.content


@pytest.mark.parametrize(
    "bogus_id",
    ["00000000-0000-0000-0000-000000000000", "not-a-uuid-at-all"],
    ids=["well-formed-but-unknown", "malformed"],
)
def test_a_bad_entity_id_is_ignored_rather_than_erroring(
    signed_in: Client, materialised: Entity, bogus_id: str
) -> None:
    """A stale bookmark or a hand-edited URL must fall back to "all
    entities", not a 500 — `Entity.objects.filter(id=...)` raises
    `ValidationError` on a string that isn't a UUID at all."""
    response = signed_in.get(reverse("app:dashboard"), {"entity": bogus_id})
    assert response.status_code == 200
    assert response.context["selected_entity"] is None


def test_weekly_workload_agrees_with_the_query(signed_in: Client, materialised: Entity) -> None:
    with platform_scope(reason="test"):
        expected = weekly_workload(as_of=timezone.localdate())

    response = signed_in.get(reverse("app:dashboard"))
    weekly_bars = response.context["weekly_bars"]

    if not expected["overdue"] and not any(expected["weeks"]):
        assert weekly_bars == []
        return

    assert len(weekly_bars) == 1 + len(expected["weeks"])
    assert weekly_bars[0]["count"] == expected["overdue"]
    assert [row["count"] for row in weekly_bars[1:]] == expected["weeks"]
    assert max(row["pct"] for row in weekly_bars) == 100.0


def test_overdue_ageing_agrees_with_the_query(signed_in: Client, materialised: Entity) -> None:
    with platform_scope(reason="test"):
        expected = overdue_aging(as_of=timezone.localdate())

    response = signed_in.get(reverse("app:dashboard"))
    aging_bars = response.context["aging_bars"]

    if not any(expected.values()):
        assert aging_bars == []
        return

    assert [row["count"] for row in aging_bars] == [
        expected["recent"],
        expected["stale"],
        expected["old"],
    ]
    assert max(row["pct"] for row in aging_bars) == 100.0


def test_your_entities_shows_a_per_entity_overdue_count(
    signed_in: Client, materialised: Entity
) -> None:
    with platform_scope(reason="test"):
        expected_overdue = status_counts(as_of=timezone.localdate(), entity_ids=[materialised.id])[
            "overdue"
        ]

    response = signed_in.get(reverse("app:dashboard"))
    rows = {entity.id: entity for entity in response.context["entities"]}

    assert rows[materialised.id].overdue_count == expected_overdue


def test_swapped_dashboard_regions_announce_themselves(
    signed_in: Client, materialised: Entity
) -> None:
    fragment = signed_in.get(reverse("app:dashboard"), headers={"HX-Request": "true"})
    body = fragment.content.decode()

    assert 'id="dashboard-obligations"' in body
    panel = body.split('id="dashboard-obligations"')[1][:200]
    assert "aria-live" in panel


def test_the_dashboard_has_a_bounded_query_count(
    signed_in: Client, materialised: Entity, django_assert_max_num_queries: object
) -> None:
    """Five extra breakdowns — category, weekly workload, overdue ageing, a
    per-entity overdue count, and the entity filter's own option list — must
    stay a handful of aggregate queries, not one query per row.
    """
    signed_in.get(reverse("app:dashboard"))

    with django_assert_max_num_queries(17):  # type: ignore[operator]
        response = signed_in.get(reverse("app:dashboard"))
    assert response.status_code == 200
