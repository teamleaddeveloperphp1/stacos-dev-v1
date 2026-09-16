"""
Editing an entity after it has been created.

Until this existed, the only remedy for a typo in a name, the wrong entity type
or a registered office in the wrong state was to archive the entity and create
another one — which throws away its obligations, its documents and its audit
trail. "Archive and retype" is not a correction; it is a loss of continuity
dressed up as one.

``tenancy.entity.edit`` had been registered in the permission catalogue and
granted to every entity steward since the beginning. It simply never had a view.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.models import AuditAction, AuditLog
from stacos.core.scope import platform_scope
from stacos.tenancy.forms import registration_field_name
from stacos.tenancy.models import Entity, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    # `tenancy.registration.manage` is sensitive: saving any identifier field
    # through this same screen now demands a fresh step-up, the same as the
    # standalone "Add Registration" modal already does.
    return sign_in(client, org_owner, step_up=True)


def _payload(entity: Entity, **overrides: str) -> dict[str, str]:
    """The whole form. A ModelForm treats an absent field as cleared —
    including the identifier fields now: ``org-owner`` (this file's
    ``signed_in`` actor) holds ``tenancy.registration.manage``, so an edit
    that omits an entity's existing PAN/GST/etc. would read as "the user
    cleared it", exactly as omitting ``name`` would. Seeding them here from
    what is actually on file is what a real browser does too — the fields are
    real inputs in the same ``<form>``, pre-filled by the GET.

    ``aggregate_turnover``/``employee_count`` are optional, but a fixed valid
    pair belongs in every payload here anyway, so tests exercising other
    fields are not also exercising the blank case.

    PAN is mandatory for every entity type too, and the base fixtures
    deliberately carry no registrations at all (``test_entity_preview`` has a
    test whose whole subject is an entity with none), so a valid one is
    defaulted in here and overridden by any the entity actually holds.
    """
    incorporated = entity.incorporation_date
    data = {
        "reg_PAN": "AAACE1234F",
        "name": entity.name,
        "legal_name": entity.legal_name or "",
        "entity_type": entity.entity_type,
        "incorporation_date": incorporated.isoformat() if incorporated else "",
        "registered_office_state": entity.registered_office_state or "",
        "registered_office_address": entity.registered_office_address or "",
        "aggregate_turnover": "50000000.00",
        "employee_count": "25",
    }
    with platform_scope(reason="test-payload"):
        for registration in entity.registrations.filter(jurisdiction="", archived_at__isnull=True):
            data[registration_field_name(registration.type)] = registration.value
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# The modal
# ---------------------------------------------------------------------------


def test_the_edit_modal_arrives_pre_filled(signed_in: Client, entity_a: Entity) -> None:
    """Pre-filled, or it is an "add" form that silently blanks what it omits."""
    response = signed_in.get(reverse("app:entity_edit", args=[entity_a.pk]), headers=HTMX)
    body = response.content.decode()

    assert response.status_code == 200
    assert "Edit entity" in body
    assert "Save changes" in body
    assert entity_a.name in body
    assert reverse("app:entity_edit", args=[entity_a.pk]) in body
    assert f"#entity-row-{entity_a.pk}" in body


def test_the_create_modal_still_says_add(signed_in: Client) -> None:
    """The two share a template now; the labels must not have followed each other."""
    body = signed_in.get(reverse("app:entity_create"), headers=HTMX).content.decode()

    assert "Add an entity" in body
    assert reverse("app:entity_create") in body
    assert "Just the essentials for now" in body


def test_the_edit_modal_drops_the_onboarding_copy(signed_in: Client, entity_a: Entity) -> None:
    """Someone correcting a typo has already been told what comes next."""
    body = signed_in.get(
        reverse("app:entity_edit", args=[entity_a.pk]), headers=HTMX
    ).content.decode()

    assert "Just the essentials for now" not in body


def test_the_list_row_offers_editing(signed_in: Client, entity_a: Entity) -> None:
    """A view nothing links to is a view nobody can reach."""
    body = signed_in.get(reverse("app:entity_list")).content.decode()

    assert reverse("app:entity_edit", args=[entity_a.pk]) in body


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def test_saving_updates_the_same_entity(signed_in: Client, entity_a: Entity) -> None:
    """The same row, not a second one — which is the whole point of the feature."""
    with platform_scope(reason="test"):
        before = Entity.objects.count()

    response = signed_in.post(
        reverse("app:entity_edit", args=[entity_a.pk]),
        _payload(entity_a, name="Acme Textiles Limited", registered_office_state="IN-MH"),
        headers=HTMX,
    )

    assert response.status_code == 200
    entity_a.refresh_from_db()
    assert entity_a.name == "Acme Textiles Limited"
    assert entity_a.registered_office_state == "IN-MH"
    with platform_scope(reason="test"):
        assert Entity.objects.count() == before, "the edit created a second entity"


def test_the_response_is_the_row_it_replaces(signed_in: Client, entity_a: Entity) -> None:
    """It is swapped into ``#entity-row-<pk>``, so it has to *be* that row."""
    body = signed_in.post(
        reverse("app:entity_edit", args=[entity_a.pk]),
        _payload(entity_a, name="Acme Textiles Limited"),
        headers=HTMX,
    ).content.decode()

    assert f'id="entity-row-{entity_a.pk}"' in body
    assert "Acme Textiles Limited" in body


