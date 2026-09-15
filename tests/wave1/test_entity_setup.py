"""
The guided path from "an entity now exists" to "its calendar exists".

Three steps — registrations, preview, build — reached only while an entity
owns no obligations at all; `entity_detail` redirects here for as long as
that holds and never again once a calendar exists. See
`stacos.tenancy.entity_setup`.
"""

from __future__ import annotations

import json

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.tenancy.models import Entity, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    return sign_in(client, org_owner, step_up=True)


# ---------------------------------------------------------------------------
# entity_detail's gate
# ---------------------------------------------------------------------------


def test_a_calendar_less_entity_is_sent_to_step_one(signed_in: Client, entity_a: Entity) -> None:
    response = signed_in.get(reverse("app:entity_detail", args=[entity_a.pk]))

    assert response.status_code == 302
    assert response["Location"] == reverse("app:entity_setup_registrations", args=[entity_a.pk])


def test_a_materialised_entity_is_not_redirected(signed_in: Client, materialised: Entity) -> None:
    response = signed_in.get(reverse("app:entity_detail", args=[materialised.pk]))

    assert response.status_code == 200


def test_creating_an_entity_lands_on_step_one(signed_in: Client) -> None:
    """The full-page create path, not the modal — see `tenancy.views.entity_create`."""
    response = signed_in.post(
        reverse("app:entity_create"),
        {
            "name": "New Ventures Pvt Ltd",
            "reg_PAN": "AAACE1234F",
            "legal_name": "New Ventures Private Limited",
            "entity_type": "PVT_LTD",
            "incorporation_date": "2020-04-01",
            "registered_office_state": "IN-KA",
            "registered_office_address": "4 Residency Road, Bengaluru",
            "aggregate_turnover": "1000000",
            "employee_count": "5",
        },
    )
    assert response.status_code == 302, response.content[:2000]

    with platform_scope(reason="test"):
        entity = Entity.objects.get(name="New Ventures Pvt Ltd")
    assert response["Location"] == reverse("app:entity_setup_registrations", args=[entity.pk])


# ---------------------------------------------------------------------------
# Step one — registrations
# ---------------------------------------------------------------------------


def test_step_one_renders_both_ways(signed_in: Client, entity_a: Entity) -> None:
    page = signed_in.get(reverse("app:entity_setup_registrations", args=[entity_a.pk]))
    fragment = signed_in.get(
        reverse("app:entity_setup_registrations", args=[entity_a.pk]), headers=HTMX
    )

    assert page.status_code == 200
    assert fragment.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b'class="app-shell"' not in fragment.content


def test_step_one_offers_next_to_preview(signed_in: Client, entity_a: Entity) -> None:
    body = signed_in.get(
        reverse("app:entity_setup_registrations", args=[entity_a.pk])
    ).content.decode()

    assert reverse("app:entity_setup_preview", args=[entity_a.pk]) in body


def test_step_one_is_a_404_for_another_tenants_entity(
    client: Client, org_owner: User, rival_entity: Entity
) -> None:
    signed_in = sign_in(client, org_owner, step_up=True)

    response = signed_in.get(reverse("app:entity_setup_registrations", args=[rival_entity.pk]))

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Step two — preview
# ---------------------------------------------------------------------------


def test_step_two_hides_the_build_button(signed_in: Client, entity_a: Entity) -> None:
    body = signed_in.get(reverse("app:entity_setup_preview", args=[entity_a.pk])).content.decode()

    assert "Create my calendar" not in body
    assert "Rebuild calendar" not in body
    assert reverse("app:entity_setup_build", args=[entity_a.pk]) in body


def test_step_two_is_a_404_for_another_tenants_entity(
    client: Client, org_owner: User, rival_entity: Entity
) -> None:
    signed_in = sign_in(client, org_owner, step_up=True)

    response = signed_in.get(reverse("app:entity_setup_preview", args=[rival_entity.pk]))

    assert response.status_code == 404


def test_answering_a_question_on_step_two_keeps_the_button_hidden(
    signed_in: Client, entity_a: Entity
) -> None:
    """The interactive re-render has to carry `hide_build` forward itself —
    nothing else on the request says which page asked for it."""
    response = signed_in.post(
        reverse("compliance:answer_entity_question", args=[entity_a.pk, "qrmp_opted"])
        + "?hide_build=1",
        {"answer": "no"},
        headers=HTMX,
    )

    assert response.status_code == 200
    assert b"Create my calendar" not in response.content
    assert b"Rebuild calendar" not in response.content


# ---------------------------------------------------------------------------
# Step three — build
# ---------------------------------------------------------------------------


def test_step_three_shows_the_build_button(signed_in: Client, entity_a: Entity) -> None:
    body = signed_in.get(reverse("app:entity_setup_build", args=[entity_a.pk])).content.decode()

    assert "Create my calendar" in body
    assert reverse("app:entity_setup_preview", args=[entity_a.pk]) in body


