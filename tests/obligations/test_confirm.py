"""
Answering the question behind an unconfirmed obligation.

An obligation whose applicability rule evaluated to UNKNOWN is materialised
anyway and flagged, because silently dropping one is what gets a client
penalised. The flag was all there was: "Confirm" rendered as an inert ``<span>``
whose tooltip invited the user to "confirm a few details" beside no way to do so.

The detail being asked for was never missing — ``Verdict.missing_facts`` names
exactly the facts that were absent *and* would have changed the answer. It was
computed on every evaluation and discarded with the verdict. Persisting it is
what turns the badge into a question.

Two things are asserted here that a screenshot could not show: that answering
recalculates immediately rather than waiting for the nightly run, and that one
tenant cannot reach another tenant's obligation through this route — as a 404,
not a 403, because confirming that a row exists elsewhere is itself a disclosure.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.models import AuditLog
from stacos.core.scope import platform_scope
from stacos.obligations.models import ObligationInstance
from stacos.tenancy.models import Entity, EntityProfile
from tests.conftest import AS_OF, sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


@pytest.fixture
def signed_in(client: Client, org_owner: User) -> Client:
    return sign_in(client, org_owner, step_up=True)


@pytest.fixture
def unconfirmed(materialised: Entity) -> ObligationInstance:
    """An obligation the rules could not decide, with a fact behind it.

    Found rather than constructed: an instance built by hand would prove that the
    view renders, not that the engine and the register agree about what is
    unknown — which is the half that was broken.
    """
    with platform_scope(reason="test"):
        row = (
            ObligationInstance.objects.filter(entity=materialised, confirmed=False)
            .exclude(missing_facts=[])
            .order_by("due_date")
            .first()
        )
    if row is None:
        pytest.skip("the live catalog produced no undecidable obligation for this profile")
    return row


def _forget(entity: Entity, key: str) -> None:
    """Remove a fact from the profile so its rule becomes undecidable again."""
    with platform_scope(reason="test"):
        profile = EntityProfile.objects.get(entity=entity)
        facts = dict(profile.facts or {})
        facts.pop(key, None)
        profile.facts = facts
        profile.save(update_fields=["facts", "updated_at"])


# ---------------------------------------------------------------------------
# The engine's answer reaches the register
# ---------------------------------------------------------------------------


def test_the_blocking_facts_are_persisted_not_discarded(
    unconfirmed: ObligationInstance,
) -> None:
    assert unconfirmed.missing_facts, (
        "the obligation is unconfirmed but does not say what would confirm it"
    )


def test_a_confirmed_obligation_names_no_missing_facts(materialised: Entity) -> None:
    """The flag and the reason have to agree, in both directions."""
    with platform_scope(reason="test"):
        decided = ObligationInstance.objects.filter(entity=materialised, confirmed=True)
        assert decided.exists(), "fixture sanity"
        assert not decided.exclude(missing_facts=[]).exists(), (
            "a decided obligation is still carrying a question"
        )


# ---------------------------------------------------------------------------
# The badge
# ---------------------------------------------------------------------------


def test_the_badge_is_a_control_when_there_is_something_to_ask(
    signed_in: Client, unconfirmed: ObligationInstance
) -> None:
    body = signed_in.get(reverse("compliance:calendar") + "?status=unconfirmed").content.decode()

    assert reverse("compliance:confirm", args=[unconfirmed.pk]) in body


def test_the_badge_stays_inert_for_an_obligation_nobody_can_answer_for(
    signed_in: Client, unconfirmed: ObligationInstance
) -> None:
    """An opt-in is unconfirmed because a person chose it, not because a rule
    could not decide. There is no question, so there must be no button."""
    with platform_scope(reason="test"):
        unconfirmed.missing_facts = []
        unconfirmed.save(update_fields=["missing_facts", "updated_at"])

    body = signed_in.get(reverse("compliance:calendar") + "?status=unconfirmed").content.decode()

    assert reverse("compliance:confirm", args=[unconfirmed.pk]) not in body
    assert "Confirm" in body, "the badge itself should still be shown"


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def test_opening_the_question_offers_something_answerable(
    signed_in: Client, unconfirmed: ObligationInstance
) -> None:
    response = signed_in.get(
        reverse("compliance:confirm", args=[unconfirmed.pk]), headers=HTMX
    )
    body = response.content.decode()

    assert response.status_code == 200
    assert 'name="fact_key"' in body
    assert 'name="answer"' in body, "a question with no input is not a question"
    assert "<!doctype html>" not in body.lower()


def test_an_obligation_with_no_question_is_not_reachable(
    signed_in: Client, unconfirmed: ObligationInstance
) -> None:
    with platform_scope(reason="test"):
        unconfirmed.missing_facts = []
        unconfirmed.save(update_fields=["missing_facts", "updated_at"])

    response = signed_in.get(
        reverse("compliance:confirm", args=[unconfirmed.pk]), headers=HTMX
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


def _answer(client: Client, obligation: ObligationInstance, value: str) -> Any:
    fact_key = obligation.missing_facts[0]
    return client.post(
        reverse("compliance:confirm", args=[obligation.pk]),
        {"fact_key": fact_key, "answer": value},
        headers=HTMX,
    )


def test_answering_records_the_fact_and_recalculates_immediately(
    signed_in: Client, unconfirmed: ObligationInstance, materialised: Entity
) -> None:
    """No background job. The user has just changed what applies to them, and
    "your calendar will catch up overnight" throws away the point of asking."""
    fact_key = unconfirmed.missing_facts[0]

    response = _answer(signed_in, unconfirmed, "yes")

    assert response.status_code == 200

    with platform_scope(reason="test"):
        profile = EntityProfile.objects.get(entity=materialised)
        stored = (profile.facts or {}).get(fact_key, getattr(profile, fact_key, None))
        assert stored is not None, f"{fact_key} was not recorded anywhere"

        settled = ObligationInstance.objects.filter(pk=unconfirmed.pk).first()
        if settled is not None:
            assert fact_key not in settled.missing_facts, (
                "the obligation is still asking for a fact it has been given"
            )


def test_answering_updates_the_screen_without_a_reload(
    signed_in: Client, unconfirmed: ObligationInstance
) -> None:
    """The row and the counters, in one response.

    One answer can settle several obligations at once — that is what ranking by
    "how much does this unlock" means — so the counters have to move too.
    """
    response = _answer(signed_in, unconfirmed, "yes")
    body = response.content.decode()

    assert response.status_code == 200
    assert 'hx-swap-oob' in body, "nothing was updated out of band"
    assert "calendar-counts" in body, "the counters were left stale"
    assert "stacos:modal-close" in response.headers.get("HX-Trigger", "")


def test_not_sure_yet_changes_nothing(
    signed_in: Client, unconfirmed: ObligationInstance, materialised: Entity
) -> None:
    """Three-valued logic all the way to the UI.

    "Not sure yet" has to stay distinguishable from "no". Recorded as a denial it
    would silently remove obligations the user never ruled out — which is the
    whole reason the engine is Kleene rather than boolean.
    """
    fact_key = unconfirmed.missing_facts[0]

    response = _answer(signed_in, unconfirmed, "")

    assert response.status_code == 200
    with platform_scope(reason="test"):
        profile = EntityProfile.objects.get(entity=materialised)
        assert fact_key not in (profile.facts or {})

        still_open = ObligationInstance.objects.get(pk=unconfirmed.pk)
        assert not still_open.confirmed


def test_answering_is_audited(
    signed_in: Client, unconfirmed: ObligationInstance, materialised: Entity
) -> None:
    """What a business shows a regulator: who changed the profile, and when."""
    fact_key = unconfirmed.missing_facts[0]

    _answer(signed_in, unconfirmed, "yes")

    with platform_scope(reason="test"):
        entries = AuditLog.objects.filter(
            object_type="tenancy.EntityProfile", action="UPDATE"
        )
        assert entries.exists(), "changing the compliance profile left no audit trail"
        assert any(fact_key in (entry.after or {}) for entry in entries), (
            f"no audit entry names {fact_key}"
        )


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


def test_another_tenant_cannot_open_the_question(
    client: Client, rival_owner: User, unconfirmed: ObligationInstance
) -> None:
    """404, not 403. Confirming that the row exists is itself a disclosure."""
    hostile = sign_in(client, rival_owner, step_up=True)

    response = hostile.get(reverse("compliance:confirm", args=[unconfirmed.pk]), headers=HTMX)

    assert response.status_code == 404


def test_another_tenant_cannot_answer_it(
    client: Client, rival_owner: User, unconfirmed: ObligationInstance, materialised: Entity
) -> None:
    hostile = sign_in(client, rival_owner, step_up=True)
    fact_key = unconfirmed.missing_facts[0]

    response = hostile.post(
        reverse("compliance:confirm", args=[unconfirmed.pk]),
        {"fact_key": fact_key, "answer": "yes"},
        headers=HTMX,
    )

    assert response.status_code == 404

    with platform_scope(reason="test"):
        profile = EntityProfile.objects.get(entity=materialised)
        assert fact_key not in (profile.facts or {}), "a foreign tenant wrote to this profile"


def test_the_as_of_date_is_the_fixture_date(materialised: Entity) -> None:
    """Guard for the fixtures above, which are anchored on a fixed date.

    A calendar test whose expectations move with the wall clock is a calendar
    test that gets switched off in March.
    """
    assert AS_OF.year == 2026
