"""
Acting as one tenant, trying to reach another's calendar.

The register is the most sensitive table in the product: it says what a business
owes, what it has failed to file, and what it is disputing with a regulator.
Every one of these attempts must fail, and the ones that go through HTTP must
fail as a 404 rather than a 403 — confirming that an obligation *exists* in
another tenant is itself a disclosure.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.exceptions import CrossTenantWriteError, UnscopedQueryError
from stacos.core.scope import platform_scope, tenant_context
from stacos.engine.lifecycle import State
from stacos.obligations.models import (
    EntityEvent,
    MaterialisationRun,
    ObligationEvent,
    ObligationInstance,
    ObligationSuppression,
)
from stacos.obligations.queries import live, status_counts
from stacos.obligations.services import materialise
from stacos.tenancy.models import Entity, Tenant

pytestmark = [pytest.mark.django_db, pytest.mark.isolation]

AS_OF = date(2026, 8, 12)

SCOPED_MODELS = (
    ObligationInstance,
    ObligationEvent,
    ObligationSuppression,
    EntityEvent,
    MaterialisationRun,
)


@pytest.fixture
def rival_calendar(rival_entity: Entity) -> Entity:
    """A fully materialised calendar belonging to somebody else entirely."""
    with platform_scope(reason="test-fixture"):
        materialise(rival_entity, as_of=AS_OF, trigger="ONBOARDING")
    return rival_entity


# ===========================================================================
# The manager
# ===========================================================================


@pytest.mark.parametrize("model", SCOPED_MODELS, ids=lambda m: m._meta.label)
def test_register_models_refuse_to_query_unscoped(model: type) -> None:
    with pytest.raises(UnscopedQueryError):
        model.objects.count()


def test_one_tenant_cannot_read_anothers_obligations(
    org: Tenant, materialised: Entity, rival_calendar: Entity
) -> None:
    with tenant_context(tenant_ids=org.id, reason="test"):
        visible = {row.entity_id for row in ObligationInstance.objects.all()}

    assert materialised.pk in visible
    assert rival_calendar.pk not in visible, "cross-tenant read of the obligation register"


def test_counts_do_not_leak_across_tenants(
    org: Tenant, materialised: Entity, rival_calendar: Entity
) -> None:
    """A count is a disclosure too.

    "Rival Industries has 47 overdue filings" is commercially useful information
    and must not be reachable, even in aggregate.
    """
    with platform_scope(reason="test"):
        everyone = ObligationInstance.objects.count()

    with tenant_context(tenant_ids=org.id, reason="test"):
        mine = status_counts(as_of=AS_OF)
        own_rows = live().filter(entity=materialised).count()

    assert mine["total"] < everyone
    assert mine["total"] == own_rows


def test_writing_an_obligation_for_another_tenant_is_refused(
    org: Tenant, rival_entity: Entity
) -> None:
    """The subtler half of a scoping bug.

    Reads are guarded by the manager; a hand-constructed instance carrying an
    attacker-supplied tenant id would otherwise write straight past it.
    """
    with tenant_context(tenant_ids=org.id, reason="test"), pytest.raises(CrossTenantWriteError):
        ObligationInstance(
            tenant=rival_entity.tenant,
            entity=rival_entity,
            definition_code="IN-GST-GSTR3B-MONTHLY",
            period_key="2026-07",
            title="Forged",
            category="TAX_INDIRECT",
        ).save()


def test_row_level_security_backs_the_manager_up(
    org: Tenant, materialised: Entity, rival_calendar: Entity
) -> None:
    """Raw SQL is the thing the ORM guard cannot see.

    RLS defends against a forgotten filter in hand-written SQL — the second line,
    not a replacement for the first.
    """
    from django.db import connection

    with tenant_context(tenant_ids=org.id, reason="test"), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM obligations_obligationinstance")
        visible = cursor.fetchone()[0]

    with platform_scope(reason="test"):
        total = ObligationInstance.objects.count()

    assert visible < total, "raw SQL saw rows from another tenant"


# ===========================================================================
# Over HTTP
# ===========================================================================


@pytest.fixture
def rival_signed_in(client: Client, rival_owner: User) -> Client:
    from stacos.accounts.middleware import SESSION_VERIFIED_KEY

    client.force_login(rival_owner)
    session = client.session
    session[SESSION_VERIFIED_KEY] = True
    session.save()
    return client


def test_another_tenants_obligation_is_a_404_not_a_403(
    rival_signed_in: Client, an_obligation: ObligationInstance
) -> None:
    """404, deliberately.

    A 403 confirms the row exists. For a compliance register that is a leak in
    itself — it tells a competitor that a given filing is on somebody's calendar.
    """
    response = rival_signed_in.get(reverse("compliance:detail", args=[an_obligation.pk]))
    assert response.status_code == 404


def test_another_tenants_obligation_cannot_be_transitioned(
    rival_signed_in: Client, an_obligation: ObligationInstance
) -> None:
    response = rival_signed_in.post(
        reverse("compliance:transition", args=[an_obligation.pk]),
        {"target": State.IN_PREPARATION},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 404

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
    assert an_obligation.state == State.NOT_STARTED


def test_another_tenants_calendar_cannot_be_rebuilt(
    rival_signed_in: Client, materialised: Entity
) -> None:
    response = rival_signed_in.post(
        reverse("compliance:rebuild", args=[materialised.pk]), headers={"HX-Request": "true"}
    )
    assert response.status_code == 404


def test_the_list_view_shows_only_the_signed_in_tenants_rows(
    rival_signed_in: Client, materialised: Entity, rival_calendar: Entity
) -> None:
    response = rival_signed_in.get(reverse("compliance:calendar"), {"status": "all"})
    assert response.status_code == 200
    assert materialised.name.encode() not in response.content
    assert rival_calendar.name.encode() in response.content


def test_filtering_by_another_tenants_entity_id_returns_nothing(
    rival_signed_in: Client, materialised: Entity
) -> None:
    """A forged query parameter narrows within the scope; it cannot widen it."""
    response = rival_signed_in.get(
        reverse("compliance:calendar"), {"entity": str(materialised.pk), "status": "all"}
    )
    assert response.status_code == 200
    assert materialised.name.encode() not in response.content


# ===========================================================================
# Engagements reach exactly as far as they were granted
# ===========================================================================


def test_a_practice_sees_only_the_entity_it_was_engaged_on(
    in_practice: object, entity_a: Entity, entity_b: Entity
) -> None:
    """An engagement grants named entities, never a whole organisation.

    A firm engaged for two of a group's eight subsidiaries must see those two and
    nothing else — which is the property that makes the practice side of the
    product sellable at all.
    """
    with platform_scope(reason="test-fixture"):
        materialise(entity_a, as_of=AS_OF, trigger="ONBOARDING")
        materialise(entity_b, as_of=AS_OF, trigger="ONBOARDING")

    visible = {row.entity_id for row in ObligationInstance.objects.all()}

    assert entity_a.pk in visible, "the engaged entity's calendar is not reachable"
    assert entity_b.pk not in visible, "an engagement leaked into a sibling entity"
