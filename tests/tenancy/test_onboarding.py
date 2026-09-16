"""
The workspace's own sense of where it is: no entity, an unfinished one, or a
tracking one — and what the dashboard and the guided setup flow do about each.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope, tenant_context
from stacos.tenancy.models import Entity, Tenant
from stacos.tenancy.onboarding import Stage, setup_step_url, workspace_state
from stacos.tenancy.services import PROVISIONAL_NAME_KEY, name_is_provisional, rename_tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# workspace_state — the pure logic
# ---------------------------------------------------------------------------


def test_a_tenant_with_no_entity_is_add_entity_stage(org: Tenant) -> None:
    with tenant_context(tenant_ids={org.id}, reason="test"):
        state = workspace_state(org)
    assert state.stage is Stage.ADD_ENTITY
    assert state.is_first_run
    assert state.entity_count == 0
    assert state.resume_url == reverse("app:entity_create")


def test_an_entity_with_no_calendar_is_finish_setup_stage(entity_a: Entity) -> None:
    with tenant_context(tenant_ids={entity_a.tenant_id}, reason="test"):
        state = workspace_state(entity_a.tenant)
    assert state.stage is Stage.FINISH_SETUP
    assert state.is_first_run
    assert state.entity_count == 1
    assert state.resume_entity.id == entity_a.id
    assert state.resume_url == reverse("app:entity_setup_registrations", args=[entity_a.pk])


def test_an_entity_with_a_calendar_is_the_tracking_stage(materialised: Entity) -> None:
    with tenant_context(tenant_ids={materialised.tenant_id}, reason="test"):
        state = workspace_state(materialised.tenant)
    assert state.stage is Stage.TRACK
    assert not state.is_first_run


def test_one_tracked_entity_is_enough_even_with_a_second_unfinished_one(
    materialised: Entity, org: Tenant
) -> None:
    """A workspace only needs *one* live calendar to stop being first-run —
    the ordinary dashboard already handles "some entities have more to do
    than others" (that is what the entity filter and the per-entity overdue
    count are for).
    """
    with platform_scope(reason="test"):
        Entity.objects.create(
            tenant=org,
            name="Unfinished Co",
            entity_type="PVT_LTD",
            country="IN",
            registered_office_state="IN-KA",
        )
    with tenant_context(tenant_ids={org.id}, reason="test"):
        state = workspace_state(org)
    assert state.stage is Stage.TRACK
    assert not state.is_first_run


def test_resume_url_follows_the_entitys_own_furthest_step(entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        entity_a.setup_step = Entity.SetupStep.ANSWERS
        entity_a.save(update_fields=["setup_step"])

    assert setup_step_url(entity_a) == reverse("app:entity_setup_answers", args=[entity_a.pk])
    with tenant_context(tenant_ids={entity_a.tenant_id}, reason="test"):
        state = workspace_state(entity_a.tenant)
    assert state.resume_step_number == 2
    assert state.setup_step_total == 4


def test_a_workspace_with_no_tenant_at_all_is_first_run(platform: object) -> None:
    """A defensive branch: nothing in the product reaches the dashboard with
    ``request.tenant`` unset, but ``OrganisationGateMiddleware`` is what
    actually enforces that — this is the fallback if it ever does not.

    Uses the ``platform`` fixture (a held platform scope) rather than
    ``tenant_context``, since there is no tenant here to scope to — the whole
    point of the test is that nothing crashes for a request with none bound.
    """
    state = workspace_state(None)
    assert state.stage is Stage.ADD_ENTITY
    assert state.entity_count == 0


# ---------------------------------------------------------------------------
# The dashboard, over HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def signed_in(client: Client, org_owner: User) -> Client:
    return sign_in(client, org_owner)


def test_no_entity_renders_the_add_entity_first_run_state(signed_in: Client, org: Tenant) -> None:
    response = signed_in.get(reverse("app:dashboard"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Add your first entity" in body
    assert reverse("app:entity_create") in body
    # None of the ordinary dashboard's aggregate panels render for a register
    # that does not exist yet.
    assert "Compliance health" not in body


def test_an_unfinished_entity_renders_the_finish_setup_first_run_state(
    signed_in: Client, entity_a: Entity
) -> None:
    response = signed_in.get(reverse("app:dashboard"))
    assert response.status_code == 200
    body = response.content.decode()
    assert entity_a.name in body
    assert reverse("app:entity_setup_registrations", args=[entity_a.pk]) in body


def test_the_first_run_dashboard_renders_as_a_page_and_a_fragment(
    signed_in: Client, org: Tenant
) -> None:
    page = signed_in.get(reverse("app:dashboard"))
    fragment = signed_in.get(reverse("app:dashboard"), headers={"HX-Request": "true"})

    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b"Add your first entity" in fragment.content


def test_the_first_run_dashboard_costs_a_bounded_number_of_queries(
    signed_in: Client, org: Tenant, django_assert_max_num_queries: object
) -> None:
    """The whole point of a dedicated first-run branch: none of the six
    aggregate breakdowns the ordinary dashboard computes should run for a
    register that provably does not exist yet.
    """
    signed_in.get(reverse("app:dashboard"))

    with django_assert_max_num_queries(10):  # type: ignore[operator]
        response = signed_in.get(reverse("app:dashboard"))
    assert response.status_code == 200


def test_a_provisional_workspace_name_carries_no_nudge(signed_in: Client, org: Tenant) -> None:
    with platform_scope(reason="test"):
        org.settings = {PROVISIONAL_NAME_KEY: True}
        org.save(update_fields=["settings"])

    response = signed_in.get(reverse("app:dashboard"))
    assert "Name your organisation" not in response.content.decode()


def test_a_deliberately_named_workspace_carries_no_nudge(signed_in: Client, org: Tenant) -> None:
    response = signed_in.get(reverse("app:dashboard"))
    assert "Name your organisation" not in response.content.decode()


# ---------------------------------------------------------------------------
# Renaming
# ---------------------------------------------------------------------------


def test_renaming_clears_the_provisional_flag_and_audits(org: Tenant, org_owner: User) -> None:
    with platform_scope(reason="test"):
        org.settings = {PROVISIONAL_NAME_KEY: True}
        org.save(update_fields=["settings"])

        rename_tenant(org, "Asha's Textiles", actor=org_owner)
        org.refresh_from_db()

    assert org.name == "Asha's Textiles"
    assert not name_is_provisional(org)


def test_renaming_over_http_updates_the_tenant(
    client: Client, org_owner: User, org: Tenant
) -> None:
    with platform_scope(reason="test"):
        org.settings = {PROVISIONAL_NAME_KEY: True}
        org.save(update_fields=["settings"])

    signed_in = sign_in(client, org_owner, step_up=True)
    response = signed_in.post(
        reverse("app:workspace_rename"),
        {"name": "Founder Textiles Pvt Ltd"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    with platform_scope(reason="test"):
        org.refresh_from_db()
        assert org.name == "Founder Textiles Pvt Ltd"
        assert not name_is_provisional(org)


def test_an_empty_name_is_rejected(client: Client, org_owner: User, org: Tenant) -> None:
    signed_in = sign_in(client, org_owner, step_up=True)
    response = signed_in.post(
        reverse("app:workspace_rename"),
        {"name": "   "},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    with platform_scope(reason="test"):
        org.refresh_from_db()
        assert org.name != ""


# ---------------------------------------------------------------------------
# The guided setup flow persists and resumes progress
# ---------------------------------------------------------------------------


def test_visiting_a_step_records_it_as_the_furthest_reached(
    signed_in: Client, entity_a: Entity
) -> None:
    signed_in.get(reverse("app:entity_setup_answers", args=[entity_a.pk]))

    with platform_scope(reason="test"):
        entity_a.refresh_from_db()
        assert entity_a.setup_step == Entity.SetupStep.ANSWERS


def test_revisiting_an_earlier_step_does_not_erase_progress(
    signed_in: Client, entity_a: Entity
) -> None:
    signed_in.get(reverse("app:entity_setup_answers", args=[entity_a.pk]))
    signed_in.get(reverse("app:entity_setup_registrations", args=[entity_a.pk]))

    with platform_scope(reason="test"):
        entity_a.refresh_from_db()
        assert entity_a.setup_step == Entity.SetupStep.ANSWERS


def test_entity_detail_resumes_at_the_furthest_step_not_always_the_first(
    signed_in: Client, entity_a: Entity
) -> None:
    with platform_scope(reason="test"):
        entity_a.setup_step = Entity.SetupStep.ANSWERS
        entity_a.save(update_fields=["setup_step"])

    response = signed_in.get(
        reverse("app:entity_detail", args=[entity_a.pk]), headers={"HX-Request": "true"}
    )

    assert response.status_code == 204
    assert reverse("app:entity_setup_answers", args=[entity_a.pk]) in response["HX-Redirect"]
