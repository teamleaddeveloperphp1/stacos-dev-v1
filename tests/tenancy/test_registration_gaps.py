"""
The "Add a registration" picker offers only what the entity does not already
hold, and the setup step says when nothing on the entity type's list is left.

Runs against the live India pack, so the per-state identifiers (GST, PT, …) are
whatever ``catalog/packs/IN.yaml`` marks ``per_jurisdiction`` — one test flips
that flag on the pack to prove nothing reads it from a list in code.
"""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.jurisdictions.models import JurisdictionPack
from stacos.tenancy.models import Entity, EntityRegistration
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}

#: `PVT_LTD`'s list in `IN.yaml`, which is what `entity_a` is — CIN first and
#: PAN after it, as `registration_requirements` pins them.
PVT_LTD_TYPES = ["CIN", "PAN", "TAN", "GST", "ESIC", "PF", "IEC", "UDYAM"]


@pytest.fixture
def signed_in(client: Client, org_owner: User) -> Client:
    return sign_in(client, org_owner, step_up=True)


def _hold(entity: Entity, code: str, jurisdiction: str = "", **extra: object) -> None:
    with platform_scope(reason="test-fixture"):
        EntityRegistration.objects.create(
            tenant=entity.tenant,
            entity=entity,
            type=code,
            value=f"{code}-{jurisdiction or 'X'}",
            jurisdiction=jurisdiction,
            **extra,
        )


def _options(html: str, name: str) -> list[str]:
    select = re.search(rf'<select name="{name}".*?</select>', html, re.S)
    assert select is not None, f"no <select name={name!r}> in the response"
    return re.findall(r'<option value="([^"]*)"', select.group(0))


def _modal(client: Client, entity: Entity, **params: str) -> str:
    response = client.get(
        reverse("app:registration_create", args=[entity.pk]), params, headers=HTMX
    )
    assert response.status_code == 200
    return response.content.decode()


def test_the_picker_offers_the_entity_types_list(signed_in: Client, entity_a: Entity) -> None:
    assert _options(_modal(signed_in, entity_a), "type") == ["", *PVT_LTD_TYPES]


def test_a_held_registration_is_no_longer_offered(signed_in: Client, entity_a: Entity) -> None:
    _hold(entity_a, "PAN")
    _hold(entity_a, "TAN")

    offered = _options(_modal(signed_in, entity_a), "type")

    assert "PAN" not in offered
    assert "TAN" not in offered
    assert "CIN" in offered


def test_a_lapsing_registration_does_not_block_its_successor(
    signed_in: Client, entity_a: Entity
) -> None:
    """Same rule as `entityreg_active_unique`: an end date frees the slot."""
    _hold(entity_a, "IEC", valid_to=date(2026, 12, 31))

    assert "IEC" in _options(_modal(signed_in, entity_a), "type")


def test_a_per_state_type_stays_offered_with_its_state_removed(
    signed_in: Client, entity_a: Entity
) -> None:
    _hold(entity_a, "GST", "IN-GJ")

    assert "GST" in _options(_modal(signed_in, entity_a), "type")

    states = _options(_modal(signed_in, entity_a, type="GST"), "jurisdiction")
    assert "IN-GJ" not in states
    assert "IN-MH" in states
    # The re-rendered State list is the region `hx-select` swaps, and it is live.
    assert 'id="registration-jurisdiction"' in _modal(signed_in, entity_a, type="GST")
    assert 'aria-live="polite"' in _modal(signed_in, entity_a, type="GST")


def test_the_flag_is_read_from_the_pack(signed_in: Client, entity_a: Entity) -> None:
    """Mark TAN per-state on the pack and it behaves like GST; nothing in code
    names which identifiers repeat."""
    _hold(entity_a, "TAN")
    assert "TAN" not in _options(_modal(signed_in, entity_a), "type")

    pack = JurisdictionPack.objects.get(country="IN")
    for entry in pack.registration_types["catalog"]:
        if entry["code"] == "TAN":
            entry["per_jurisdiction"] = True
    pack.save(update_fields=["registration_types"])

    assert "TAN" in _options(_modal(signed_in, entity_a), "type")
    # The blank slot is held, so the "Select…" placeholder no longer means
    # "not state-specific": leaving it is refused, not saved as a duplicate.
    response = signed_in.post(
        reverse("app:registration_create", args=[entity_a.pk]),
        {"type": "TAN", "value": "MUMA12345B", "jurisdiction": ""},
        headers=HTMX,
    )
    assert response.status_code == 422
    assert "already recorded without a state" in response.content.decode()


def test_posting_a_taken_state_is_refused_by_name(signed_in: Client, entity_a: Entity) -> None:
    _hold(entity_a, "GST", "IN-GJ")

    response = signed_in.post(
        reverse("app:registration_create", args=[entity_a.pk]),
        {"type": "GST", "value": "24AABCS1429B1Z5", "jurisdiction": "IN-GJ"},
        headers=HTMX,
    )

    assert response.status_code == 422
    assert "already recorded for that state" in response.content.decode()


def test_posting_a_held_type_is_refused(signed_in: Client, entity_a: Entity) -> None:
    """A forged post, since the picker no longer offers it."""
    _hold(entity_a, "PAN")

    response = signed_in.post(
        reverse("app:registration_create", args=[entity_a.pk]),
        {"type": "PAN", "value": "AABCS1429B"},
        headers=HTMX,
    )

    assert response.status_code == 422
    assert "already recorded" in response.content.decode()


def test_the_setup_step_lists_what_is_still_to_record(signed_in: Client, entity_a: Entity) -> None:
    _hold(entity_a, "PAN")

    response = signed_in.get(reverse("app:entity_setup_registrations", args=[entity_a.pk]))
    body = response.content.decode()

    assert "Still to record" in body
    assert "TAN" in body
    assert "All recorded" not in body


def test_the_setup_step_reads_finished_once_nothing_is_left(
    signed_in: Client, entity_a: Entity
) -> None:
    for code in PVT_LTD_TYPES:
        _hold(entity_a, code, "IN-GJ" if code == "GST" else "")

    response = signed_in.get(reverse("app:entity_setup_registrations", args=[entity_a.pk]))
    body = response.content.decode()

    assert "All recorded" in body
    assert "Still to record" not in body
    # GST can still be added for another state, so the button stays.
    assert "?setup=1" in body
    assert _options(_modal(signed_in, entity_a), "type") == ["", "GST"]


def test_adding_from_setup_updates_the_note_out_of_band(
    signed_in: Client, entity_a: Entity
) -> None:
    url = reverse("app:registration_create", args=[entity_a.pk])
    data = {"type": "PAN", "value": "AABCS1429B", "jurisdiction": ""}

    from_setup = signed_in.post(url, {**data, "setup": "1"}, headers=HTMX).content.decode()
    assert 'hx-swap-oob="innerHTML:#registration-progress"' in from_setup
    assert "Still to record" in from_setup

    with platform_scope(reason="test"):
        EntityRegistration.objects.filter(entity=entity_a).delete()

    from_detail = signed_in.post(url, data, headers=HTMX).content.decode()
    assert "registration-progress" not in from_detail
