"""
Self-service onboarding, end to end.

Before this flow existed, a user who registered was authenticated, owned nothing,
and had no route in the product to create the company they had signed up to
manage: ``entity_create`` opens with ``raise Http404`` when there is no tenant,
and tenants were only ever created by ``seed_dev``. The first test below is that
gap closing.

The others guard the three decisions the flow rests on: that a membership-less
user gets exactly one permission and no more, that the draft leaves nothing
behind until it is committed, and that the preview stays honest about what it
does not know.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.obligations.models import MaterialisationRun, ObligationInclusion, ObligationInstance
from stacos.tenancy.models import (
    Entity,
    EntityFactValue,
    EntityProfile,
    EntityRegistration,
    Membership,
    Tenant,
)
from stacos.tenancy.onboarding.state import DraftRegistration, OnboardingDraft
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

# A real-shaped CIN for a Karnataka private company incorporated in 2021, and the
# PAN and GSTIN that go with it.
CIN = "U72200KA2021PTC012345"
PAN = "AABCU9603R"
GSTIN = "29AABCU9603R1ZJ"


@pytest.fixture
def newcomer(db) -> User:
    """An authenticated user who is a member of nothing. The whole point."""
    return User.objects.create_user(
        email="founder@example.com", password="correct-horse-battery", full_name="Asha Founder"
    )


@pytest.fixture
def signed_in(client: Client, newcomer: User) -> Client:
    return sign_in(client, newcomer)


def test_a_user_with_no_membership_can_reach_the_wizard(signed_in: Client) -> None:
    response = signed_in.get(reverse("onboarding:identity"))
    assert response.status_code == 200


def test_a_user_with_no_membership_still_cannot_reach_anything_else(
    signed_in: Client, newcomer: User
) -> None:
    """The permission granted in the no-membership branch has to be exactly one.

    It is the only place in the product where access is decided without a tenant
    bound, so a second permission slipping in there would be a hole with no
    scope behind it.

    Asserted on the **scope**, not on the status code. Such a user is now sent to
    the setup flow rather than shown a 403, and a redirect looks identical
    whether or not access leaked — so a status assertion here would pass just as
    happily if the branch handed out every permission in the registry.
    """
    response = signed_in.get(reverse("app:entity_list"))

    scope = response.wsgi_request.access_scope
    assert scope is not None
    assert scope.permissions == frozenset({"tenancy.onboarding.start"})
    assert scope.principal_tenant_id is None
    assert scope.readable_tenant_ids == frozenset()
    assert scope.writable_tenant_ids == frozenset()


def test_identity_decoding_prefills_and_explains(signed_in: Client) -> None:
    """CIN has no dedicated field, so a pasted one is how it reaches the decoder."""
    response = signed_in.post(
        reverse("onboarding:identity_decode"), {"pasted": CIN, "pan": PAN, "gstin": GSTIN}
    )
    body = response.content.decode()

    assert response.status_code == 200
    assert "private limited company" in body
    assert "Karnataka" in body
    # The explanation names its source, so the user can tell a read from a guess.
    assert "CIN characters 13-15" in body or "CIN" in body


def test_the_identity_step_carries_what_it_read_into_the_draft(signed_in: Client) -> None:
    signed_in.post(reverse("onboarding:identity"), {"pasted": CIN, "pan": PAN, "gstin": GSTIN})

    draft = OnboardingDraft.from_session(signed_in.session)
    assert draft.entity_type == "PVT_LTD"
    assert draft.registered_office_state == "IN-KA"
    assert "IN-KA" in draft.states_of_operation
    assert {row.type for row in draft.registrations} == {"CIN", "PAN", "GST"}


def test_nothing_is_written_until_the_user_finishes(signed_in: Client) -> None:
    """An abandoned wizard must leave no rows.

    The reason the draft lives in the session rather than in a table: somebody
    who opens the wizard, types a CIN and changes their mind should not have
    created a tenant.
    """
    signed_in.post(reverse("onboarding:identity"), {"cin": CIN})
    signed_in.post(
        reverse("onboarding:profile"),
        {"name": "Nimbus Software", "entity_type": "PVT_LTD", "registered_office_state": "IN-KA"},
    )
    signed_in.get(reverse("onboarding:preview"))

    with platform_scope(reason="test"):
        assert Tenant.objects.count() == 0
        assert Entity.objects.count() == 0


def test_a_decimal_answer_survives_the_session_round_trip(signed_in: Client) -> None:
    """``aggregate_turnover`` is a ``FactType.DECIMAL`` fact.

    ``forms.DecimalField.clean()`` returns a ``decimal.Decimal``, and the
    session backend serialises the draft as JSON on every request — a raw
    ``Decimal`` used to raise ``TypeError: Object of type Decimal is not JSON
    serializable`` the moment this answer was submitted, a 500 on a real user
    typing their turnover into the wizard.
    """
    signed_in.post(reverse("onboarding:identity"), {"cin": CIN, "pan": PAN, "gstin": GSTIN})
    signed_in.post(
        reverse("onboarding:profile"),
        {"name": "Nimbus Software", "entity_type": "PVT_LTD", "registered_office_state": "IN-KA"},
    )

    response = signed_in.post(
        reverse("onboarding:answer", args=["aggregate_turnover"]), {"answer": "1200000.50"}
    )
    assert response.status_code == 200

    draft = OnboardingDraft.from_session(signed_in.session)
    assert draft.answers["aggregate_turnover"] == 1200000.5

    finish = signed_in.post(reverse("onboarding:finish"))
    assert finish.status_code in {200, 204, 302}

    with platform_scope(reason="test"):
        profile = EntityProfile.objects.get(entity__tenant__name="Nimbus Software")
        assert profile.aggregate_turnover == Decimal("1200000.50")


def test_accepting_a_pack_moves_the_finish_button_count(signed_in: Client) -> None:
    """The "Create my calendar with N obligations" button used to read
    ``preview.applies_count`` alone, so accepting or dropping a pack changed
    what ``commit_draft`` was about to build without changing the number that
    promised what it would build.
    """
    signed_in.post(reverse("onboarding:identity"), {"cin": CIN, "pan": PAN, "gstin": GSTIN})
    signed_in.post(
        reverse("onboarding:profile"),
        {"name": "Nimbus Software", "entity_type": "PVT_LTD", "registered_office_state": "IN-KA"},
    )

    before = signed_in.get(reverse("onboarding:preview"))
    baseline_count = before.context["total_count"]

    toggled = signed_in.post(reverse("onboarding:toggle_pack", args=["IN-PACK-GST"]))
    assert b'id="onboarding-finish-button"' in toggled.content

    after = signed_in.get(reverse("onboarding:preview"))
    assert after.context["total_count"] > baseline_count

    # The rendered button text itself must carry the new number, not just the
    # context variable — this is what a user actually sees on screen.
    assert str(after.context["total_count"]).encode() in after.content

    signed_in.post(reverse("onboarding:toggle_pack", args=["IN-PACK-GST"]))
    reverted = signed_in.get(reverse("onboarding:preview"))
    assert reverted.context["total_count"] == baseline_count


def test_a_turnover_too_large_for_the_column_is_rejected_not_crashed(signed_in: Client) -> None:
    """``EntityProfile.aggregate_turnover`` is ``DecimalField(max_digits=18,
    decimal_places=2)`` — 16 integer digits. Nothing capped the form field to
    match, so a value with more digits than that passed form validation clean
    and died at commit time with ``psycopg.errors.NumericValueOutOfRange``, a
    500 rather than a rejected field.
    """
    signed_in.post(reverse("onboarding:identity"), {"cin": CIN, "pan": PAN, "gstin": GSTIN})
    signed_in.post(
        reverse("onboarding:profile"),
        {"name": "Nimbus Software", "entity_type": "PVT_LTD", "registered_office_state": "IN-KA"},
    )

    response = signed_in.post(
        reverse("onboarding:answer", args=["aggregate_turnover"]),
        {"answer": "999999999999999999"},  # 18 digits — one more than the column allows
    )
    assert response.status_code == 200
    assert "stacos:toast" in response.headers.get("HX-Trigger", "")

    draft = OnboardingDraft.from_session(signed_in.session)
    assert "aggregate_turnover" not in draft.answers

    finish = signed_in.post(reverse("onboarding:finish"))
    assert finish.status_code in {200, 204, 302}


def test_a_stale_oversized_decimal_already_in_the_session_does_not_crash_finish(
    signed_in: Client,
) -> None:
    """The form-level cap only stops a *new* answer from being this large.

    A draft that already carries one — typed before the cap shipped, or from
    any other path that writes into ``draft.answers`` without going through
    ``QuestionForm`` — must not turn ``commit_draft`` into an unhandled
    ``psycopg.errors.NumericValueOutOfRange``. This reproduces exactly that: a
    value planted straight into the session, bypassing the form entirely.
    """
    signed_in.post(reverse("onboarding:identity"), {"cin": CIN, "pan": PAN, "gstin": GSTIN})
    signed_in.post(
        reverse("onboarding:profile"),
        {"name": "Nimbus Software", "entity_type": "PVT_LTD", "registered_office_state": "IN-KA"},
    )

    draft = OnboardingDraft.from_session(signed_in.session)
    draft = draft.with_(answers={**draft.answers, "aggregate_turnover": 5.555555555555555e16})
    draft.save(signed_in.session)
    signed_in.session.save()

    finish = signed_in.post(reverse("onboarding:finish"))
    assert finish.status_code in {200, 204, 302}

    with platform_scope(reason="test"):
        profile = EntityProfile.objects.get(entity__tenant__name="Nimbus Software")
        assert profile.aggregate_turnover is None


def test_a_bad_gstin_checksum_shows_a_toast_not_a_500(signed_in: Client) -> None:
    """The identity step's GSTIN field is a plain, unvalidated ``CharField`` —
    "paste whatever you have" is the whole point of that step — so a typo
    with a valid shape but the wrong check digit sails through it and only
    fails ``EntityRegistration.full_clean()``'s real ``validate_gstin`` at
    commit. That used to be an unhandled ``ValidationError``: a 500 with
    nothing on screen to say which of the identifiers was the problem.
    """
    broken_gstin = GSTIN[:-1] + ("A" if GSTIN[-1] != "A" else "B")
    signed_in.post(reverse("onboarding:identity"), {"cin": CIN, "pan": PAN, "gstin": broken_gstin})
    signed_in.post(
        reverse("onboarding:profile"),
        {"name": "Nimbus Software", "entity_type": "PVT_LTD", "registered_office_state": "IN-KA"},
    )

    response = signed_in.post(reverse("onboarding:finish"), headers={"HX-Request": "true"})
    assert response.status_code == 204

    payload = json.loads(response.headers["HX-Trigger"])
    assert payload["stacos:toast"]["level"] == "danger"
    assert "GSTIN" in payload["stacos:toast"]["message"]

    with platform_scope(reason="test"):
        assert not Tenant.objects.filter(name="Nimbus Software").exists()


def test_net_profit_and_women_employees_count_land_in_facts_not_a_missing_column(
    signed_in: Client,
) -> None:
    """Both are registered, askable facts with no matching ``EntityProfile``
    column — answering either used to raise ``TypeError`` from
    ``EntityProfile.objects.create(**_profile_columns(...))`` passing a
    keyword argument the model does not have.
    """
    signed_in.post(reverse("onboarding:identity"), {"cin": CIN, "pan": PAN, "gstin": GSTIN})
    signed_in.post(
        reverse("onboarding:profile"),
        {"name": "Nimbus Software", "entity_type": "PVT_LTD", "registered_office_state": "IN-KA"},
    )
    signed_in.post(reverse("onboarding:answer", args=["net_profit"]), {"answer": "500000"})
    signed_in.post(reverse("onboarding:answer", args=["women_employees_count"]), {"answer": "12"})

    finish = signed_in.post(reverse("onboarding:finish"))
    assert finish.status_code in {200, 204, 302}

    with platform_scope(reason="test"):
        profile = EntityProfile.objects.get(entity__tenant__name="Nimbus Software")
        assert profile.facts["net_profit"] == 500000.0
        assert profile.facts["women_employees_count"] == 12


def test_finishing_creates_everything_and_builds_the_calendar(signed_in: Client) -> None:
    signed_in.post(reverse("onboarding:identity"), {"cin": CIN, "pan": PAN, "gstin": GSTIN})
    signed_in.post(
        reverse("onboarding:profile"),
        {
            "name": "Nimbus Software",
            "legal_name": "Nimbus Software Private Limited",
            "entity_type": "PVT_LTD",
            "registered_office_state": "IN-KA",
            "incorporation_date": "2021-02-03",
        },
    )
    signed_in.post(reverse("onboarding:answer", args=["employee_count"]), {"answer": "40"})

    response = signed_in.post(reverse("onboarding:finish"))
    assert response.status_code in {200, 204, 302}

    with platform_scope(reason="test"):
        tenant = Tenant.objects.get(name="Nimbus Software")
        assert tenant.type == Tenant.Type.ORGANISATION

        membership = Membership.objects.get(tenant=tenant)
        assert membership.status == Membership.Status.ACTIVE
        assert membership.role.code == "org-owner"

        entity = Entity.objects.get(tenant=tenant)
        assert entity.entity_type == "PVT_LTD"
        assert entity.registered_office_state == "IN-KA"

        profile = EntityProfile.objects.get(entity=entity)
        assert profile.employee_count == 40
        assert "IN-KA" in profile.states_of_operation

        # The registrations the identifiers implied, actually saved.
        assert EntityRegistration.objects.filter(entity=entity, type="GST").exists()

        # An effective-dated answer is written as history, not just as a column.
        # Without this, today's headcount would be silently true for every period
        # that ever was — the failure EntityFactValue exists to prevent.
        assert EntityFactValue.objects.filter(entity=entity, key="employee_count").exists()

        run = MaterialisationRun.objects.filter(entity=entity).first()
        assert run is not None
        assert run.trigger == MaterialisationRun.Trigger.ONBOARDING

        # The calendar is there immediately, not at 01:30 tomorrow.
        assert ObligationInstance.objects.filter(entity=entity).count() > 0


def test_accepting_a_pack_writes_inclusions(signed_in: Client) -> None:
    # `IN-PACK-PVT-ROC-ANNUAL` was never a real pack — no such code exists in
    # `catalog/bundles/`, only `IN-PACK-GST` and `IN-PACK-TDS-DEDUCTOR` do —
    # so `_adopt_packs`'s `CompliancePack.objects.filter(code__in=packs)`
    # matched nothing and this test failed regardless of the code under test.
    signed_in.post(
        reverse("onboarding:profile"),
        {"name": "Nimbus Software", "entity_type": "PVT_LTD", "registered_office_state": "IN-KA"},
    )
    signed_in.post(reverse("onboarding:toggle_pack", args=["IN-PACK-GST"]))

    draft = OnboardingDraft.from_session(signed_in.session)
    assert "IN-PACK-GST" in draft.packs

    signed_in.post(reverse("onboarding:finish"))

    with platform_scope(reason="test"):
        entity = Entity.objects.get(name="Nimbus Software")
        inclusions = ObligationInclusion.objects.filter(entity=entity)
        assert inclusions.exists()
        assert all(row.source == ObligationInclusion.Source.PACK for row in inclusions)


def test_both_render_paths(signed_in: Client) -> None:
    """A direct GET is a full page; an HTMX GET is the fragment."""
    signed_in.post(
        reverse("onboarding:profile"),
        {"name": "Nimbus", "entity_type": "PVT_LTD", "registered_office_state": "IN-KA"},
    )
    page = signed_in.get(reverse("onboarding:preview"))
    assert b"<html" in page.content.lower()


# ---------------------------------------------------------------------------
# The draft, in isolation
# ---------------------------------------------------------------------------


def test_an_empty_draft_omits_facts_rather_than_denying_them() -> None:
    """The single most consequential line in the wizard.

    ``EntityProfileView`` for a *saved* entity always sets ``registrations``,
    even to the empty list — and an empty list is definite absence, so
    ``registrations includes GST`` evaluates FALSE rather than UNKNOWN. Most of
    the catalog reads exactly that clause. Copy the saved-entity shape into the
    draft and the preview turns confidently empty at the very moment it is
    supposed to be inviting.
    """
    facts = OnboardingDraft(entity_type="PVT_LTD").to_profile_view().facts

    assert "registrations" not in facts
    assert "states_of_operation" not in facts
    assert facts["entity_type"] == "PVT_LTD"


def test_a_draft_with_registrations_declares_them() -> None:
    draft = OnboardingDraft(
        entity_type="PVT_LTD",
        registrations=(DraftRegistration(type="GST", value=GSTIN, jurisdiction="IN-KA"),),
    )
    facts = draft.to_profile_view().facts
    assert facts["registrations"] == ["GST"]
    assert draft.jurisdictions == frozenset({"IN-KA"})
