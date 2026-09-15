"""
Entity-type-based registration identifiers on the Add/Edit Entity screen.

The mapping (which identifier, at what requirement level, for which entity
type) is authored in ``catalog/packs/IN.yaml`` and loaded into
``JurisdictionPack.registration_types`` by ``manage.py loadpack`` — these
tests run against the live pack (``tests/conftest.py`` loads it once per
session) rather than a hand-built fixture, so a mistake in the YAML shows up
here rather than only in the running app.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.models import AuditLog
from stacos.core.scope import platform_scope
from stacos.tenancy.forms import registration_field_name
from stacos.tenancy.models import Entity, EntityRegistration, Membership, Role, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    """``org-owner`` holds both ``tenancy.registration.manage`` and
    ``tenancy.profile.edit`` — the combination that sees the full screen.

    ``step_up=True`` because ``.manage`` is sensitive: saving an identifier
    demands a fresh re-authentication, the same as the pre-existing
    standalone "Add Registration" modal already requires.
    """
    return sign_in(client, org_owner, step_up=True)


@pytest.fixture
def limited_user(org: Tenant) -> User:
    """A member who can edit entities but holds neither
    ``tenancy.registration.manage`` nor ``tenancy.profile.edit`` — there is no
    shipped system role shaped exactly this way, so this builds a tenant-local
    one, the same mechanism a real customer's custom role would use.
    """
    with platform_scope(reason="test-fixture"):
        role = Role.objects.create(
            tenant=org,
            code="entity-editor-only",
            name="Entity editor (no registrations or profile)",
            tenant_type=Tenant.Type.ORGANISATION,
            permissions=["tenancy.entity.view", "tenancy.entity.create", "tenancy.entity.edit"],
        )
        user = User.objects.create_user(
            email="editor@acme.example",
            password="test-password-12345",
            full_name="Priya Nair",
            phone_e164="+919800000099",
            email_verified=True,
            phone_verified=True,
        )
        Membership.objects.create(tenant=org, user=user, role=role, status=Membership.Status.ACTIVE)
        return user


def _fields_shown(body: str) -> set[str]:
    """Which ``reg_<CODE>`` inputs are present in a rendered form body."""
    import re

    return set(re.findall(r'name="(reg_[A-Za-z0-9_]+)"', body))


# ---------------------------------------------------------------------------
# The mapping drives what appears, in what order, and what is required
# ---------------------------------------------------------------------------


def test_pvt_ltd_shows_the_mapped_identifiers_with_cin_conditional(signed_in: Client) -> None:
    body = signed_in.get(
        reverse("app:entity_registration_fields"), {"entity_type": "PVT_LTD"}
    ).content.decode()

    shown = _fields_shown(body)
    assert shown == {
        registration_field_name(c)
        for c in ("CIN", "PAN", "TAN", "GST", "ESIC", "PF", "IEC", "UDYAM")
    }
    # CIN is conditional here, not mandatory — no asterisk / required attribute.
    assert 'id="id_reg_CIN"' in body
    assert 'name="reg_CIN" maxlength="64" required' not in body


def test_public_ltd_makes_cin_mandatory_and_pins_it_first(signed_in: Client) -> None:
    body = signed_in.get(
        reverse("app:entity_registration_fields"), {"entity_type": "PUBLIC_LTD"}
    ).content.decode()

    assert "required" in body.split('id="id_reg_CIN"')[0][-200:], (
        "CIN must carry the same required marker the server also enforces"
    )
    # CIN first, PAN immediately after — the one explicit ordering override.
    assert body.index("reg_CIN") < body.index("reg_PAN") < body.index("reg_TAN")


def test_llp_shows_llpin_instead_of_cin(signed_in: Client) -> None:
    body = signed_in.get(
        reverse("app:entity_registration_fields"), {"entity_type": "LLP"}
    ).content.decode()

    shown = _fields_shown(body)
    assert "reg_CIN" not in shown, "CIN does not apply to an LLP"
    assert "reg_LLPIN" in shown


def test_trust_shows_12ab_80g_darpan_and_no_iec_or_udyam(signed_in: Client) -> None:
    body = signed_in.get(
        reverse("app:entity_registration_fields"), {"entity_type": "TRUST"}
    ).content.decode()

    shown = _fields_shown(body)
    assert {"reg_12AB", "reg_80G", "reg_DARPAN", "reg_TRUST_REGN"} <= shown
    assert "reg_IEC" not in shown
    assert "reg_UDYAM" not in shown


def test_huf_shows_only_karta_pan_as_a_registration_number(signed_in: Client) -> None:
    body = signed_in.get(
        reverse("app:entity_registration_fields"), {"entity_type": "HUF"}
    ).content.decode()

    shown = _fields_shown(body)
    assert shown == {
        registration_field_name(c) for c in ("PAN", "TAN", "GST", "ESIC", "PF", "KARTA_PAN")
    }


def test_no_entity_type_shows_no_identifier_fields(signed_in: Client) -> None:
    body = signed_in.get(
        reverse("app:entity_registration_fields"), {"entity_type": ""}
    ).content.decode()
    assert _fields_shown(body) == set()


# ---------------------------------------------------------------------------
# Submitting the Add/Edit Entity form
# ---------------------------------------------------------------------------


def _base_payload(**overrides: str) -> dict[str, str]:
    """A complete entity form. Every field on it is mandatory — and so is PAN,
    for every entity type — so a payload that leaves those blank would fail on
    them rather than on the identifier field each test here is actually about.
    """
    data = {
        "name": "Test Co",
        "reg_PAN": "AAACE1234F",
        "legal_name": "Test Co Private Limited",
        "incorporation_date": "2018-07-02",
        "registered_office_state": "IN-GJ",
        "registered_office_address": "22 Ring Road, Surat",
        "aggregate_turnover": "1000000.00",
        "employee_count": "5",
    }
    data.update(overrides)
    return data


def test_missing_mandatory_identifier_is_a_field_error_not_a_silent_save(signed_in: Client) -> None:
    """Public Ltd requires a CIN. Leaving it out must reject, not save blank."""
    response = signed_in.post(
        reverse("app:entity_create"),
        _base_payload(entity_type="PUBLIC_LTD"),
        headers=HTMX,
    )

    assert response.status_code == 422
    assert "id_reg_CIN_error" in response.content.decode()
    with platform_scope(reason="test"):
        assert not Entity.objects.filter(name="Test Co").exists(), (
            "an invalid submission must not save"
        )


def test_invalid_format_is_field_specific_and_not_stale(signed_in: Client) -> None:
    """A bad PAN is rejected with a current message; fixing it clears that message."""
    bad = signed_in.post(
        reverse("app:entity_create"),
        _base_payload(entity_type="PVT_LTD", reg_PAN="not-a-pan"),
        headers=HTMX,
    )
    assert bad.status_code == 422
    body = bad.content.decode()
    assert "id_reg_PAN_error" in body
    assert "A PAN is ten characters" in body

    good = signed_in.post(
        reverse("app:entity_create"),
        _base_payload(entity_type="PVT_LTD", reg_PAN="AAACE1234F"),
        headers=HTMX,
    )
    assert good.status_code == 200
    with platform_scope(reason="test"):
        entity = Entity.objects.get(name="Test Co")
        registration = entity.registrations.get(type="PAN", jurisdiction="")
        assert registration.value == "AAACE1234F"


def test_creating_an_entity_saves_registrations_and_profile(signed_in: Client) -> None:
    response = signed_in.post(
        reverse("app:entity_create"),
        _base_payload(
            entity_type="LLP",
            reg_LLPIN="AAB-1234",
            aggregate_turnover="2500000.00",
            employee_count="12",
        ),
        headers=HTMX,
    )
    assert response.status_code == 200

    with platform_scope(reason="test"):
        entity = Entity.objects.get(name="Test Co")
        assert entity.profile.aggregate_turnover == 2500000
        assert entity.profile.employee_count == 12
        registration = entity.registrations.get(type="LLPIN", jurisdiction="")
        assert registration.value == "AAB-1234"

        assert AuditLog.objects.filter(
            object_type="tenancy.EntityRegistration", object_id=str(registration.pk)
        ).exists()


# ---------------------------------------------------------------------------
# Editing: pre-fill, clearing, and type changes
# ---------------------------------------------------------------------------


@pytest.fixture
def pvt_ltd_entity(org: Tenant) -> Entity:
    from datetime import date

    from stacos.tenancy.models import EntityProfile

    with platform_scope(reason="test-fixture"):
        entity = Entity.objects.create(
            tenant=org,
            name="Fixture Pvt Ltd",
            entity_type="PVT_LTD",
            country="IN",
            incorporation_date=date(2018, 1, 1),
            registered_office_state="IN-KA",
        )
        EntityProfile.objects.create(
            tenant=org, entity=entity, aggregate_turnover=1000, employee_count=3
        )
        EntityRegistration.objects.create(
            tenant=org, entity=entity, type="PAN", jurisdiction="", value="AAACE1234F"
        )
        return entity


def test_editing_prefills_the_existing_value(signed_in: Client, pvt_ltd_entity: Entity) -> None:
    body = signed_in.get(
        reverse("app:entity_edit", args=[pvt_ltd_entity.pk]), headers=HTMX
    ).content.decode()

    assert 'value="AAACE1234F"' in body


def test_clearing_a_field_archives_rather_than_deletes(
    signed_in: Client, pvt_ltd_entity: Entity
) -> None:
    """TAN rather than PAN, deliberately: PAN is mandatory for every entity
    type, so clearing it is a validation error and could never reach the
    archiving path this test is about. TAN is the nearest optional identifier.
    """
    with platform_scope(reason="test"):
        EntityRegistration.objects.create(
            tenant=pvt_ltd_entity.tenant,
            entity=pvt_ltd_entity,
            type="TAN",
            jurisdiction="",
            value="ABCD12345E",
        )

    response = signed_in.post(
        reverse("app:entity_edit", args=[pvt_ltd_entity.pk]),
        _base_payload(
            name=pvt_ltd_entity.name,
            entity_type="PVT_LTD",
            registered_office_state="IN-KA",
            incorporation_date="2018-01-01",
            reg_TAN="",
        ),
        headers=HTMX,
    )
    assert response.status_code == 200, response.content[:600]

    with platform_scope(reason="test"):
        registration = EntityRegistration.objects.get(entity=pvt_ltd_entity, type="TAN")
        assert registration.archived_at is not None, "cleared, not deleted"
        assert EntityRegistration.objects.filter(pk=registration.pk).exists()


def test_changing_entity_type_hides_but_does_not_delete_an_inapplicable_registration(
    signed_in: Client, pvt_ltd_entity: Entity
) -> None:
    """Switch to HUF (no PAN slot distinct from the entity's own — wait, PAN
    still applies to HUF; use a code that genuinely disappears instead: give
    the fixture a CIN, then switch to HUF, where CIN does not apply at all.
    """
    with platform_scope(reason="test"):
        EntityRegistration.objects.create(
            tenant=pvt_ltd_entity.tenant,
            entity=pvt_ltd_entity,
            type="CIN",
            jurisdiction="",
            value="U72200KA2015PTC012345",
        )

    response = signed_in.post(
        reverse("app:entity_edit", args=[pvt_ltd_entity.pk]),
        _base_payload(
            name=pvt_ltd_entity.name,
            entity_type="HUF",
            registered_office_state="IN-KA",
            incorporation_date="2018-01-01",
            reg_PAN="AAACE1234F",
            reg_KARTA_PAN="AAACE1234F",
        ),
        headers=HTMX,
    )
    assert response.status_code == 200

    with platform_scope(reason="test"):
        cin = EntityRegistration.objects.get(entity=pvt_ltd_entity, type="CIN")
        assert cin.archived_at is None, "an inapplicable field is hidden, never silently cleared"


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_hostile_client_cannot_probe_another_tenants_registrations(
    client: Client, rival_owner: User, pvt_ltd_entity: Entity
) -> None:
    rival = sign_in(client, rival_owner)
    response = rival.get(
        reverse("app:entity_registration_fields"),
        {"entity_type": "PVT_LTD", "entity": str(pvt_ltd_entity.pk)},
    )
    assert response.status_code == 404
    assert b"AAACE1234F" not in response.content


def test_without_the_permission_the_screen_behaves_as_before(
    limited_user: User, client: Client
) -> None:
    """No `tenancy.registration.manage` or `tenancy.profile.edit`: no
    identifier or turnover/employee fields appear or are required — the
    screen works exactly as it did before this feature existed.
    """
    signed_in = sign_in(client, limited_user)

    body = signed_in.get(reverse("app:entity_create"), headers=HTMX).content.decode()
    assert _fields_shown(body) == set()
    assert "aggregate_turnover" not in body
    assert "employee_count" not in body

    response = signed_in.post(
        reverse("app:entity_create"),
        {
            "name": "Legacy Flow Co",
            "legal_name": "Legacy Flow Company Limited",
            "entity_type": "PUBLIC_LTD",  # would need a mandatory CIN, if shown
            "incorporation_date": "2018-07-02",
            "registered_office_state": "IN-GJ",
            "registered_office_address": "22 Ring Road, Surat",
        },
        headers=HTMX,
    )
    assert response.status_code == 200
    with platform_scope(reason="test"):
        assert Entity.objects.filter(name="Legacy Flow Co").exists()
