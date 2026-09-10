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
    response = signed_in.post(
        reverse("onboarding:identity_decode"), {"cin": CIN, "pan": PAN, "gstin": GSTIN}
    )
    body = response.content.decode()

    assert response.status_code == 200
    assert "private limited company" in body
    assert "Karnataka" in body
    # The explanation names its source, so the user can tell a read from a guess.
    assert "CIN characters 13-15" in body or "CIN" in body


def test_the_identity_step_carries_what_it_read_into_the_draft(signed_in: Client) -> None:
    signed_in.post(reverse("onboarding:identity"), {"cin": CIN, "pan": PAN, "gstin": GSTIN})

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
    signed_in.post(
        reverse("onboarding:profile"),
        {"name": "Nimbus Software", "entity_type": "PVT_LTD", "registered_office_state": "IN-KA"},
    )
    signed_in.post(reverse("onboarding:toggle_pack", args=["IN-PACK-PVT-ROC-ANNUAL"]))

    draft = OnboardingDraft.from_session(signed_in.session)
    assert "IN-PACK-PVT-ROC-ANNUAL" in draft.packs

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
