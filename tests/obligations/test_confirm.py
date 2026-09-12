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

An obligation opted into by hand — a pack, or one at a time — has no fact
behind it either, and used to render the same "Confirm" word as an inert
``<span>`` a second time: the badge above got a real button, but the one
below still invited a click that did nothing. It gets the same fix here: a
plain "does this apply?" yes/no, "yes" recorded on the instance itself
(``confirmed_by_user``, which the planner never touches and so never
reverts), "no" routed through the same "mark not applicable" transition the
detail page's action menu already offers.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.models import AuditLog
from stacos.core.scope import platform_scope
from stacos.engine.lifecycle import State
from stacos.obligations.models import ObligationInclusion, ObligationInstance
from stacos.obligations.services import materialise
from stacos.tenancy.models import Entity, EntityProfile, Tenant
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


@pytest.fixture
def opted_in(materialised: Entity) -> ObligationInstance:
    """An obligation added by hand — no rule ever decided it, so unlike
    ``unconfirmed`` above, there is no missing fact behind it at all.

    ``IN-IT-ITR7`` (trusts, societies, Section 8 companies) is definitely
    FALSE for a PVT_LTD entity — exactly the shape that needs an opt-in to
    appear on the register at all (`stacos.engine.planner`: an opt-in
    overrides a definite NO, a plain FALSE verdict without one is dropped
    before an instance is ever created).
    """
    with platform_scope(reason="test"):
        ObligationInclusion.objects.create(
            tenant=materialised.tenant,
            entity=materialised,
            definition_code="IN-IT-ITR7",
            source=ObligationInclusion.Source.USER,
        )
        materialise(materialised, as_of=AS_OF, trigger="MANUAL")
        row = (
            ObligationInstance.objects.filter(
                entity=materialised, definition_code="IN-IT-ITR7", confirmed=False
            )
            .order_by("due_date")
            .first()
        )
    if row is None:
        pytest.skip("the opt-in did not materialise a row for this profile")
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


def test_the_badge_is_a_control_even_with_nothing_to_ask(
    signed_in: Client, opted_in: ObligationInstance
) -> None:
    """An opt-in is unconfirmed because a person chose it, not because a rule
    could not decide — but it must not be a dead badge either. Every
    unconfirmed obligation offers a real Confirm control now, fact-based or
    not."""
    body = signed_in.get(reverse("compliance:calendar") + "?status=unconfirmed").content.decode()

    assert reverse("compliance:confirm", args=[opted_in.pk]) in body
    assert "Added manually" not in body


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def test_opening_the_question_offers_something_answerable(
    signed_in: Client, unconfirmed: ObligationInstance
) -> None:
    response = signed_in.get(reverse("compliance:confirm", args=[unconfirmed.pk]), headers=HTMX)
    body = response.content.decode()

    assert response.status_code == 200
    assert 'name="fact_key"' in body
    assert 'name="answer"' in body, "a question with no input is not a question"
    assert "<!doctype html>" not in body.lower()


def test_an_opt_in_offers_yes_and_no_instead_of_a_fact_question(
    signed_in: Client, opted_in: ObligationInstance
) -> None:
    response = signed_in.get(reverse("compliance:confirm", args=[opted_in.pk]), headers=HTMX)
    body = response.content.decode()

    assert response.status_code == 200
    assert 'name="decision" value="yes"' in body
    assert 'name="decision" value="no"' in body
    assert "<!doctype html>" not in body.lower()


def test_an_already_settled_opt_in_is_not_reachable_again(
    signed_in: Client, opted_in: ObligationInstance
) -> None:
    with platform_scope(reason="test"):
        opted_in.confirmed_by_user = True
        opted_in.save(update_fields=["confirmed_by_user", "updated_at"])

    response = signed_in.get(reverse("compliance:confirm", args=[opted_in.pk]), headers=HTMX)

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
    assert "hx-swap-oob" in body, "nothing was updated out of band"
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
        entries = AuditLog.objects.filter(object_type="tenancy.EntityProfile", action="UPDATE")
        assert entries.exists(), "changing the compliance profile left no audit trail"
        assert any(fact_key in (entry.after or {}) for entry in entries), (
            f"no audit entry names {fact_key}"
        )


# ---------------------------------------------------------------------------
# Deciding without a fact — the opt-in's plain yes/no
# ---------------------------------------------------------------------------


