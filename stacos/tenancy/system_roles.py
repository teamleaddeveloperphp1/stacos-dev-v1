"""
The system role bundles that ship with the product.

Roles are named sets of permission codes, never authority in their own right.
That distinction matters: the moment code asks "is this user a manager?" instead
of "may this user approve a return?", the same decision starts being made in
several places and disagreeing in one of them.

Defined here as data and applied by ``manage.py sync_system_roles`` rather than
in a data migration, so a permission added to a bundle reaches existing tenants
without editing migration history.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stacos.tenancy.models import Tenant

__all__ = ["SYSTEM_ROLES", "RoleSpec"]


@dataclass(frozen=True, slots=True)
class RoleSpec:
    code: str
    name: str
    tenant_type: str
    description: str
    permissions: frozenset[str]
    rank: int = 100
    #: Categories this role is limited to by default. Empty means all — the
    #: limit is a property of the membership, and this is only its starting point.
    default_categories: tuple[str, ...] = field(default_factory=tuple)


# --- Shared bundles, so the same capability is spelled the same way twice -----

_VIEW_BASICS = frozenset(
    {
        "core.search",
        "accounts.security.manage",
        "tenancy.tenant.view",
        "tenancy.entity.view",
        "tenancy.profile.view",
        "engagements.view",
    }
)

_ENTITY_STEWARD = _VIEW_BASICS | {
    "tenancy.entity.edit",
    "tenancy.profile.edit",
    "tenancy.registration.view",
    "tenancy.premises.manage",
    "core.audit.view",
}


SYSTEM_ROLES: tuple[RoleSpec, ...] = (
    # -----------------------------------------------------------------------
    # Organisation — a business
    # -----------------------------------------------------------------------
    RoleSpec(
        code="org-owner",
        name="Owner / Director",
        tenant_type=Tenant.Type.ORGANISATION,
        rank=10,
        description=(
            "Full authority over the organisation: approves returns and "
            "engagements, manages users and billing."
        ),
        permissions=_ENTITY_STEWARD
        | {
            "tenancy.tenant.manage",
            "tenancy.entity.create",
            "tenancy.entity.archive",
            "tenancy.registration.manage",
            "accounts.user.view",
            "accounts.user.invite",
            "accounts.user.role.change",
            "accounts.user.deactivate",
            "accounts.role.manage",
            "engagements.invite",
            "engagements.grant",
            "engagements.revoke",
            "engagements.scope.edit",
            "core.audit.export",
            "core.data.export",
            "finance.view",
        },
    ),
    RoleSpec(
        code="org-compliance-manager",
        name="Compliance Manager",
        tenant_type=Tenant.Type.ORGANISATION,
        rank=20,
        description=(
            "Works the checklist day to day: uploads evidence, answers "
            "information requests, chases internal departments. Cannot approve "
            "returns or change who has access."
        ),
        permissions=_ENTITY_STEWARD
        | {
            "tenancy.entity.create",
            "tenancy.registration.view",
            "accounts.user.view",
            "engagements.invite",
            "finance.view",
        },
    ),
    RoleSpec(
        code="org-department-user",
        name="Department User",
        tenant_type=Tenant.Type.ORGANISATION,
        rank=40,
        description=(
            "A plant, HR or admin user who closes internal compliances only — "
            "fire, safety, POSH, licences. Sees nothing financial."
        ),
        # Note the absence of `finance.view`: this user sees an obligation's
        # title and due date but every amount is masked.
        permissions=_VIEW_BASICS | {"tenancy.premises.manage"},
        default_categories=("SAFETY_FIRE", "LABOUR", "ENVIRONMENT", "LICENSING"),
    ),
    RoleSpec(
        code="org-viewer",
        name="Viewer",
        tenant_type=Tenant.Type.ORGANISATION,
        rank=50,
        description="Read-only access, for auditors and observers.",
        permissions=_VIEW_BASICS | {"core.audit.view"},
    ),
    # -----------------------------------------------------------------------
    # Practice — a professional firm
    # -----------------------------------------------------------------------
    RoleSpec(
        code="practice-partner",
        name="Partner",
        tenant_type=Tenant.Type.PRACTICE,
        rank=10,
        description=(
            "Runs the firm: the whole client book, the risk board, staff "
            "utilisation, work in progress and billing."
        ),
        permissions=_ENTITY_STEWARD
        | {
            "tenancy.tenant.manage",
            "accounts.user.view",
            "accounts.user.invite",
            "accounts.user.role.change",
            "accounts.user.deactivate",
            "accounts.role.manage",
            "engagements.invite",
            "engagements.scope.edit",
            "core.audit.export",
            "core.data.export",
            "finance.view",
        },
    ),
    RoleSpec(
        code="practice-manager",
        name="Manager",
        tenant_type=Tenant.Type.PRACTICE,
        rank=20,
        description=(
            "Owns a set of clients, assigns work to staff, reviews preparer "
            "work and pushes returns for client approval."
        ),
        permissions=_ENTITY_STEWARD
        | {
            "accounts.user.view",
            "engagements.invite",
            "finance.view",
        },
    ),
    RoleSpec(
        code="practice-staff",
        name="Staff / Article",
        tenant_type=Tenant.Type.PRACTICE,
        rank=40,
        description=(
            "Executes assigned work, prepares returns, logs time and raises information requests."
        ),
        permissions=_VIEW_BASICS | {"tenancy.profile.edit", "tenancy.registration.view"},
    ),
    # -----------------------------------------------------------------------
    # Dealer — a channel partner
    #
    # Note what is absent. A dealer sells subscriptions and manages accounts; it
    # has NO access to a client's compliance, financial or document data. That
    # requires an explicit, time-boxed, client-approved grant, modelled as an
    # engagement like any other. This is not a limitation to be relaxed later —
    # it is the difference between a trusted platform and a data-leak headline.
    # -----------------------------------------------------------------------
    RoleSpec(
        code="dealer-principal",
        name="Dealer Principal",
        tenant_type=Tenant.Type.DEALER,
        rank=10,
        description=(
            "Creates and manages client accounts, applies plans, takes payment "
            "on their behalf, and sees the commission ledger. Cannot see any "
            "client compliance data."
        ),
        permissions=frozenset(
            {
                "core.search",
                "accounts.security.manage",
                "tenancy.tenant.view",
                "accounts.user.view",
                "accounts.user.invite",
            }
        ),
    ),
    RoleSpec(
        code="dealer-staff",
        name="Dealer Staff",
        tenant_type=Tenant.Type.DEALER,
        rank=30,
        description="Onboards and supports client accounts. No compliance data.",
        permissions=frozenset({"core.search", "accounts.security.manage", "tenancy.tenant.view"}),
    ),
)
