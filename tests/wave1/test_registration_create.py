"""Recording a registration against an entity."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.tenancy.models import Entity, EntityRegistration
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(client: Client, org_owner: User) -> Client:
    return sign_in(client, org_owner, step_up=True)


def test_the_modal_renders(signed_in: Client, entity_a: Entity) -> None:
    response = signed_in.get(
        reverse("app:registration_create", args=[entity_a.pk]),
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert b"Add a registration" in response.content
    # The type picker is populated from the fact registry.
    assert b"PAN" in response.content


def test_recording_one_replaces_the_table_body(signed_in: Client, entity_a: Entity) -> None:
    response = signed_in.post(
        reverse("app:registration_create", args=[entity_a.pk]),
        {"type": "PAN", "value": "AABCS1429B", "jurisdiction": "", "is_primary": "on"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert b'id="registration-rows"' in response.content
    assert b"AABCS1429B" in response.content
    # The empty state is gone, because the whole body was re-rendered.
    assert b"No registrations recorded" not in response.content

    with platform_scope(reason="test"):
        assert EntityRegistration.objects.filter(entity=entity_a, type="PAN").exists()


def test_an_invalid_number_keeps_the_modal_open(signed_in: Client, entity_a: Entity) -> None:
    """422 so HTMX re-renders the form rather than swapping the table."""
    response = signed_in.post(
        reverse("app:registration_create", args=[entity_a.pk]),
        {"type": "PAN", "value": "NOT-A-PAN"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert b"Add a registration" in response.content


def test_another_tenants_entity_is_not_reachable(
    client: Client, org_owner: User, rival_entity: Entity
) -> None:
    """Acme's owner must not be able to attach a registration to Rival's entity."""
    signed_in = sign_in(client, org_owner, step_up=True)
    response = signed_in.post(
        reverse("app:registration_create", args=[rival_entity.pk]),
        {"type": "PAN", "value": "AABCS1429B"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 404

    with platform_scope(reason="test"):
        assert not EntityRegistration.objects.filter(entity=rival_entity).exists()
