"""
The adversarial tenant-isolation suite.

This is the executable specification of the tenancy design. Its only job is to
act as one tenant and try to reach another's data — through the ORM, through the
write path, through raw SQL, and through an engagement that should not extend
that far. Every one of those attempts must fail.

Several tests are **parametrised over every scoped model in the project**. That
shape is what makes the suite durable: a developer adding a model in month eight
inherits these tests without writing one, and a model that forgets to declare its
tenancy fails the build rather than quietly leaking.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.db import ProgrammingError, connection, models

from stacos.core.exceptions import CrossTenantWriteError, UnscopedQueryError
from stacos.core.models import TenantScopedModel
from stacos.core.rls import iter_scoped_models
from stacos.core.scope import tenant_context
from stacos.engagements.models import Engagement
from stacos.tenancy.models import Entity, EntityProfile, Membership, Tenant

pytestmark = [pytest.mark.django_db, pytest.mark.isolation]


def scoped_models() -> list[type[models.Model]]:
    return sorted(iter_scoped_models(), key=lambda m: m._meta.label)


def _model_ids(model: type[models.Model]) -> str:
    return model._meta.label


# ===========================================================================
# 1. The manager refuses to run unscoped
# ===========================================================================


@pytest.mark.parametrize("model", scoped_models(), ids=_model_ids)
def test_default_manager_raises_without_a_bound_scope(model: type[models.Model]) -> None:
    """A query with no scope must raise, not return every row.

    This is the property the whole design rests on: forgetting to scope is a
    loud failure in development rather than a quiet leak in production.
    """
    with pytest.raises(UnscopedQueryError):
        model.objects.count()


@pytest.mark.parametrize("model", scoped_models(), ids=_model_ids)
def test_unscoped_manager_exists_and_is_separate(model: type[models.Model]) -> None:
    """The escape hatch is a *different*, deliberately ugly manager.

    If ``objects_unscoped`` were the same object as ``objects`` the guard would
    be decorative.
    """
    assert hasattr(model, "objects_unscoped")
    assert model.objects_unscoped is not model.objects


# ===========================================================================
# 2. Reads cannot cross a tenant boundary
# ===========================================================================


def test_org_cannot_see_another_orgs_entities(
    org: Tenant, entity_a: Entity, other_org: Tenant, rival_entity: Entity
) -> None:
    with tenant_context(tenant_ids=org.id, reason="test"):
        visible = set(Entity.objects.values_list("id", flat=True))

    assert entity_a.id in visible
    assert rival_entity.id not in visible


def test_org_cannot_fetch_another_orgs_entity_by_id(org: Tenant, rival_entity: Entity) -> None:
    """Knowing the primary key must not be enough.

    UUIDv7 keys are not guessable, but an id can leak through a log, a support
    ticket or a shared link — so the scope, not the obscurity, is the control.
    """
    with tenant_context(tenant_ids=org.id, reason="test"), pytest.raises(Entity.DoesNotExist):
        Entity.objects.get(pk=rival_entity.pk)


def test_org_cannot_see_another_orgs_profile(org: Tenant, rival_entity: Entity) -> None:
    with tenant_context(tenant_ids=org.id, reason="test"):
        assert EntityProfile.objects.filter(entity_id=rival_entity.id).count() == 0


def test_org_cannot_see_another_orgs_memberships(
    org: Tenant, org_owner: Any, other_org: Tenant, rival_owner: Any
) -> None:
    """Membership lists are their own disclosure — who works at a company, and
    at what seniority, is information a competitor should not be able to read."""
    with tenant_context(tenant_ids=org.id, reason="test"):
        emails = set(Membership.objects.values_list("user__email", flat=True))

    assert "owner@acme.example" in emails
    assert "owner@rival.example" not in emails


@pytest.mark.parametrize("model", scoped_models(), ids=_model_ids)
def test_empty_scope_returns_nothing_rather_than_everything(
    model: type[models.Model], entity_a: Entity
) -> None:
    """A scope with no tenants must fail closed.

    The tempting bug is to treat an empty tenant set as "no filter needed".
    """
    with tenant_context(tenant_ids=[], reason="test:empty"):
        assert model.objects.count() == 0


# ===========================================================================
# 3. Writes cannot cross a tenant boundary
# ===========================================================================


def test_cannot_save_a_row_for_a_tenant_outside_the_write_scope(
    org: Tenant, other_org: Tenant
) -> None:
    """The subtler half of a scoping bug.

    Reads are guarded by the manager, but a hand-constructed instance carrying an
    attacker-supplied tenant id would otherwise write straight past it.
    """
    with tenant_context(tenant_ids=org.id, reason="test"):
        rogue = Entity(
            tenant=other_org,
            name="Injected",
            entity_type="PVT_LTD",
            country="IN",
        )
        with pytest.raises(CrossTenantWriteError):
            rogue.save()


def test_cannot_reassign_an_existing_row_to_another_tenant(
    org: Tenant, other_org: Tenant, entity_a: Entity
) -> None:
    with tenant_context(tenant_ids=org.id, reason="test"):
        entity = Entity.objects.get(pk=entity_a.pk)
        entity.tenant = other_org
        with pytest.raises(CrossTenantWriteError):
            entity.save()


def test_read_only_engagement_cannot_write_to_the_client(
    in_practice: Any, entity_a: Entity
) -> None:
    """An engagement grants working access to a client's records, but the client
    tenant itself is never writable by the practice — it cannot create entities,
    add users, or change the plan."""
    entity = Entity.objects.get(pk=entity_a.pk)
    entity.name = "Renamed by the accountant"
    with pytest.raises(CrossTenantWriteError):
        entity.save()


# ===========================================================================
# 4. Engagements grant exactly what they say, and no more
# ===========================================================================


def test_practice_sees_only_the_engaged_entity(
    in_practice: Any, entity_a: Entity, entity_b: Entity
) -> None:
    """The central cross-tenant case.

    The firm is engaged on entity A. Entity B belongs to the *same client
    tenant* and must still be invisible — engagement scope is per entity, not
    per customer.
    """
    visible = set(Entity.objects.values_list("id", flat=True))

    assert entity_a.id in visible, "the engaged entity should be visible"
    assert entity_b.id not in visible, (
        "a sibling entity of the same client is NOT covered by this engagement"
    )


def test_practice_cannot_see_an_unrelated_clients_entity(
    in_practice: Any, rival_entity: Entity
) -> None:
    assert not Entity.objects.filter(pk=rival_entity.pk).exists()


def test_practice_cannot_read_the_clients_user_list(
    in_practice: Any, org: Tenant, org_owner: Any
) -> None:
    """Membership is tenant-level, not entity-level.

    An engagement reaches the client's *compliance records*, never its staff
    directory — the distinction that makes ``ENTITY_FIELD`` load-bearing rather
    than cosmetic.
    """
    assert not Membership.objects.filter(tenant=org).exists()


def test_engagement_scope_is_limited_to_its_categories(in_practice: Any) -> None:
    scope = in_practice
    assert scope.categories is not None
    assert "TAX_INDIRECT" in scope.categories
    assert "LABOUR" not in scope.categories, "labour was never part of this engagement"


def test_practice_permissions_come_from_the_engagement_not_its_own_role(
    in_practice: Any,
) -> None:
    """A firm cannot grant itself more access to a client than the client agreed.

    The engagement is the ceiling.
    """
    scope = in_practice
    assert scope.has_permission("tenancy.profile.view")
    assert not scope.has_permission("tenancy.registration.manage")
    assert not scope.has_permission("tenancy.entity.create")


def test_ending_an_engagement_removes_access_immediately(
    practice_membership: Membership, engagement: Engagement, entity_a: Entity
) -> None:
    """Revocation happens on the next request, not on a nightly job."""
    from stacos.tenancy.scope_resolver import resolve_scope_for_membership

    before = resolve_scope_for_membership(practice_membership, reason="test")
    assert entity_a.id in (before.entity_ids or set())

    with tenant_context(tenant_ids=engagement.tenant_id, reason="test:revoke"):
        engagement.end(reason="test")

    after = resolve_scope_for_membership(practice_membership, reason="test")
    assert entity_a.id not in (after.entity_ids or set())
    assert entity_a.tenant_id not in after.readable_tenant_ids


def test_an_expired_engagement_grants_nothing(
    practice_membership: Membership, engagement: Engagement, entity_a: Entity
) -> None:
    """Access must stop on the date the client agreed to, even while the row
    still says ACTIVE because nothing has run to change it."""
    from datetime import date

    from stacos.tenancy.scope_resolver import resolve_scope_for_membership

    with tenant_context(tenant_ids=engagement.tenant_id, reason="test"):
        engagement.ends_on = date(2024, 12, 31)
        engagement.save(update_fields=["ends_on"])

    scope = resolve_scope_for_membership(practice_membership, reason="test")
    assert entity_a.id not in (scope.entity_ids or set())


def test_a_suspended_engagement_grants_nothing(
    practice_membership: Membership, engagement: Engagement, entity_a: Entity
) -> None:
    from stacos.tenancy.scope_resolver import resolve_scope_for_membership

    with tenant_context(tenant_ids=engagement.tenant_id, reason="test"):
        engagement.status = Engagement.Status.SUSPENDED
        engagement.save(update_fields=["status"])

    scope = resolve_scope_for_membership(practice_membership, reason="test")
    assert entity_a.id not in (scope.entity_ids or set())


# ===========================================================================
# 5. PostgreSQL Row-Level Security, the second line of defence
# ===========================================================================


@pytest.mark.parametrize("model", scoped_models(), ids=_model_ids)
def test_every_scoped_table_has_rls_enabled_forced_and_policed(
    model: type[models.Model],
) -> None:
    """A new model must not ship without database-level protection.

    ``FORCE`` matters as much as ``ENABLE``: without it the table owner bypasses
    every policy, and in this test environment the owner is the connecting role —
    which would make every other RLS test here silently pass.
    """
    from stacos.core.rls import table_has_rls

    enabled, forced, policy = table_has_rls(model._meta.db_table)
    assert enabled, f"{model._meta.label}: RLS is not enabled"
    assert forced, f"{model._meta.label}: RLS is not forced; the owner would bypass it"
    assert policy, f"{model._meta.label}: no isolation policy attached"


def test_raw_sql_cannot_cross_tenants(
    org: Tenant, entity_a: Entity, other_org: Tenant, rival_entity: Entity
) -> None:
    """Raw SQL is exactly what RLS exists to catch.

    A hand-written query, a ``.extra()`` call or a ``RawSQL`` aggregate bypasses
    the ORM manager entirely. The database must still refuse.
    """
    with tenant_context(tenant_ids=org.id, reason="test"), connection.cursor() as cursor:
        cursor.execute("SELECT id FROM tenancy_entity")
        visible = {row[0] for row in cursor.fetchall()}

    assert entity_a.id in visible
    assert rival_entity.id not in visible, "raw SQL reached another tenant's rows"


def test_raw_sql_with_no_tenant_setting_returns_nothing(entity_a: Entity) -> None:
    """The policy fails closed.

    An unset ``stacos.tenant_ids`` yields NULL, and ``= ANY(NULL)`` is NULL — so
    a query with no tenant bound sees zero rows rather than all of them.
    """
    from stacos.core.rls import clear_rls_tenants, set_rls_bypass

    set_rls_bypass(False)
    clear_rls_tenants()

    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM tenancy_entity")
        count = cursor.fetchone()[0]

    assert count == 0


def test_raw_insert_for_another_tenant_is_rejected(org: Tenant, other_org: Tenant) -> None:
    """The policy carries a WITH CHECK clause, so writes are constrained too."""
    from uuid import uuid4

    with (
        tenant_context(tenant_ids=org.id, reason="test"),
        pytest.raises(ProgrammingError),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            """
            INSERT INTO tenancy_entity
                (id, tenant_id, name, legal_name, short_code, status, entity_type,
                 country, registered_office_state, registered_office_address,
                 created_at, updated_at, archive_reason)
            VALUES (%s, %s, 'Injected', '', '', 'ACTIVE', 'PVT_LTD', 'IN', '', '',
                    now(), now(), '')
            """,
            [uuid4(), other_org.id],
        )


# ===========================================================================
# 6. The audit log is genuinely append-only
# ===========================================================================


def test_audit_rows_cannot_be_updated(platform: Any, org: Tenant) -> None:
    """ "Immutable" has to be enforced by PostgreSQL, not by convention.

    An audit trail a compromised application can rewrite is not evidence.
    """
    from stacos.core.models import AuditAction, AuditLog

    entry = AuditLog.objects.create(
        tenant=org, action=AuditAction.CREATE, object_type="test", object_id="1"
    )

    with pytest.raises(Exception, match="append-only"), connection.cursor() as cursor:
        cursor.execute("UPDATE core_auditlog SET action = 'DELETE' WHERE id = %s", [entry.id])


def test_audit_rows_cannot_be_deleted(platform: Any, org: Tenant) -> None:
    from stacos.core.models import AuditAction, AuditLog

    entry = AuditLog.objects.create(
        tenant=org, action=AuditAction.CREATE, object_type="test", object_id="1"
    )

    with pytest.raises(Exception, match="append-only"), connection.cursor() as cursor:
        cursor.execute("DELETE FROM core_auditlog WHERE id = %s", [entry.id])


# ===========================================================================
# 7. Structural guarantees
# ===========================================================================


def test_every_project_model_declares_its_tenancy() -> None:
    """Fails the build when someone adds a model without deciding its tenancy.

    Not "without scoping it" — without *deciding*. A genuinely global model is
    fine; leaving the question unanswered is not.
    """
    from stacos.core.checks import check_model_tenancy

    errors = check_model_tenancy()
    assert not errors, "\n".join(f"{e.id}: {e.msg}" for e in errors)


def test_every_view_declares_a_permission() -> None:
    from stacos.core.checks import check_view_permissions

    errors = check_view_permissions()
    assert not errors, "\n".join(f"{e.id}: {e.msg}" for e in errors)


@pytest.mark.parametrize("model", scoped_models(), ids=_model_ids)
def test_scoped_models_declare_a_tenant_field(model: type[models.Model]) -> None:
    assert issubclass(model, TenantScopedModel)
    field_name = model.TENANT_FIELD.removesuffix("_id")
    assert model._meta.get_field(field_name) is not None