def test_step_three_puts_the_build_button_in_the_wizard_footer(
    signed_in: Client, entity_a: Entity
) -> None:
    """Where steps one and two put their "Next", and where a user who has
    scrolled to the bottom of a wizard looks for the thing that ends it.

    Step three used to be the only step whose `wizard__actions` had an empty
    `<span></span>` on that side, with the real action a small button in the
    Compliance card's header instead — several rows up, at the far right of the
    page, reading "Rebuild calendar" as often as not. People scrolled down,
    found "Back" and nothing beside it, and reported that the step had no build
    button at all.
    """
    body = signed_in.get(reverse("app:entity_setup_build", args=[entity_a.pk])).content.decode()

    footer = body.split('class="wizard__actions"', 1)[1]
    assert reverse("compliance:rebuild", args=[entity_a.pk]) in footer, "not in the footer"
    assert "<span></span>" not in footer, "the empty placeholder is still there"

    # And exactly once on the page: the card must not render a second one in
    # its header, or the step offers the same action twice.
    assert body.count(reverse("compliance:rebuild", args=[entity_a.pk])) == 1


def test_the_card_on_step_three_has_no_build_button_of_its_own(
    signed_in: Client, entity_a: Entity
) -> None:
    """The Compliance card re-renders itself on every answered question. The
    button that ends the flow is deliberately not inside it — see
    `tenancy/setup/_fragments/build_body.html`."""
    body = signed_in.get(reverse("app:entity_setup_build", args=[entity_a.pk])).content.decode()

    card = body.split('id="entity-obligations"', 1)[1].split('class="wizard__actions"', 1)[0]
    assert reverse("compliance:rebuild", args=[entity_a.pk]) not in card


def test_step_three_is_a_404_for_another_tenants_entity(
    client: Client, org_owner: User, rival_entity: Entity
) -> None:
    signed_in = sign_in(client, org_owner, step_up=True)

    response = signed_in.get(reverse("app:entity_setup_build", args=[rival_entity.pk]))

    assert response.status_code == 404


def test_building_the_calendar_navigates_to_the_dashboard(
    signed_in: Client, entity_a: Entity
) -> None:
    """`?finish_setup=1` — the one thing the build button adds on this page —
    is what turns "rebuild in place" into "the last step of the flow".

    `HX-Location`, not the `stacos:navigate` trigger the rest of the product
    uses: the button that fires this request sits inside the region the
    response swaps, so the trigger route had to win a race against the card
    replacing itself and lost — obligations built, button gone, user still on
    "what applies to you". htmx acts on `HX-Location` in core, before any swap.
    """
    response = signed_in.post(
        reverse("compliance:rebuild", args=[entity_a.pk]) + "?finish_setup=1", headers=HTMX
    )

    assert response.status_code == 200
    location = json.loads(response["HX-Location"])
    assert location["path"] == reverse("app:dashboard")
    assert location["target"] == "#main", "a bare path reloads the whole shell"

    # The toast still has to survive: htmx reads HX-Trigger before HX-Location.
    assert "Calendar rebuilt" in response["HX-Trigger"]

    # And no card body, because htmx throws the response away on HX-Location.
    assert b"entity-obligations" not in response.content

    # And the entity page now renders normally instead of bouncing back into
    # setup: the guard is "has this entity ever been materialised", which the
    # build above satisfied, not "did the user land here afterwards".
    landed = signed_in.get(reverse("app:entity_detail", args=[entity_a.pk]))
    assert landed.status_code == 200


def test_rebuilding_without_finish_setup_does_not_navigate(
    signed_in: Client, materialised: Entity
) -> None:
    """The ordinary, steady-state "Rebuild calendar" button on the entity page
    itself must keep re-rendering in place — it never passes `finish_setup`."""
    response = signed_in.post(reverse("compliance:rebuild", args=[materialised.pk]), headers=HTMX)

    assert response.status_code == 200
    triggers = json.loads(response["HX-Trigger"])
    assert "stacos:navigate" not in triggers


def test_answering_a_question_on_step_three_keeps_the_flow_endable(
    signed_in: Client, entity_a: Entity
) -> None:
    """The bug this guards: the build button silently stopped ending the flow.

    `finish_setup` used to be a context variable the Build view set once, on a
    button rendered inside the Compliance card. Every re-render of that card
    went through `entity_preview_context`, which knew nothing about it — so
    answering a single question first replaced the button with one that posted
    without `?finish_setup=1`, and clicking it rebuilt in place and left the
    user sitting on the same page.

    The button now lives in the wizard footer, outside `#entity-obligations`,
    so a re-render of the card cannot touch it. What this asserts is that
    structural fact: the card comes back with no build button in it at all, and
    therefore with nothing to get wrong.
    """
    response = signed_in.post(
        reverse("compliance:answer_entity_question", args=[entity_a.pk, "qrmp_opted"])
        + "?hide_build=1&setup=1",
        {"answer": "no"},
        headers=HTMX,
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert reverse("compliance:rebuild", args=[entity_a.pk]) not in body, (
        "the card re-rendered a build button of its own — it is the footer's job now"
    )
    # The card itself did come back; it is the button that is absent, not the swap.
    assert 'id="entity-obligations"' in body


def test_packs_are_offered_during_setup(signed_in: Client, entity_a: Entity) -> None:
    body = signed_in.get(reverse("app:entity_setup_preview", args=[entity_a.pk])).content.decode()

    assert "Sets other businesses like yours track" in body


def test_packs_are_not_offered_on_the_entity_page(signed_in: Client, materialised: Entity) -> None:
    """Packs are a first-run suggestion, not a permanent fixture on the page an
    entity keeps — see the comment in `entity_summary.html`."""
    body = signed_in.get(
        reverse("compliance:entity_summary", args=[materialised.pk]), headers=HTMX
    ).content.decode()

    assert "Sets other businesses like yours track" not in body
    # The card itself is still there; only the strip went.
    assert "Compliance" in body
