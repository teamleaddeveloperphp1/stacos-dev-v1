"""
Managers that make an unscoped query impossible to write by accident.

The central claim of the tenancy design is that a developer cannot forget to
filter by tenant, because the unfiltered queryset does not exist on the default
manager. Everything else — Row-Level Security, the isolation test suite — is
defence in depth behind this one property.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, cast

from django.db import models

from stacos.core.exceptions import UnscopedQueryError
from stacos.core.scope import AccessScope, current_scope

if TYPE_CHECKING:
    from uuid import UUID

__all__ = ["TenantScopedManager", "TenantScopedQuerySet", "UnscopedManager"]


class TenantScopedQuerySet(models.QuerySet[Any]):
    """QuerySet with helpers for working inside a bound scope."""

    def for_entities(self, entity_ids: list[UUID]) -> Self:
        """Narrow to specific entities, on top of whatever the scope allows."""
        entity_field = getattr(self.model, "ENTITY_FIELD", None)
        if not entity_field:
            raise TypeError(f"{self.model.__name__} is not entity-scoped")
        return self.filter(**{f"{entity_field}__in": entity_ids})

    def writable(self) -> Self:
        """Restrict to rows the current scope may modify.

        Read access through an engagement is often broader than write access, so
        a list that offers edit controls should build them from this, not from
        the read queryset.
        """
        scope = current_scope()
        if scope is None:
            raise UnscopedQueryError(f"{self.model.__name__}.writable() outside a scope")
        if scope.bypass:
            return self
        tenant_field = getattr(self.model, "TENANT_FIELD", "tenant_id")
        return self.filter(**{f"{tenant_field}__in": scope.writable_tenant_ids})


class TenantScopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):  # type: ignore[misc]
    """The default manager for every tenant-owned model.

    Raises :class:`UnscopedQueryError` when no scope is bound. That is the point:
    a missing scope surfaces as a loud failure in development rather than a quiet
    cross-tenant read in production.

    If you genuinely need every row — a platform-wide report, a data migration —
    use ``platform_scope()`` or the deliberately ugly ``objects_unscoped``
    manager, both of which are allowlisted in CI and audited.
    """

    # Django uses the *base* manager for related-object descriptors and for
    # `Model.refresh_from_db`. Leaving those scoped would make a legitimate
    # `obj.entity` raise when the FK's tenant is outside the scope, which is
    # confusing; the read is still protected by RLS at the database.
    use_in_migrations = False

    def get_queryset(self) -> TenantScopedQuerySet:
        scope = current_scope()
        if scope is None:
            raise UnscopedQueryError(
                f"{self.model._meta.label} was queried with no AccessScope bound.\n"
                f"  In a view:    the ScopeMiddleware binds one automatically — is this "
                f"path excluded from it?\n"
                f"  In a task:    wrap the work in `tenant_context(tenant_ids=..., reason=...)`.\n"
                f"  In a test:    use the `tenant_scope` fixture.\n"
                f"  Platform-wide: use `platform_scope(reason=...)`."
            )

        queryset = cast("TenantScopedQuerySet", super().get_queryset())
        if scope.bypass:
            return queryset

        return cast("TenantScopedQuerySet", _apply_scope(queryset, self.model, scope))


class UnscopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):  # type: ignore[misc]
    """Bypasses tenant filtering entirely.

    Exposed as ``Model.objects_unscoped``. The name is deliberately unpleasant so
    it stands out in review, and every use site is counted against
    ``tests/unscoped_allowlist.txt`` by a CI check — adding one requires an
    explicit entry with a justification.

    Legitimate uses are narrow: uniqueness checks that must consider all tenants
    (an email address, a GSTIN duplicate detector), and platform administration.
    """


def _apply_scope(
    queryset: models.QuerySet[Any],
    model: type[models.Model],
    scope: AccessScope,
) -> Any:
    """Filter a queryset down to what the scope permits."""
    tenant_field = getattr(model, "TENANT_FIELD", "tenant_id")
    entity_field = getattr(model, "ENTITY_FIELD", None)

    if entity_field:
        # Entity-scoped: reachable through an engagement, but only for the named
        # entities that engagement covers.
        if not scope.readable_tenant_ids:
            return queryset.none()
        queryset = queryset.filter(**{f"{tenant_field}__in": scope.readable_tenant_ids})
        if scope.entity_ids is not None:
            queryset = queryset.filter(**{f"{entity_field}__in": scope.entity_ids})
        return queryset

    # Tenant-level: memberships, settings, billing. An engagement grants a
    # practice access to a client's *compliance records*, never to the client's
    # own administration — so these are filtered on membership, not on the
    # broader engagement reach. Without this distinction a firm engaged on one
    # subsidiary could read the whole client's staff directory.
    if not scope.member_tenant_ids:
        return queryset.none()
    return queryset.filter(**{f"{tenant_field}__in": scope.member_tenant_ids})
