"""
A member's access: their role, and — separately — their reach.

A role answers "what may this person do". Reach answers "on which entities, in
which compliance categories, for which of a firm's clients". Keeping them apart is
what lets one Compliance Manager role serve both the head-office lead who sees
everything and the plant accountant who sees one factory's tax — and it is why
this module never infers one from the other, beyond starting an invitation from a
system role's ``default_categories``.

Both the invitation (reach chosen before anyone accepts) and the edit-access
screen go through here, so the two cannot disagree about what a valid grant is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils.translation import gettext as _

from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.tenancy.models import ComplianceCategory, Entity, Membership, Role, Tenant
from stacos.tenancy.system_roles import system_role

__all__ = [
    "ACCESS_MANAGER_PERMISSION",
    "ROLE_ONLY",
    "AccessError",
    "AccessGrant",
    "apply_grant",
    "default_categories_for",
    "describe_reach",
    "engaged_clients",
    "permission_choices",
    "set_member_access",
    "set_member_permissions",
    "snapshot",
    "validate_grant",
]

#: Holding this is what makes somebody able to fix everyone else's access. A
#: workspace must always keep at least one active member who holds it.
ACCESS_MANAGER_PERMISSION = "accounts.user.role.change"


class AccessError(Exception):
    """A change to somebody's access that cannot be made, fit to show a user."""


@dataclass(frozen=True, slots=True)
class AccessGrant:
    """A member's reach, as chosen on a form.

    ``categories`` empty means every category. For a business, ``all_entities``
    with no ``entity_ids`` means every entity, including ones added later. For a
    firm, ``client_tenant_ids`` empty means the whole client book.
    """

    all_entities: bool = True
    entity_ids: tuple[UUID, ...] = ()
    categories: tuple[str, ...] = ()
    client_tenant_ids: tuple[UUID, ...] = ()


def default_categories_for(role: Role) -> tuple[str, ...]:
    """The compliance areas an invitation into ``role`` starts limited to."""
    spec = system_role(role.code) if role.is_system else None
    return spec.default_categories if spec is not None else ()


def engaged_clients(practice: Tenant) -> list[tuple[UUID, str]]:
    """The client businesses a firm currently has live engagements with.

    Read through the caller's scope, so a member already limited to part of the
    client book can only hand on the part they reach.
    """
    from stacos.engagements.models import Engagement

    rows = (
        Engagement.objects.filter(practice_tenant=practice, status=Engagement.Status.ACTIVE)
        .select_related("tenant")
        .order_by("tenant__name")
    )
    seen: dict[UUID, str] = {}
    for engagement in rows:
        if engagement.is_live:
            seen.setdefault(engagement.tenant_id, engagement.tenant.name)
    return list(seen.items())


def describe_reach(membership: Membership) -> str:
    """One line for the people list: "All entities · Tax only"."""
    tenant_type = membership.tenant.type
    parts: list[str] = []
    if tenant_type == Tenant.Type.ORGANISATION:
        if membership.all_entities:
            parts.append(_("All entities"))
        else:
            names = [entity.name for entity in membership.entities.all()]
            parts.append(", ".join(names) if names else _("No entities"))
    elif tenant_type == Tenant.Type.PRACTICE:
        clients = [tenant.name for tenant in membership.client_tenants.all()]
        parts.append(", ".join(clients) if clients else _("All clients"))
    else:
        return _("No client compliance data")

    # Only said when it narrows something. Areas are set by the role's default
    # (a department user's), not chosen on a form, so "all" is not news.
    if membership.categories:
        labels = dict(ComplianceCategory.choices)
        parts.append(", ".join(str(labels.get(code, code)) for code in membership.categories))
    return " · ".join(str(part) for part in parts)


def _validate(tenant: Tenant, role: Role, grant: AccessGrant) -> None:
    if role.tenant_id not in (None, tenant.id) or role.tenant_type != tenant.type:
        raise AccessError(_("That role does not exist for this workspace."))

    unknown = set(grant.categories) - set(ComplianceCategory.values)
    if unknown:
        raise AccessError(_("Unknown compliance area: %(codes)s.") % {"codes": ", ".join(unknown)})

    if tenant.type == Tenant.Type.ORGANISATION and not grant.all_entities:
        if not grant.entity_ids:
            raise AccessError(_("Choose at least one entity, or give access to all of them."))
        # Through the scoped manager: an id from another tenant — or one this
        # administrator cannot see themselves — simply does not resolve.
        found = set(
            Entity.objects.filter(pk__in=grant.entity_ids, tenant=tenant).values_list(
                "pk", flat=True
            )
        )
        if found != set(grant.entity_ids):
            raise AccessError(_("One of those entities is not in this workspace."))

    if tenant.type == Tenant.Type.PRACTICE and grant.client_tenant_ids:
        reachable = {client_id for client_id, _name in engaged_clients(tenant)}
        if not set(grant.client_tenant_ids) <= reachable:
            raise AccessError(_("One of those clients has no live engagement with this firm."))


