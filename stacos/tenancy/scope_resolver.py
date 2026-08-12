"""
Turning an authenticated user into an :class:`~stacos.core.scope.AccessScope`.

This is the single place in STACOS where cross-tenant access is granted. Every
query in every module is filtered by what this function returns, so it is the one
function worth reading twice.

The resolution, in order:

1. Which tenant is the user *in* right now (the tenant switcher's selection)?
2. What does their membership of that tenant allow?
3. If it is a practice, which client entities do live engagements reach — and
   with which permissions and categories?

Two rules that are easy to get wrong and expensive to get wrong:

* **Permissions for a client entity come from the engagement, not from the
  practice's own role.** A firm cannot grant itself more access to a client than
  the client agreed to. The engagement is the ceiling.
* **An expired engagement is dead the day it expires**, not whenever a job next
  runs. The date window is evaluated here, on every request.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

import structlog
from django.http import HttpRequest

from stacos.core.scope import AccessScope
from stacos.tenancy.models import Membership, Tenant

logger = structlog.get_logger(__name__)

__all__ = ["SESSION_TENANT_KEY", "resolve_scope_for_membership", "resolve_scope_for_request"]

SESSION_TENANT_KEY = "stacos_tenant_id"
REQUEST_CACHE_ATTR = "_stacos_access_scope"


def resolve_scope_for_request(request: HttpRequest) -> AccessScope | None:
    """Build the scope for this request, memoised for its lifetime."""
    cached = getattr(request, REQUEST_CACHE_ATTR, None)
    if cached is not None:
        return cast("AccessScope", cached)

    membership = _select_membership(request)
    if membership is None:
        # Authenticated but not a member of anything yet — mid-onboarding, or an
        # invitation not yet accepted. An empty scope means every scoped query
        # returns nothing rather than raising, so onboarding screens still work.
        scope = AccessScope(reason="request:no-membership")
        setattr(request, REQUEST_CACHE_ATTR, scope)
        request.tenant = None  # type: ignore[attr-defined]
        return scope

    scope = resolve_scope_for_membership(membership, reason="request")
    setattr(request, REQUEST_CACHE_ATTR, scope)
    request.tenant = membership.tenant  # type: ignore[attr-defined]
    request.membership = membership  # type: ignore[attr-defined]
    return scope


def _select_membership(request: HttpRequest) -> Membership | None:
    """Find the membership for the tenant the user has switched to.

    Falls back to their first active membership, so a user with one tenant never
    has to choose. A stale or forged tenant id in the session resolves to nothing
    and falls back — it cannot be used to reach a tenant the user is not in,
    because the lookup is always filtered by ``user``.
    """
    user = request.user
    base = (
        Membership.objects_unscoped.filter(user=user, status=Membership.Status.ACTIVE)
        .select_related("tenant", "role")
        .order_by("created_at")
    )

    selected = request.session.get(SESSION_TENANT_KEY)
    if selected:
        membership = cast("Membership | None", base.filter(tenant_id=selected).first())
        if membership is not None:
            return membership
        logger.info("tenancy.stale_tenant_selection", user_id=str(user.pk), tenant_id=selected)

    return cast("Membership | None", base.first())


def resolve_scope_for_membership(membership: Membership, *, reason: str) -> AccessScope:
    """Build the scope a membership implies, including engagement reach."""
    tenant = membership.tenant
    permissions = set(membership.resolved_permissions())

    readable: set[UUID] = {tenant.id}
    writable: set[UUID] = {tenant.id}
    entity_ids: set[UUID] | None = None
    categories: set[str] | None = set(membership.categories) or None

    # -- Entities within the member's own tenant ----------------------------
    if not membership.all_entities:
        entity_ids = set(membership.entities.values_list("id", flat=True))

    # -- Client entities reached through engagements ------------------------
    if tenant.type == Tenant.Type.PRACTICE:
        engaged_entities, engaged_tenants, engaged_permissions, engaged_categories = (
            _resolve_engagements(membership)
        )
        readable |= engaged_tenants
        # Engagements are read-and-work access to a client's records, but the
        # client tenant itself is never writable by the practice: the practice
        # cannot add users to it, change its plan, or create entities in it.
        entity_ids = (entity_ids or set()) | engaged_entities
        permissions |= engaged_permissions
        if engaged_categories is not None:
            categories = (categories or set()) | engaged_categories

    if tenant.type == Tenant.Type.DEALER:
        # A dealer sells and provisions. It sees billing and subscription state,
        # never compliance data, unless the client has granted it explicitly —
        # and such a grant is modelled as an engagement like any other.
        pass

    return AccessScope(
        principal_tenant_id=tenant.id,
        readable_tenant_ids=frozenset(readable),
        writable_tenant_ids=frozenset(writable),
        entity_ids=frozenset(entity_ids) if entity_ids is not None else None,
        categories=frozenset(categories) if categories is not None else None,
        permissions=frozenset(permissions),
        reason=reason,
    )


def _resolve_engagements(
    membership: Membership,
) -> tuple[set[UUID], set[UUID], set[str], set[str] | None]:
    """Collect the entities, tenants, permissions and categories an engagement grants.

    Uses ``objects_unscoped`` because this *is* the query that establishes the
    scope — there is nothing bound yet to filter by. It is safe precisely because
    it is anchored on the practice tenant the user is already a member of. This
    call is allowlisted in ``tests/unscoped_allowlist.txt``.
    """
    from stacos.engagements.models import Engagement

    queryset = Engagement.objects_unscoped.filter(
        practice_tenant_id=membership.tenant_id,
        status=Engagement.Status.ACTIVE,
    ).select_related("entity")

    # A practice member may be limited to part of the client book.
    client_tenant_ids = set(membership.client_tenants.values_list("id", flat=True))
    if client_tenant_ids:
        queryset = queryset.filter(tenant_id__in=client_tenant_ids)

    entities: set[UUID] = set()
    tenants: set[UUID] = set()
    permissions: set[str] = set()
    categories: set[str] = set()
    unrestricted_categories = False

    for engagement in queryset:
        if not engagement.is_live:
            continue
        entities.add(engagement.entity_id)
        tenants.add(engagement.tenant_id)
        permissions.update(engagement.permissions)
        if engagement.categories:
            categories.update(engagement.categories)
        else:
            # An unrestricted engagement means the practice is not category
            # limited, so no category filter can be applied at all.
            unrestricted_categories = True

    from stacos.core.permissions import permission_registry

    return (
        entities,
        tenants,
        set(permission_registry.expand(permissions)),
        None if unrestricted_categories else (categories or None),
    )
