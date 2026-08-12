"""
Who gets told.

This is the part of a notification system that is always underestimated. Sending
is trivial; deciding *who* is the product decision, and getting it wrong in
either direction is expensive:

* Too narrow — only the assignee — and a filing goes unfiled while the assignee
  is on leave, which is precisely the failure the product is sold to prevent.
* Too wide — everyone in the tenant — and within a fortnight every user has a
  filter rule sending STACOS to a folder they never open, at which point no
  notification works at all.

So the rule is: the person responsible, plus the people who would have to answer
for it if nobody acted. In practice that is the assignee and the members whose
role carries the relevant permission *and* whose scoping actually reaches the
entity in question. The scoping check is not decoration — a plant HR user with
two entities and a labour-only category must not be told about a group company's
GST return.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.contrib.auth import get_user_model

from stacos.core.permissions import permission_registry
from stacos.tenancy.models import Membership

__all__ = ["for_entity", "for_tenant", "with_permission"]

User = get_user_model()


def with_permission(
    *,
    tenant_id: UUID | str,
    permission: str,
    entity: Any = None,
    category: str = "",
) -> list[Any]:
    """Active members who hold ``permission`` and can actually see ``entity``.

    The permission is resolved through the role's own expansion, so a role that
    implies a permission counts — otherwise every caller would have to know the
    implication graph, and would eventually get it wrong.
    """
    memberships = (
        Membership.objects.filter(tenant_id=tenant_id, status=Membership.Status.ACTIVE)
        .select_related("user", "role")
        .prefetch_related("entities")
    )

    recipients: list[Any] = []
    seen: set[Any] = set()

    for membership in memberships:
        user = membership.user
        if user.pk in seen or not user.is_active:
            continue
        if not _holds(membership, permission):
            continue
        if entity is not None and not _reaches(membership, entity):
            continue
        if category and membership.categories and category not in membership.categories:
            continue
        seen.add(user.pk)
        recipients.append(user)

    return recipients


def _holds(membership: Membership, permission: str) -> bool:
    """Whether this membership carries the permission, implications included.

    Closing over ``implies`` rather than testing the stored list directly: a role
    granted `compliance.obligation.close` implies the view permission, and a
    notification rule that tested the raw list would skip exactly the senior
    people most in need of telling.
    """
    codes = list(membership.role.permissions or []) + list(membership.extra_permissions or [])
    return permission in permission_registry.expand(codes)


def _reaches(membership: Membership, entity: Any) -> bool:
    if membership.all_entities:
        return True
    return any(scoped.pk == entity.pk for scoped in membership.entities.all())


def for_entity(
    *,
    tenant_id: UUID | str,
    entity: Any,
    permission: str,
    assignee: Any = None,
    category: str = "",
) -> list[Any]:
    """The assignee first, then everyone who would have to answer for it.

    The assignee is included whether or not their role carries the permission:
    somebody handed a piece of work is told about it, and an assignment that
    outruns a permission is a configuration problem to surface elsewhere, not a
    reason to leave them uninformed.
    """
    recipients: list[Any] = []
    seen: set[Any] = set()

    if assignee is not None and getattr(assignee, "is_active", False):
        recipients.append(assignee)
        seen.add(assignee.pk)

    for user in with_permission(
        tenant_id=tenant_id, permission=permission, entity=entity, category=category
    ):
        if user.pk not in seen:
            seen.add(user.pk)
            recipients.append(user)

    return recipients


def for_tenant(*, tenant_id: UUID | str, permission: str) -> list[Any]:
    """Tenant-level notifications — billing, mostly — which belong to no entity."""
    return with_permission(tenant_id=tenant_id, permission=permission)


def external_contacts(request: Any) -> list[str]:
    """Email addresses on an information request that belong to no user account.

    A client contact who has never signed in still has to be chased, and they are
    the single largest source of "we never heard back". Returned as addresses
    rather than users because there is no user to return.
    """
    address = (getattr(request, "assigned_email", "") or "").strip()
    return [address] if address else []


def deduplicate(users: list[Any]) -> list[Any]:
    """Preserve order, drop repeats. Used where several sources are concatenated."""
    seen: set[Any] = set()
    out: list[Any] = []
    for user in users:
        if user is not None and user.pk not in seen:
            seen.add(user.pk)
            out.append(user)
    return out
