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

from stacos.core.rls import rls_bootstrap
from stacos.core.scope import AccessScope
from stacos.tenancy.models import Membership, Tenant

logger = structlog.get_logger(__name__)

__all__ = [
    "SESSION_TENANT_KEY",
    "TENANT_HEADER",
    "resolve_scope_for_membership",
    "resolve_scope_for_request",
]

SESSION_TENANT_KEY = "stacos_tenant_id"

#: How a mobile client says which tenant it is working in. A JWT request has no
#: session, so without this an accountant who works across four clients would be
#: permanently stuck in whichever membership happened to be created first.
#:
#: Safe to take from the client because the lookup that consumes it is anchored
#: on the authenticated user: a forged id resolves to no membership and falls
#: back, exactly as a stale session value does. It cannot widen access.
TENANT_HEADER = "X-Stacos-Tenant"

REQUEST_CACHE_ATTR = "_stacos_access_scope"


def resolve_scope_for_request(request: HttpRequest) -> AccessScope | None:
    """Build the scope for this request, memoised for its lifetime."""
    cached = getattr(request, REQUEST_CACHE_ATTR, None)
    if cached is not None:
        return cast("AccessScope", cached)

    membership = _select_membership(request)
    if membership is None:
        # Authenticated but not a member of anything *active*. Every scoped query
        # returns nothing rather than raising, so the screens render.
        #
        # Which permission that comes with depends on why, and the difference is
        # the whole of it:
        #
        # * **Nothing, or an invitation not yet accepted** — one permission,
        #   ``tenancy.onboarding.start``. Without it a self-service user who has
        #   just verified their email is authenticated, owns nothing, and has no
        #   route in the product to create the company they signed up to manage.
        # * **Suspended** — none at all. Somebody revoked this deliberately, and
        #   handing them the permission to create a fresh organisation would make
        #   the revocation a formality. ``OrganisationGateMiddleware`` renders the
        #   screen that says so; this is what makes it a boundary rather than a
        #   sign on a door that opens.
        #
        # Neither confers access to anybody else's data: no tenant is bound, so
        # the scoped managers and the RLS policy both return nothing.
        suspended = getattr(request, "suspended_memberships", None)
        scope = AccessScope(
            permissions=frozenset() if suspended else frozenset({"tenancy.onboarding.start"}),
            reason="request:suspended" if suspended else "request:no-membership",
        )
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

    The whole set is materialised rather than queried twice, and left on the
    request as ``user_memberships``. The tenant switcher in the shell needs
    exactly this list and cannot obtain it for itself: ``member_tenant_ids``
    holds only the tenant currently switched *to*, so the scoped manager would
    answer with the one organisation the user is already looking at. Reusing the
    bootstrap read that has to happen anyway is cheaper than a second escape
    hatch, and keeps the one RLS lift in the request path where it is.

    The read covers suspended and invited rows as well. Only the active ones are
    ever selected from — that has not changed — but "suspended", "invited, not
    yet accepted" and "belongs to nothing at all" are three situations that need
    three different answers, and one of them must not be given the permission to
    create an organisation. Widening this one query is what lets the caller tell
    them apart, without a second query or a second lift of Row-Level Security.
    """
    user = request.user

    # The bootstrap read: memberships are themselves RLS-protected, and nothing
    # is bound yet. Anchored on the authenticated user, so it can only ever
    # return rows about them.
    with rls_bootstrap():
        rows = list(
            Membership.objects_unscoped.filter(
                user=user,
                status__in=(
                    Membership.Status.ACTIVE,
                    Membership.Status.SUSPENDED,
                    Membership.Status.INVITED,
                ),
            )
            .select_related("tenant", "role")
            .order_by("created_at")
        )

    memberships = [m for m in rows if m.status == Membership.Status.ACTIVE]
    request.user_memberships = memberships  # type: ignore[attr-defined]
    # Only when there is nothing active. A user suspended from one organisation
    # and active in another is simply a member of the second — showing them a
    # refusal would be wrong, and so would revoking their permissions.
    request.suspended_memberships = (  # type: ignore[attr-defined]
        [m for m in rows if m.status == Membership.Status.SUSPENDED] if not memberships else []
    )
    by_tenant = {str(m.tenant_id): m for m in memberships}

    for selected, source in (
        (_session_selection(request), "session"),
        (request.headers.get(TENANT_HEADER), "header"),
    ):
        if not selected:
            continue
        membership = cast("Membership | None", by_tenant.get(str(selected)))
        if membership is not None:
            return membership
        logger.info(
            "tenancy.stale_tenant_selection",
            user_id=str(user.pk),
            tenant_id=selected,
            source=source,
        )

    return memberships[0] if memberships else None


def _session_selection(request: HttpRequest) -> str | None:
    """The tenant switcher's choice, when there is a session at all.

    A mobile request authenticated by JWT has no session, and touching
    ``request.session`` on one would raise.
    """
    session = getattr(request, "session", None)
    return session.get(SESSION_TENANT_KEY) if session is not None else None


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
        # Only the tenant the user actually belongs to. Engagement-reached
        # tenants are in `readable_tenant_ids` and stop at entity-scoped records.
        member_tenant_ids=frozenset({tenant.id}),
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

    entities: set[UUID] = set()
    tenants: set[UUID] = set()
    permissions: set[str] = set()
    categories: set[str] = set()
    unrestricted_categories = False

    # Same bootstrap problem as memberships: engagements are RLS-protected and
    # nothing is bound yet. Anchored on the practice tenant the user is already
    # an active member of.
    with rls_bootstrap():
        queryset = Engagement.objects_unscoped.filter(
            practice_tenant_id=membership.tenant_id,
            status=Engagement.Status.ACTIVE,
        ).select_related("entity")

        # A practice member may be limited to part of the client book.
        client_tenant_ids = set(membership.client_tenants.values_list("id", flat=True))
        if client_tenant_ids:
            queryset = queryset.filter(tenant_id__in=client_tenant_ids)

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