def test_saving_closes_the_modal_and_toasts(signed_in: Client, entity_a: Entity) -> None:
    response = signed_in.post(
        reverse("app:entity_edit", args=[entity_a.pk]),
        _payload(entity_a, name="Acme Textiles Limited"),
        headers=HTMX,
    )

    triggers = response["HX-Trigger"]
    assert "stacos:modal-close" in triggers
    assert "stacos:toast" in triggers
    assert "Save changes" in triggers, "the user is not told the calendar may now be stale"


def test_the_edited_row_keeps_its_registration_count(
    signed_in: Client, manufacturer: Entity
) -> None:
    """``registration_count`` is an annotation, and the template falls back to zero.

    Right for a newly created entity, wrong for an edited one: without the
    annotation an entity with eleven registrations would report none the moment
    somebody fixed its name.
    """
    with platform_scope(reason="test"):
        expected = manufacturer.registrations.filter(archived_at__isnull=True).count()
    assert expected > 1, "the fixture is meant to carry several registrations"

    # The fixture's PAN/TAN/CIN/ESIC/PF values (`PANXXTEST`, ...) were written
    # straight through the ORM and never format-checked. `_payload()` now
    # round-trips them through the real identifier fields on this same screen,
    # which *does* check format — so a real-shaped value stands in for each
    # here, the same way a person fixing the format would retype it, rather
    # than leaving the fixture's placeholder to be rejected and archived as
    # "cleared".
    body = signed_in.post(
        reverse("app:entity_edit", args=[manufacturer.pk]),
        _payload(
            manufacturer,
            name="Renamed Manufacturing Pvt Ltd",
            reg_PAN="AAACE1234F",
            reg_TAN="ABCD12345E",
            reg_CIN="U72200KA2015PTC012345",
            reg_ESIC="12345678901234567",
            reg_PF="KN/BNG/0012345/000",
        ),
        headers=HTMX,
    ).content.decode()

    assert re.search(rf'<td class="num">\s*{expected}\s*</td>', body), (
        f"the row does not report {expected} registrations: {body}"
    )


def test_an_invalid_edit_keeps_the_form_open(
    signed_in: Client, entity_a: Entity, entity_b: Entity
) -> None:
    """A duplicate name comes back as 422 with the form, not as a saved row."""
    response = signed_in.post(
        reverse("app:entity_edit", args=[entity_a.pk]),
        _payload(entity_a, name=entity_b.name),
        headers=HTMX,
    )

    assert response.status_code == 422
    assert b"already have an entity with this name" in response.content
    entity_a.refresh_from_db()
    assert entity_a.name != entity_b.name


def test_an_entity_may_keep_its_own_name(signed_in: Client, entity_a: Entity) -> None:
    """The duplicate check must not fire against the row being edited."""
    response = signed_in.post(
        reverse("app:entity_edit", args=[entity_a.pk]),
        _payload(entity_a, registered_office_address="12 MG Road"),
        headers=HTMX,
    )

    assert response.status_code == 200
    entity_a.refresh_from_db()
    assert entity_a.registered_office_address == "12 MG Road"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def test_an_edit_is_audited_with_before_and_after(signed_in: Client, entity_a: Entity) -> None:
    """What a business shows a regulator: who changed what, and when."""
    signed_in.post(
        reverse("app:entity_edit", args=[entity_a.pk]),
        _payload(entity_a, name="Acme Textiles Limited", registered_office_state="IN-MH"),
        headers=HTMX,
    )

    with platform_scope(reason="test"):
        entry = (
            AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(entity_a.pk))
            .order_by("-occurred_at")
            .first()
        )

    assert entry is not None, "no audit row was written for the edit"
    assert entry.before["name"] == "Acme Textiles Pvt Ltd"
    assert entry.after["name"] == "Acme Textiles Limited"
    assert entry.after["registered_office_state"] == "IN-MH"
    assert entry.actor_id is not None


def test_an_edit_that_changes_nothing_records_nothing_changed(
    signed_in: Client, entity_a: Entity
) -> None:
    """``diff_fields`` reports only what moved, so a no-op edit says so."""
    signed_in.post(reverse("app:entity_edit", args=[entity_a.pk]), _payload(entity_a), headers=HTMX)

    with platform_scope(reason="test"):
        entry = (
            AuditLog.objects.filter(action=AuditAction.UPDATE, object_id=str(entity_a.pk))
            .order_by("-occurred_at")
            .first()
        )

    assert entry is not None
    assert entry.before == {}
    assert entry.after == {}


# ---------------------------------------------------------------------------
# Who may edit what
# ---------------------------------------------------------------------------


def test_another_tenants_entity_is_not_editable(
    client: Client, rival_owner: User, entity_a: Entity
) -> None:
    """The hostile client: act as tenant B, ask for tenant A's URL, get a 404.

    404 rather than 403 — confirming that an entity exists in another tenant is
    itself a disclosure.
    """
    rival = sign_in(client, rival_owner)
    url = reverse("app:entity_edit", args=[entity_a.pk])

    assert rival.get(url, headers=HTMX).status_code == 404
    assert (
        rival.post(url, _payload(entity_a, name="Taken Over Pvt Ltd"), headers=HTMX).status_code
        == 404
    )

    with platform_scope(reason="test"):
        entity_a.refresh_from_db()
    assert entity_a.name == "Acme Textiles Pvt Ltd"


def test_an_archived_entity_is_not_editable(signed_in: Client, entity_a: Entity) -> None:
    """Archived is history, and history is not retyped."""
    with platform_scope(reason="test"):
        entity_a.archive(reason="test")

    response = signed_in.get(reverse("app:entity_edit", args=[entity_a.pk]), headers=HTMX)

    assert response.status_code == 404
