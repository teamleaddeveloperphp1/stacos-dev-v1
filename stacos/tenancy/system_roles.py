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

#: Reading the things a compliance user reads. Bundled with the view basics
#: because a calendar you cannot attach a document to, or whose notices you
#: cannot see, is not a compliance product.
_DOCUMENT_AND_TRACKER_BASICS = frozenset(
    {
        "vault.document.view",
        "vault.document.download",
        "rfi.request.view",
        "notices.notice.view",
        "returns.preparation.view",
        "secretarial.view",
    }
)

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
        "catalog.view",
        # Notifications are addressed to one person and grant nothing about
        # anyone else, so they sit in the floor bundle. A user who can sign in
        # but cannot see what the platform has been telling them — or change how
        # loudly it does so — is a user who will turn the product off at their
        # mail client instead.
        "notifications.view",
        "notifications.preferences.manage",
    }
    | _DOCUMENT_AND_TRACKER_BASICS
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
        "vault.document.upload",
        "rfi.request.create",
        "rfi.request.send",
        "rfi.request.review",
        "rfi.request.close",
        "notices.notice.create",
        "notices.notice.edit",
        "notices.notice.assign",
        "returns.preparation.prepare",
        "returns.reconciliation.run",
        "secretarial.meeting.manage",
        "secretarial.resolution.manage",
        "secretarial.register.manage",
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
    "notices.notice.respond",
    "notices.notice.close",
    "vault.document.delete",
    # The checker half of maker-checker. Deliberately NOT in the preparer bundle:
    # a preparer holding both would satisfy the permission check and still be
    # refused by the service and the database, which is the right answer but a
    # confusing way to discover the rule.
    "returns.preparation.review",
    "returns.preparation.file",
    "secretarial.minutes.sign",
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
        }
        | _COMPLIANCE_APPROVER
        | {
            # Client-side sign-off and reopening a closed obligation both belong
            # to whoever is answerable for the filing, which is the owner.
            "compliance.obligation.approve",
            "compliance.obligation.reopen",
            # The client answers requests. A practice user never holds this:
            # nobody should be able to satisfy their own outstanding item.
            "rfi.request.respond",
            "vault.document.export",
            "returns.preparation.approve",
            # Who owns what is the owner's business, and nobody else's by default.
            "secretarial.captable.view",
            "secretarial.captable.manage",
            "billing.view",
            "billing.subscription.manage",
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
        }
        # Prepares and chases, but does not approve — that is the whole point of
        # the role, and the reason `_COMPLIANCE_APPROVER` is not used here.
        | _COMPLIANCE_PREPARER
        | {"compliance.obligation.close", "rfi.request.respond"},
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
        permissions=_VIEW_BASICS
        | {"tenancy.premises.manage"}
        | _COMPLIANCE_PREPARER
        | {"compliance.obligation.close", "rfi.request.respond"},
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
        }
        | _COMPLIANCE_APPROVER
        | {
            "practice.work.manage",
            "practice.time.log",
            "practice.time.view_all",
            "practice.wip.view",
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
        permissions=_VIEW_BASICS
        | {"tenancy.profile.edit", "tenancy.registration.view"}
        | _COMPLIANCE_PREPARER
        # The board, their own cards, and their own time. Not
        # `practice.time.view_all`: seeing everybody's hours is a management
        # view, not a peer-comparison tool. And not `practice.wip.view`: what the
        # work is worth is a partner's question.
        | {"practice.work.view", "practice.work.manage", "practice.time.log"},
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
    RoleSpec(
        code="dealer-staff",
        name="Dealer Staff",
        tenant_type=Tenant.Type.DEALER,
        rank=30,
        description="Onboards and supports client accounts. No compliance data.",
        permissions=frozenset(
            {
                "core.search",
                "accounts.security.manage",
                "tenancy.tenant.view",
                "dealers.account.manage",
            }
        ),
    ),
)