def _others_can_manage_access(membership: Membership) -> bool:
    """Whether, without ``membership``, somebody active could still manage access."""
    others = (
        Membership.objects.filter(tenant=membership.tenant, status=Membership.Status.ACTIVE)
        .exclude(pk=membership.pk)
        .select_related("role", "tenant")
    )
    return any(ACCESS_MANAGER_PERMISSION in other.resolved_permissions() for other in others)


def snapshot(membership: Membership) -> dict[str, Any]:
    """The audit trail's before/after picture of somebody's access."""
    return {
        "role": membership.role.code,
        "all_entities": membership.all_entities,
        "entities": sorted(str(pk) for pk in membership.entities.values_list("pk", flat=True)),
        "categories": sorted(membership.categories),
        "client_tenants": sorted(
            str(pk) for pk in membership.client_tenants.values_list("pk", flat=True)
        ),
        "extra_permissions": sorted(membership.extra_permissions or ()),
        "revoked_permissions": sorted(membership.revoked_permissions or ()),
    }


def apply_grant(membership: Membership, grant: AccessGrant) -> None:
    """Write a grant onto a membership. No checks and no audit — the callers do both."""
    tenant_type = membership.tenant.type
    membership.categories = list(grant.categories)
    membership.all_entities = tenant_type != Tenant.Type.ORGANISATION or grant.all_entities
    membership.save(update_fields=["categories", "all_entities", "updated_at"])
    if tenant_type == Tenant.Type.ORGANISATION:
        entity_ids: list[Any] = [] if grant.all_entities else [*grant.entity_ids]
        membership.entities.set(entity_ids)
    if tenant_type == Tenant.Type.PRACTICE:
        client_ids: list[Any] = [*grant.client_tenant_ids]
        membership.client_tenants.set(client_ids)


def validate_grant(tenant: Tenant, role: Role, grant: AccessGrant) -> None:
    """Refuse a role or reach that is not valid for ``tenant``."""
    _validate(tenant, role, grant)


@transaction.atomic
def set_member_access(
    membership: Membership,
    *,
    role: Role,
    grant: AccessGrant,
    actor: Any,
) -> Membership:
    """Change somebody's role and reach together, and record both.

    Refuses two changes that would leave a workspace worse off than a refusal:
    changing your own access (an administrator narrowing themselves by accident
    has nobody to undo it, and widening yourself is the thing the audit trail
    exists to prevent), and removing the last person who can manage access.
    """
    if getattr(actor, "pk", None) == membership.user_id:
        raise AccessError(_("You cannot change your own access. Ask another administrator."))

    _validate(membership.tenant, role, grant)

    loses_management = ACCESS_MANAGER_PERMISSION in membership.resolved_permissions() and (
        ACCESS_MANAGER_PERMISSION not in role.permissions
    )
    if loses_management and not _others_can_manage_access(membership):
        raise AccessError(
            _(
                "This is the last person who can manage access here. Give somebody "
                "else that role first."
            )
        )

    before = snapshot(membership)
    if role.pk != membership.role_id:
        # A new role is a fresh start: individual additions and removals were
        # made against the old one and would mean something different now.
        membership.extra_permissions = []
        membership.revoked_permissions = []
    membership.role = role
    membership.save(
        update_fields=["role", "extra_permissions", "revoked_permissions", "updated_at"]
    )
    apply_grant(membership, grant)
    after = snapshot(membership)

    if before != after:
        record_event(
            action=AuditAction.UPDATE,
            actor=actor,
            obj=membership,
            before=before,
            after=after,
            context={"change": "access"},
        )
    return membership


# ---------------------------------------------------------------------------
# Individual permissions
# ---------------------------------------------------------------------------


#: Permissions that come only with a role, never one person at a time. Marking
#: work done without review is the maker-checker exception, kept to the people
#: answerable for the business or the firm (Owner, Partner) by decision — a
#: checkbox on one colleague would quietly widen it.
ROLE_ONLY: frozenset[str] = frozenset({"compliance.obligation.complete_unreviewed"})


