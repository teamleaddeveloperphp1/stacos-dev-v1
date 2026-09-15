"""
The entity detail page's permanent "What applies to you" card.

Ported from the retired onboarding wizard's Preview step (see
``tests/wave1/test_preview_grouping.py`` for the grouping/counting logic
itself) to work against a real, saved entity instead of a session draft —
these tests are about the three views that make it interactive:
``entity_summary`` (the panel itself), ``answer_entity_question`` and
``toggle_entity_pack``.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.obligations.models import ObligationInclusion
from stacos.tenancy.models import Entity, EntityProfile, EntityRegistration, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    return sign_in(client, org_owner, step_up=True)


@pytest.fixture
def entity_with_gst(entity_a: Entity) -> Entity:
    """``entity_a``, with a GST registration — enough to suggest the GST pack."""
    with platform_scope(reason="test-fixture"):
        EntityRegistration.objects.create(
            tenant=entity_a.tenant,
            entity=entity_a,
            type="GST",
            value="24AABCU9603R1ZM",
            jurisdiction="IN-GJ",
        )
    return entity_a


# ---------------------------------------------------------------------------
# The panel itself
# ---------------------------------------------------------------------------


def test_the_panel_shows_the_build_button_before_any_calendar_exists(
    signed_in: Client, entity_a: Entity
) -> None:
    body = signed_in.get(
        reverse("compliance:entity_summary", args=[entity_a.pk]), headers=HTMX
    ).content.decode()

    assert "Create my calendar" in body
    assert "Rebuild calendar" not in body


def test_a_fresh_entity_with_no_registrations_reads_as_undecided_not_ruled_out(
    entity_a: Entity,
) -> None:
    """The false negative in the screenshot, pinned.

    ``IN-IT-BELATED-RETURN`` settles on ``registrations includes PAN``.
    ``entity_a`` has no registrations at all yet — not "confirmed none", just
    "has not had the chance" — so this must land in "might apply", not a
    confident "doesn't apply", or the very first thing a new entity's page
    says is wrong.
    """
    from datetime import date

    from stacos.obligations.preview import for_preview, preview_entity
    from stacos.obligations.profile import build_profile_view

    with platform_scope(reason="test"):
        profile = for_preview(build_profile_view(entity_a, as_of=date(2026, 8, 12)))
    preview = preview_entity(profile, country=entity_a.country)

    codes = {row.code for row in preview.might_apply}
    excluded = {row.code for row in preview.does_not_apply}
    assert "IN-IT-BELATED-RETURN" in codes, (
        "a registration-gated rule was decided FALSE for an entity that has "
        "simply never been asked, rather than left undecided"
    )
    assert "IN-IT-BELATED-RETURN" not in excluded


def test_another_tenants_entity_summary_is_not_reachable(
    client: Client, org_owner: User, rival_entity: Entity
) -> None:
    signed_in = sign_in(client, org_owner, step_up=True)

    response = signed_in.get(
        reverse("compliance:entity_summary", args=[rival_entity.pk]), headers=HTMX
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Answering a question
# ---------------------------------------------------------------------------


def test_answering_a_question_writes_the_fact_and_rebuilds(
    signed_in: Client, entity_with_gst: Entity
) -> None:
    response = signed_in.post(
        reverse("compliance:answer_entity_question", args=[entity_with_gst.pk, "qrmp_opted"]),
        {"answer": "no"},
        headers=HTMX,
    )

    assert response.status_code == 200
    with platform_scope(reason="test"):
        profile = EntityProfile.objects.get(entity=entity_with_gst)
        assert profile.facts.get("qrmp_opted") is False


def test_another_tenants_entity_cannot_be_answered_for(
    client: Client, org_owner: User, rival_entity: Entity
) -> None:
    """``rival_entity``'s fixture already answers ``qrmp_opted`` as ``False`` —
    posting "yes" would flip it, which is exactly what must not reach it."""
    signed_in = sign_in(client, org_owner, step_up=True)

    response = signed_in.post(
        reverse("compliance:answer_entity_question", args=[rival_entity.pk, "qrmp_opted"]),
        {"answer": "yes"},
        headers=HTMX,
    )

    assert response.status_code == 404
    with platform_scope(reason="test"):
        profile = EntityProfile.objects.get(entity=rival_entity)
        assert profile.facts.get("qrmp_opted") is False


# ---------------------------------------------------------------------------
# Toggling a pack
# ---------------------------------------------------------------------------


def test_toggling_a_pack_on_and_off_adds_and_revokes_its_inclusions(
    signed_in: Client, entity_with_gst: Entity
) -> None:
    added = signed_in.post(
        reverse("compliance:toggle_entity_pack", args=[entity_with_gst.pk, "IN-PACK-GST"]),
        headers=HTMX,
    )
    assert added.status_code == 200
    with platform_scope(reason="test"):
        live = ObligationInclusion.objects.filter(
            entity=entity_with_gst,
            pack_code="IN-PACK-GST",
            revoked_at__isnull=True,
        )
        assert live.exists()

    removed = signed_in.post(
        reverse("compliance:toggle_entity_pack", args=[entity_with_gst.pk, "IN-PACK-GST"]),
        headers=HTMX,
    )
    assert removed.status_code == 200
    with platform_scope(reason="test"):
        assert not ObligationInclusion.objects.filter(
            entity=entity_with_gst,
            pack_code="IN-PACK-GST",
            revoked_at__isnull=True,
        ).exists()
        # Revoked, not deleted — the row itself survives.
        assert ObligationInclusion.objects.filter(
            entity=entity_with_gst, pack_code="IN-PACK-GST"
        ).exists()


def test_another_tenants_entity_cannot_have_a_pack_toggled(
    client: Client, org_owner: User, rival_entity: Entity
) -> None:
    signed_in = sign_in(client, org_owner, step_up=True)

    response = signed_in.post(
        reverse("compliance:toggle_entity_pack", args=[rival_entity.pk, "IN-PACK-GST"]),
        headers=HTMX,
    )

    assert response.status_code == 404
    with platform_scope(reason="test"):
        assert not ObligationInclusion.objects.filter(entity=rival_entity).exists()
