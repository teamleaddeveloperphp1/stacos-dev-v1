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
        # Seeing the calendar, and seeing what the law behind an entry says, is
        # the product's floor. A user who cannot read either has nothing to look
        # at, so both sit in the basic bundle rather than being granted upwards.
        "compliance.obligation.view",
        "compliance.obligation.comment",
        "compliance.library.view",
        "catalog.view",
        # Notifications are addressed to one person and grant nothing about
        # anyone else, so they sit in the floor bundle. A user who can sign in
        # but cannot see what the platform has been telling them — or change how
        # loudly it does so — is a user who will turn the product off at their
        # mail client instead.
        "notifications.view",
        "notifications.preferences.manage",
    }
)

_ENTITY_STEWARD = _VIEW_BASICS | {
    "tenancy.entity.edit",
    "tenancy.profile.edit",
    "tenancy.registration.view",
    "tenancy.premises.manage",
    "core.audit.view",
}

#: Doing the work: preparing, chasing information, recording the dates that
#: unblock a due date. Everything short of taking responsibility for a filing.
_COMPLIANCE_PREPARER = frozenset(
    {
        "compliance.obligation.prepare",
        "compliance.obligation.request_info",
        "compliance.obligation.assign",
        "compliance.event.record",
        "compliance.calendar.rebuild",
    }
)

#: Taking responsibility: review, sign-off, recording the filing, closing it.
#: Split from preparation because that is how firms actually delegate — an
#: article clerk prepares and a partner is answerable.
_COMPLIANCE_APPROVER = _COMPLIANCE_PREPARER | {
    "compliance.obligation.review",
    "compliance.obligation.file",
    "compliance.obligation.close",
    "compliance.obligation.defer",
    "compliance.obligation.dismiss",
    "compliance.obligation.dispute",
    "compliance.library.manage",
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
            "Full authority over the organisation: approves filings and "
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
        }
        | _COMPLIANCE_APPROVER
        | {
            # Client-side sign-off and reopening a closed obligation both belong
            # to whoever is answerable for the filing, which is the owner.
            "compliance.obligation.approve",
            "compliance.obligation.reopen",
            "billing.view",
            "billing.subscription.manage",
        },
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
        }
        | _COMPLIANCE_APPROVER
        | {
            "compliance.obligation.reopen",
            "practice.work.manage",
            "practice.time.log",
            "practice.time.view_all",
            "practice.rates.manage",
            "practice.wip.view",
            "billing.view",
            "billing.subscription.manage",
            "billing.payment.record",
        },
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
                # Their own commission ledger and the accounts they manage.
                # Deliberately nothing from compliance: that needs an engagement.
                "dealers.commission.view",
                "dealers.account.manage",
                "billing.view",
            }
        ),
    ),
)
