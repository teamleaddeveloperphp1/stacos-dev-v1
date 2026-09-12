"""Recording a premises against an entity."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.tenancy.models import Entity, EntityPremises
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(client: Client, org_owner: User) -> Client:
    return sign_in(client, org_owner, step_up=True)


def test_the_modal_renders(signed_in: Client, entity_a: Entity) -> None:
    response = signed_in.get(
        reverse("app:premises_create", args=[entity_a.pk]),
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert b"Add a premises" in response.content
    # The type picker is populated from the fact registry.
    assert b"FACTORY" in response.content


def test_recording_one_replaces_the_table_body(signed_in: Client, entity_a: Entity) -> None:
    response = signed_in.post(
        reverse("app:premises_create", args=[entity_a.pk]),
        {"name": "Surat Head Office", "type": "REGISTERED_OFFICE", "jurisdiction": "IN-GJ"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert b'id="premises-rows"' in response.content
    assert b"Surat Head Office" in response.content
    # The empty state is gone, because the whole body was re-rendered.
    assert b"No premises recorded" not in response.content

    with platform_scope(reason="test"):
        assert EntityPremises.objects.filter(entity=entity_a, name="Surat Head Office").exists()


def test_a_missing_name_keeps_the_modal_open(signed_in: Client, entity_a: Entity) -> None:
    """422 so HTMX re-renders the form rather than swapping the table."""
    response = signed_in.post(
        reverse("app:premises_create", args=[entity_a.pk]),
        {"name": "", "type": "FACTORY"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert b"Add a premises" in response.content


def test_another_tenants_entity_is_not_reachable(
    client: Client, org_owner: User, rival_entity: Entity
) -> None:
    """Acme's owner must not be able to attach a premises to Rival's entity."""
    signed_in = sign_in(client, org_owner, step_up=True)
    response = signed_in.post(
        reverse("app:premises_create", args=[rival_entity.pk]),
        {"name": "Somewhere", "type": "FACTORY"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 404

    with platform_scope(reason="test"):
        assert not EntityPremises.objects.filter(entity=rival_entity).exists()
