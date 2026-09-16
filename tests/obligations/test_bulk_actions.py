"""
Bulk actions on the calendar: assign several obligations at once, or mark
several Not Applicable with one shared reason.

Neither bulk view introduces a new authorization model — both call exactly the
same ``stacos.obligations.transitions.assign``/``apply_transition`` the
single-obligation flow uses, looped per row (see the module docstring on
``apply_transition``: "so the API, the web app and a future bulk action
cannot drift apart"). What is genuinely new, and what these tests are for, is
the looping behaviour itself: an id outside the caller's reach must be
dropped rather than leaked or crashed on, and one obligation's failure must
not roll back the others.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from stacos.core.scope import platform_scope
from stacos.engine.lifecycle import State
from stacos.obligations.models import ObligationInstance
from stacos.obligations.services import materialise
from stacos.tenancy.models import Entity, EntityRegistration, Tenant
from tests.obligations.test_views import signed_in  # noqa: F401

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 8, 12)


@pytest.fixture
def two_obligations(materialised: Entity) -> list[ObligationInstance]:
    with platform_scope(reason="test-fixture"):
        return list(
            ObligationInstance.objects.filter(
                entity=materialised, state=State.NOT_STARTED
            ).order_by("due_date")[:2]
        )


@pytest.fixture
def an_obligation_in_another_tenant(rival_entity: Entity) -> ObligationInstance:
    """A hostile client's payload: an id that belongs to somebody else entirely."""
    with platform_scope(reason="test-fixture"):
        EntityRegistration.objects.create(
            tenant=rival_entity.tenant, entity=rival_entity, type="PAN", value="AAACR1234C"
        )
        materialise(rival_entity, as_of=AS_OF, trigger="ONBOARDING")
        return (
            ObligationInstance.objects.filter(entity=rival_entity, state=State.NOT_STARTED)
            .order_by("due_date")
            .first()
        )


# ===========================================================================
# Bulk assign
# ===========================================================================


def test_bulk_assign_modal_lists_the_selected_obligations(
    signed_in: Client, two_obligations: list[ObligationInstance]
) -> None:
    response = signed_in.get(
        reverse("compliance:bulk_assign"),
        {"ids": [str(o.pk) for o in two_obligations]},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    body = response.content.decode()
    # The modal strips the same static clarifier the calendar row does (see
    # `strip_trailing_paren`), so it names the entity rather than the raw
    # title — both obligations here share one, so that alone is unambiguous.
    assert two_obligations[0].entity.name in body
    assert body.count(two_obligations[0].entity.name) >= 2


def test_bulk_assign_updates_every_selected_obligation(
    signed_in: Client, two_obligations: list[ObligationInstance], org: Tenant
) -> None:
    from tests.conftest import _make_member

    colleague = _make_member(
        org, "ramesh@acme.example", "Ramesh Patel", "+919800000099", "org-owner"
    )

    response = signed_in.post(
        reverse("compliance:bulk_assign"),
        {"ids": [str(o.pk) for o in two_obligations], "assigned_to": colleague.pk},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    body = response.content.decode()
    for obligation in two_obligations:
        assert f"obligation-{obligation.pk}" in body
    assert "calendar-counts" in body
    assert "HX-Trigger-After-Swap" in response.headers
    assert "stacos:modal-close" in response.headers["HX-Trigger-After-Swap"]

    with platform_scope(reason="test"):
        for obligation in two_obligations:
            obligation.refresh_from_db()
            assert obligation.assigned_to_id == colleague.id
            assert obligation.events.filter(kind="ASSIGNED").exists()


def test_bulk_assign_drops_ids_outside_the_callers_scope(
    signed_in: Client,
    two_obligations: list[ObligationInstance],
    org: Tenant,
    an_obligation_in_another_tenant: ObligationInstance,
) -> None:
    """A hostile client mixes a rival tenant's obligation id into the batch."""
    from tests.conftest import _make_member

    colleague = _make_member(
        org, "ramesh@acme.example", "Ramesh Patel", "+919800000099", "org-owner"
    )
    ids = [str(o.pk) for o in two_obligations] + [str(an_obligation_in_another_tenant.pk)]

    response = signed_in.post(
        reverse("compliance:bulk_assign"),
        {"ids": ids, "assigned_to": colleague.pk},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert f"obligation-{an_obligation_in_another_tenant.pk}".encode() not in response.content

    with platform_scope(reason="test"):
        an_obligation_in_another_tenant.refresh_from_db()
        assert an_obligation_in_another_tenant.assigned_to_id is None
        for obligation in two_obligations:
            obligation.refresh_from_db()
            assert obligation.assigned_to_id == colleague.id


# ===========================================================================
# Bulk Not Applicable
# ===========================================================================


def test_bulk_not_applicable_requires_a_reason(
    signed_in: Client, two_obligations: list[ObligationInstance]
) -> None:
    response = signed_in.post(
        reverse("compliance:bulk_not_applicable"),
        {"ids": [str(o.pk) for o in two_obligations], "reason": ""},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 422
    with platform_scope(reason="test"):
        for obligation in two_obligations:
            obligation.refresh_from_db()
            assert obligation.state != State.NOT_APPLICABLE


def test_bulk_not_applicable_marks_every_selected_obligation(
    signed_in: Client, two_obligations: list[ObligationInstance]
) -> None:
    response = signed_in.post(
        reverse("compliance:bulk_not_applicable"),
        {
            "ids": [str(o.pk) for o in two_obligations],
            "reason": "Entity deregistered from this scheme.",
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    with platform_scope(reason="test"):
        for obligation in two_obligations:
            obligation.refresh_from_db()
            assert obligation.state == State.NOT_APPLICABLE
            event = obligation.events.filter(kind="TRANSITION").latest("created_at")
            assert event.note == "Entity deregistered from this scheme."


def test_bulk_not_applicable_skips_a_row_that_cannot_make_the_move(
    signed_in: Client, two_obligations: list[ObligationInstance]
) -> None:
    """One obligation is already closed; the other must still go through."""
    already_closed, still_open = two_obligations
    with platform_scope(reason="test"):
        already_closed.state = State.NOT_APPLICABLE
        already_closed.save(update_fields=["state"])

    response = signed_in.post(
        reverse("compliance:bulk_not_applicable"),
        {
            "ids": [str(already_closed.pk), str(still_open.pk)],
            "reason": "Entity deregistered from this scheme.",
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    with platform_scope(reason="test"):
        still_open.refresh_from_db()
        assert still_open.state == State.NOT_APPLICABLE


def test_bulk_not_applicable_drops_ids_outside_the_callers_scope(
    signed_in: Client,
    two_obligations: list[ObligationInstance],
    an_obligation_in_another_tenant: ObligationInstance,
) -> None:
    response = signed_in.post(
        reverse("compliance:bulk_not_applicable"),
        {
            "ids": [str(o.pk) for o in two_obligations]
            + [str(an_obligation_in_another_tenant.pk)],
            "reason": "Entity deregistered from this scheme.",
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    with platform_scope(reason="test"):
        an_obligation_in_another_tenant.refresh_from_db()
        assert an_obligation_in_another_tenant.state != State.NOT_APPLICABLE
