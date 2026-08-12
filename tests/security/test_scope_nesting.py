"""
Scopes nest, and the inner one must not disturb the outer.

Regression suite for a bug found while wiring billing to the channel programme:
issuing an invoice runs inside the *client's* scope and has to write a commission
row into the *dealer's*, so it opens a nested platform scope. The inner exit was
clearing PostgreSQL's Row-Level Security settings unconditionally rather than
restoring what the outer scope had — and RLS fails closed, so the symptom was a
query in the *caller* silently returning nothing.

Nothing raised. No error appeared. The invoice was simply missing its commission,
and the next query in the enclosing block came back empty. That is the worst
failure mode a tenancy layer has, which is why these are their own file.
"""

from __future__ import annotations

import pytest
from django.db import connection

from stacos.core.rls import BYPASS_GUC, TENANT_GUC
from stacos.core.scope import current_scope, platform_scope, tenant_context
from stacos.tenancy.models import Entity, Tenant

pytestmark = [pytest.mark.django_db, pytest.mark.isolation]


def _guc(name: str) -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT coalesce(current_setting(%s, true), '')", [name])
        return cursor.fetchone()[0]


def test_a_nested_platform_scope_restores_the_outer_tenant_scope(
    org: Tenant, entity_a: Entity, rival_entity: Entity
) -> None:
    """The bug, stated as a test.

    After an inner platform scope closes, the outer tenant scope must still be
    able to read its own rows — and must still not be able to read anybody
    else's.
    """
    with tenant_context(tenant_ids=org.id, reason="test:outer"):
        before = set(Entity.objects.values_list("id", flat=True))
        assert entity_a.pk in before

        with platform_scope(reason="test:inner"):
            assert Entity.objects.filter(pk=rival_entity.pk).exists()

        after = set(Entity.objects.values_list("id", flat=True))

    assert after == before, "the outer scope lost its rows when the inner scope closed"
    assert rival_entity.pk not in after, "the inner platform scope leaked into the outer one"


def test_a_nested_platform_scope_restores_the_database_settings(org: Tenant) -> None:
    """Asserted at the GUC level, because that is where the bug lived.

    The ORM guard is restored by ``contextvars`` and was never the problem. What
    broke was PostgreSQL's own settings, which no amount of Python-level
    correctness would have caught.
    """
    with tenant_context(tenant_ids=org.id, reason="test:outer"):
        tenants_before = _guc(TENANT_GUC)
        assert str(org.id) in tenants_before

        with platform_scope(reason="test:inner"):
            assert _guc(BYPASS_GUC) == "on"

        assert _guc(BYPASS_GUC) != "on", "the bypass was left on after the inner scope closed"
        assert _guc(TENANT_GUC) == tenants_before, "the tenant list was cleared, not restored"


def test_a_platform_scope_inside_a_platform_scope_stays_open(
    org: Tenant, rival_entity: Entity
) -> None:
    """Two platform scopes nest without the inner one closing the outer."""
    with platform_scope(reason="test:outer"):
        with platform_scope(reason="test:inner"):
            pass
        assert Entity.objects.filter(pk=rival_entity.pk).exists()
        assert _guc(BYPASS_GUC) == "on"


def test_a_nested_tenant_scope_restores_the_outer_one(
    org: Tenant, other_org: Tenant, entity_a: Entity, rival_entity: Entity
) -> None:
    with tenant_context(tenant_ids=org.id, reason="test:outer"):
        with tenant_context(tenant_ids=other_org.id, reason="test:inner"):
            assert Entity.objects.filter(pk=rival_entity.pk).exists()
            assert not Entity.objects.filter(pk=entity_a.pk).exists()

        assert Entity.objects.filter(pk=entity_a.pk).exists()
        assert not Entity.objects.filter(pk=rival_entity.pk).exists()


def test_everything_is_released_at_the_outermost_exit(org: Tenant) -> None:
    """Restoring inner scopes must not leave the outermost one bound."""
    with tenant_context(tenant_ids=org.id, reason="test:outer"), platform_scope(reason="inner"):
        pass

    assert current_scope() is None
    assert _guc(BYPASS_GUC) != "on"
    assert _guc(TENANT_GUC) == ""


def test_an_exception_still_restores_the_outer_scope(org: Tenant, entity_a: Entity) -> None:
    """A scope that unwinds through an error is the case that matters most."""
    with tenant_context(tenant_ids=org.id, reason="test:outer"):
        with pytest.raises(RuntimeError), platform_scope(reason="test:inner"):
            raise RuntimeError("something went wrong inside the nested scope")

        assert Entity.objects.filter(pk=entity_a.pk).exists()
        assert _guc(BYPASS_GUC) != "on"
