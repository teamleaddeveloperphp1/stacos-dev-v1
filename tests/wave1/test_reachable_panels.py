"""
The screens that had no link, now that they have one.

Thirteen working routes were unreachable: the views ran, the templates rendered
and the permissions were declared, but nothing in the application pointed at
them. The most damaging was the per-entity compliance summary, because it is the
only place the "Rebuild calendar" button exists anywhere in the product — so
after changing an entity's profile there was no way to rebuild its calendar.

These tests check the panels answer, and that the entity and obligation pages
actually reference them. ``tests/test_templates.py`` guards the general rule;
this is the behaviour behind it.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.tenancy.models import Entity, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    return sign_in(client, org_owner, step_up=True)


@pytest.mark.parametrize(
    ("route", "marker"),
    [
        ("compliance:entity_summary", b"Rebuild calendar"),
        ("secretarial:entity_summary", b"Secretarial"),
        ("notices:for_entity", b"notices"),
    ],
    ids=["compliance summary", "secretarial summary", "entity notices"],
)
def test_the_entity_panels_answer(
    route: str, marker: bytes, signed_in: Client, entity_a: Entity
) -> None:
    response = signed_in.get(reverse(route, args=[entity_a.pk]), headers=HTMX)

    assert response.status_code == 200
    assert marker.lower() in response.content.lower()


def test_the_entity_page_asks_for_all_three_panels(signed_in: Client, entity_a: Entity) -> None:
    """Without these, the rebuild button has no home and cannot be pressed."""
    body = signed_in.get(reverse("app:entity_detail", args=[entity_a.pk])).content.decode()

    for route in ("compliance:entity_summary", "secretarial:entity_summary", "notices:for_entity"):
        assert reverse(route, args=[entity_a.pk]) in body, f"{route} is not loaded by the page"


def test_the_rebuild_calendar_control_is_reachable(signed_in: Client, entity_a: Entity) -> None:
    """The whole point of restoring the compliance panel."""
    panel = signed_in.get(
        reverse("compliance:entity_summary", args=[entity_a.pk]), headers=HTMX
    ).content.decode()

    assert reverse("compliance:rebuild", args=[entity_a.pk]) in panel


def test_rebuilding_sends_back_the_table_it_lives_in(
    signed_in: Client, manufacturer: Entity
) -> None:
    """The button was doing its job invisibly.

    The plan ran, the toast fired and the data was right, but the response body
    was the calendar's counter strip — a fragment belonging to a page this
    button does not appear on — and the form threw it away with
    ``hx-swap="none"``. So the table in front of the user stayed exactly as it
    was until a manual reload, which reads as a button that does nothing.
    """
    response = signed_in.post(reverse("compliance:rebuild", args=[manufacturer.pk]), headers=HTMX)

    assert response.status_code == 200
    body = response.content.decode()
    assert 'id="entity-obligations"' in body, (
        "the rebuild must return the card it was pressed in, or nothing on screen moves"
    )
    assert "Compliance" in body
    assert reverse("compliance:rebuild", args=[manufacturer.pk]) in body, (
        "the swapped-in card has to carry the button again"
    )


def test_rebuilding_still_toasts(signed_in: Client, manufacturer: Entity) -> None:
    """The summary of what changed is the other half of the feedback."""
    response = signed_in.post(reverse("compliance:rebuild", args=[manufacturer.pk]), headers=HTMX)

    triggers = response["HX-Trigger"]
    assert "stacos:toast" in triggers
    assert "Calendar rebuilt" in triggers
    assert "stacos:calendar-rebuilt" in triggers


def test_the_rebuild_control_swaps_rather_than_discarding(
    signed_in: Client, entity_a: Entity
) -> None:
    """Both controls — the one in the header and the one in the empty state."""
    panel = signed_in.get(
        reverse("compliance:entity_summary", args=[entity_a.pk]), headers=HTMX
    ).content.decode()

    assert 'hx-swap="none"' not in panel, (
        "a rebuild whose response is discarded cannot update the table"
    )
    assert 'hx-target="#entity-obligations"' in panel


def test_the_plans_page_renders_both_ways(signed_in: Client) -> None:
    page = signed_in.get(reverse("billing:plans"))
    fragment = signed_in.get(reverse("billing:plans"), headers=HTMX)

    assert page.status_code == 200
    assert fragment.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()


def test_the_resolutions_page_renders_both_ways(signed_in: Client) -> None:
    """It was fragment-only, so a deep link or a refresh returned a bare fragment."""
    page = signed_in.get(reverse("secretarial:resolutions"))
    fragment = signed_in.get(reverse("secretarial:resolutions"), headers=HTMX)

    assert page.status_code == 200
    assert fragment.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()


def test_the_billing_overview_offers_the_plans_page(signed_in: Client) -> None:
    body = signed_in.get(reverse("billing:overview")).content.decode()
    assert reverse("billing:plans") in body


def test_the_entity_row_offers_archiving(signed_in: Client, entity_a: Entity) -> None:
    body = signed_in.get(reverse("app:entity_list")).content.decode()
    assert reverse("app:entity_archive", args=[entity_a.pk]) in body


def test_the_completed_tile_filters_the_calendar(signed_in: Client) -> None:
    """It was a plain span while every sibling tile was a link."""
    body = signed_in.get(reverse("compliance:calendar")).content.decode()

    assert 'href="?status=completed"' in body
    assert 'hx-get="?status=completed"' in body