def test_saying_yes_settles_it_without_touching_the_profile(
    signed_in: Client, opted_in: ObligationInstance, materialised: Entity
) -> None:
    """Unlike the fact-based path, "yes" here writes nothing to the entity's
    compliance profile — there was never a fact behind this row to write."""
    with platform_scope(reason="test"):
        facts_before = dict(EntityProfile.objects.get(entity=materialised).facts or {})

    response = signed_in.post(
        reverse("compliance:confirm", args=[opted_in.pk]), {"decision": "yes"}, headers=HTMX
    )
    body = response.content.decode()

    assert response.status_code == 200
    assert "hx-swap-oob" in body, "the row was not updated out of band"
    assert "calendar-counts" in body, "the counters were left stale"
    assert "stacos:modal-close" in response.headers.get("HX-Trigger", "")

    with platform_scope(reason="test"):
        refreshed = ObligationInstance.objects.get(pk=opted_in.pk)
        assert refreshed.confirmed_by_user is True
        # The rule still never decided this — only the human did. Folding the
        # two together would make `confirmed=True` a lie about where the
        # answer came from.
        assert refreshed.confirmed is False

        assert dict(EntityProfile.objects.get(entity=materialised).facts or {}) == facts_before


def test_a_yes_survives_the_next_materialisation(
    signed_in: Client, opted_in: ObligationInstance, materialised: Entity
) -> None:
    """The entire point of a separate field: the planner recomputes
    ``confirmed`` from scratch on every run and would silently flip an
    opt-in back to unconfirmed if the "yes" lived anywhere the planner
    touches."""
    signed_in.post(
        reverse("compliance:confirm", args=[opted_in.pk]), {"decision": "yes"}, headers=HTMX
    )

    with platform_scope(reason="test"):
        materialise(materialised, as_of=AS_OF, trigger="MANUAL")
        refreshed = ObligationInstance.objects.get(pk=opted_in.pk)
        assert refreshed.confirmed_by_user is True

    body = signed_in.get(reverse("compliance:calendar") + "?status=unconfirmed").content.decode()
    assert reverse("compliance:confirm", args=[opted_in.pk]) not in body


def test_saying_no_dismisses_it_through_the_same_transition_the_detail_page_uses(
    signed_in: Client, opted_in: ObligationInstance
) -> None:
    response = signed_in.post(
        reverse("compliance:confirm", args=[opted_in.pk]), {"decision": "no"}, headers=HTMX
    )
    body = response.content.decode()

    assert response.status_code == 200
    assert 'hx-swap-oob="delete"' in body

    with platform_scope(reason="test"):
        refreshed = ObligationInstance.objects.get(pk=opted_in.pk)
        assert refreshed.state == State.NOT_APPLICABLE


def test_a_dismissed_opt_in_is_not_resurrected(
    signed_in: Client, opted_in: ObligationInstance, materialised: Entity
) -> None:
    """Dismissing has to survive the nightly rebuild the same way any other
    "mark not applicable" does — mirrors
    ``test_materialisation.test_a_dismissed_obligation_is_not_resurrected``,
    for the opt-in path specifically.

    Scoped to the exact period that was dismissed: the suppression is
    deliberately per-period (``ObligationSuppression.period_key``), not
    blanket — "this year does not apply" is a different claim from "this
    never applies", and only the user knows which they mean. A second,
    later period of the same opt-in code is a different identity and stays
    live on its own.
    """
    dismissed_period = opted_in.period_key
    signed_in.post(
        reverse("compliance:confirm", args=[opted_in.pk]), {"decision": "no"}, headers=HTMX
    )

    with platform_scope(reason="test"):
        materialise(materialised, as_of=AS_OF, trigger="MANUAL")

        refreshed = ObligationInstance.objects.get(pk=opted_in.pk)
        assert refreshed.archived_at is None, "dismissing must not delete the evidence"
        assert refreshed.state == State.NOT_APPLICABLE

        still_live = ObligationInstance.objects.filter(
            entity=materialised,
            definition_code="IN-IT-ITR7",
            period_key=dismissed_period,
            archived_at__isnull=True,
        ).exclude(state=State.NOT_APPLICABLE)
        assert not still_live.exists(), "the dismissed opt-in came back as something live"


def test_saying_no_without_permission_is_refused_cleanly(
    opted_in: ObligationInstance, org: Tenant, client: Client
) -> None:
    """``confirm_obligation`` is gated on ``tenancy.profile.edit``, which is
    not the same permission the underlying dismissal transition requires
    (``compliance.obligation.dismiss``) — a role that can open this modal but
    not dismiss must get a clean refusal, not a crash or a silent no-op."""
    from tests.conftest import _make_member

    limited = _make_member(
        org, "manager@acme.example", "Manoj Manager", "+919800000401", "org-compliance-manager"
    )
    manager_client = sign_in(client, limited, step_up=True)

    response = manager_client.post(
        reverse("compliance:confirm", args=[opted_in.pk]), {"decision": "no"}, headers=HTMX
    )

    assert response.status_code == 403
    assert "danger" in response.headers.get("HX-Trigger", "")

    with platform_scope(reason="test"):
        refreshed = ObligationInstance.objects.get(pk=opted_in.pk)
        assert refreshed.state != State.NOT_APPLICABLE


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