def _grantable_codes(tenant_type: str) -> frozenset[str]:
    """Every permission that exists for this kind of workspace, platform-only aside."""
    from stacos.core.permissions import permission_registry

    return frozenset(p.code for p in permission_registry.for_tenant_type(tenant_type))


def permission_choices(
    membership: Membership, actor_permissions: frozenset[str]
) -> list[dict[str, Any]]:
    """The checklist, by category: what this administrator can change for this member.

    Each row says whether the member holds it now, whether it comes with their
    role, and whether it is locked (held by the member, not by the
    administrator). Permissions neither of them holds are left out — offering a
    box that can only be refused is not a choice.
    """
    from stacos.core.permissions import permission_registry

    current = membership.resolved_permissions()
    from_role = permission_registry.expand(membership.role.permissions)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for permission in sorted(
        permission_registry.for_tenant_type(membership.tenant.type),
        key=lambda p: (p.category, p.label),
    ):
        code = permission.code
        if code not in actor_permissions and code not in current:
            continue
        held = code in current
        grouped.setdefault(permission.category, []).append(
            {
                "code": code,
                "label": permission.label,
                "description": permission.description,
                "sensitive": permission.is_sensitive,
                "held": held,
                "locked": code not in actor_permissions or code in ROLE_ONLY,
                "role_only": code in ROLE_ONLY,
                "origin": (
                    "role"
                    if held and code in from_role
                    else "added"
                    if held
                    else "removed"
                    if code in from_role
                    else ""
                ),
            }
        )
    return [{"category": category, "rows": rows} for category, rows in grouped.items()]


@transaction.atomic
def set_member_permissions(
    membership: Membership,
    *,
    granted: Any,
    actor: Any,
    actor_permissions: frozenset[str],
) -> Membership:
    """Tailor one member's permissions on top of their role.

    ``granted`` is the whole ticked list, not a diff. It is closed over
    ``implies`` first — ticking "record a filing" brings "view the calendar"
    with it — so a removal can never leave a permission standing without the
    one it depends on. What is stored is only the difference from the role:
    additions in ``extra_permissions``, removals in ``revoked_permissions``.

    An administrator changes only what they hold themselves: they cannot hand
    out more than they have, and what the member holds beyond them is kept
    untouched. Two outright refusals, each a change nobody could safely undo:
    tailoring yourself, and taking access management from the last person who
    has it.
    """
    from stacos.core.permissions import permission_registry

    if getattr(actor, "pk", None) == membership.user_id:
        raise AccessError(_("You cannot change your own permissions. Ask another administrator."))

    allowed = _grantable_codes(membership.tenant.type)
    requested = set(granted)
    unknown = requested - allowed
    if unknown:
        raise AccessError(
            _("These permissions do not exist here: %(codes)s.")
            % {"codes": ", ".join(sorted(unknown))}
        )

    current = membership.resolved_permissions()
    forged = sorted(requested - actor_permissions - current)
    if forged:
        labels = ", ".join(permission_registry.get(code).label for code in forged)
        raise AccessError(
            _("You can only give permissions you hold yourself. Not held by you: %(labels)s.")
            % {"labels": labels}
        )
    # What the member holds and the administrator does not is not theirs to
    # change either way: it is kept, whatever the form sent.
    untouchable = (current - actor_permissions) | (current & ROLE_ONLY)
    requested -= ROLE_ONLY
    checked = permission_registry.expand((requested & actor_permissions) | untouchable) & allowed

    if (
        ACCESS_MANAGER_PERMISSION in current
        and ACCESS_MANAGER_PERMISSION not in checked
        and not _others_can_manage_access(membership)
    ):
        raise AccessError(
            _(
                "This is the last person who can manage access here. Give somebody "
                "else that permission first."
            )
        )

    from_role = permission_registry.expand(membership.role.permissions) & allowed
    before = snapshot(membership)
    membership.extra_permissions = sorted(checked - from_role)
    membership.revoked_permissions = sorted(from_role - checked)
    membership.save(update_fields=["extra_permissions", "revoked_permissions", "updated_at"])
    after = snapshot(membership)

    if before != after:
        record_event(
            action=AuditAction.UPDATE,
            actor=actor,
            obj=membership,
            before=before,
            after=after,
            context={"change": "permissions"},
        )
    return membership
