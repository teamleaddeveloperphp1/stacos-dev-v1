"""
The system role bundles that ship with the product.

Roles are named sets of permission codes, never authority in their own right.
That distinction matters: the moment code asks "is this user a manager?" instead
of "may this user approve a return?", the same decision starts being made in
several places and disagreeing in one of them.

A role says *what* somebody may do. *Where* — which entities, which compliance
categories, which of a firm's clients — is the membership's scope, set per
person and independently of the role; ``default_categories`` below is only the
scope an invitation starts from. On a client's entity a firm member is further
capped by the engagement. See ``stacos.tenancy.scope_resolver``.

The bundles follow the maker-checker chain rather than read/write:

* **prepare** — doing the work and submitting it;
* **check** — reviewing someone else's work, recording the filing once it has
  been approved, closing it;
* **client sign-off** — the business taking responsibility for what is filed in
  its name. Organisation-only, and never grantable to a firm;
* **exceptions** — deferring, dismissing, disputing, reopening, and marking done
  without review. Each overrides the ordinary flow and is held by fewer people.

Defined here as data and applied by ``manage.py sync_system_roles`` rather than
in a data migration, so a permission added to a bundle reaches existing tenants
without editing migration history. The Roles & Permissions guide is generated
from this module (``manage.py roles_guide``), so the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stacos.tenancy.models import Tenant

__all__ = ["SYSTEM_ROLES", "RoleSpec", "system_role"]


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
    #: One line for the roles guide: who this is, in the customer's words.
    typical_holder: str = ""


# --- Shared bundles, so the same capability is spelled the same way twice -----

#: Every signed-in person's floor: seeing what they have been given access to,
#: and managing their own account. Grants nothing about anyone else, and writes
#: nothing — a viewer holds exactly this.
_READ_ONLY = frozenset(
    {
        "core.search",
        "accounts.security.manage",
        "accounts.profile.view",
        "tenancy.tenant.view",
        "tenancy.entity.view",
        "tenancy.profile.view",
        "engagements.view",
        # Seeing the calendar, and seeing what the law behind an entry says, is
        # the product's floor. A user who cannot read either has nothing to look
        # at, so both sit in the basic bundle rather than being granted upwards.
        "compliance.obligation.view",
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

#: Anyone who works on obligations talks about them. Not in the floor, because a
#: read-only auditor's access should be exactly that.
_PARTICIPANT = _READ_ONLY | {"compliance.obligation.comment"}

#: Keeping an entity's facts right: its profile (which decides what it owes),
#: its premises, registrations to read, and the audit trail to check changes by.
_ENTITY_STEWARD = _PARTICIPANT | {
    "tenancy.entity.edit",
    "tenancy.profile.edit",
    "tenancy.registration.view",
    "tenancy.premises.manage",
    "core.audit.view",
}

#: Doing the work and handing it over: preparing, chasing information, recording
#: the dates that unblock a due date, and submitting for review.
_PREPARE = frozenset(
    {
        "compliance.obligation.prepare",
        "compliance.obligation.request_info",
        "compliance.event.record",
    }
)

#: Checking someone else's work and seeing it through: review (never your own —
#: `apply_transition` enforces that), record the filing once approved, close it,
#: and run the calendar that decides what there is to do.
_CHECK = _PREPARE | {
    "compliance.obligation.assign",
    "compliance.obligation.review",
    "compliance.obligation.file",
    "compliance.obligation.close",
    "compliance.calendar.rebuild",
    "compliance.library.manage",
}

#: Overriding the ordinary flow for a reason that has to be written down.
_EXCEPTIONS = frozenset(
    {
        "compliance.obligation.defer",
        "compliance.obligation.dismiss",
        "compliance.obligation.dispute",
        "compliance.event.withdraw",
    }
)

#: The person answerable for the business or the firm: reopen a closed filing,
#: and — the one sanctioned exception to maker-checker — mark work done without
#: anyone reviewing it. Recorded as such on the timeline and in the audit trail.
_ANSWERABLE = frozenset(
    {
        "compliance.obligation.reopen",
        "compliance.obligation.complete_unreviewed",
    }
)

#: Deciding who is in the workspace and what they may do there.
_PEOPLE_AND_ACCESS = frozenset(
    {
        "accounts.user.view",
        "accounts.user.invite",
        "accounts.user.role.change",
        "accounts.user.deactivate",
        "accounts.role.manage",
    }
)


SYSTEM_ROLES: tuple[RoleSpec, ...] = (
    # -----------------------------------------------------------------------
    # Business — the organisation that owes the compliance
    # -----------------------------------------------------------------------
    RoleSpec(
        code="org-owner",
        name="Owner / Director",
        tenant_type=Tenant.Type.ORGANISATION,
        rank=10,
        typical_holder="A director, proprietor or partner of the business.",
        description=(
            "Answerable for the business: signs off filings as the client and "
            "manages people, access and billing. May mark work done without "
            "review, which is recorded as such."
        ),
        permissions=_ENTITY_STEWARD
        | _CHECK
        | _EXCEPTIONS
        | _ANSWERABLE
        | _PEOPLE_AND_ACCESS
        | {
            "tenancy.tenant.manage",
            "tenancy.entity.create",
            "tenancy.entity.archive",
            "tenancy.registration.manage",
            "engagements.invite",
            "engagements.grant",
            "engagements.revoke",
            "engagements.scope.edit",
            "core.audit.export",
            "core.data.export",
            "finance.view",
            # The client's sign-off. Only a business can hold it: see
            # `stacos.obligations.permissions_catalog`.
            "compliance.obligation.approve",
            "billing.view",
            "billing.subscription.manage",
        },
    ),
    RoleSpec(
        code="org-compliance-manager",
        name="Compliance Manager",
        tenant_type=Tenant.Type.ORGANISATION,
        rank=20,
        typical_holder="The in-house accountant, company secretary or compliance lead.",
        description=(
            "Runs the compliance calendar day to day: prepares, reviews colleagues' "
            "work, and records filings once approved. Cannot sign off as the "
            "client, skip review, or change who has access."
        ),
        # The checker for a business with no firm — without review and filing,
        # every return would queue on the owner. Not the client's sign-off, not
        # the exceptions, not people and access: that is the point of the role.
        permissions=_ENTITY_STEWARD
        | _CHECK
        | {
            "tenancy.entity.create",
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
        typical_holder="A plant, HR or admin colleague who owns a few specific compliances.",
        description=(
            "Prepares and evidences the compliances in their own areas — fire, "
            "safety, labour, licences — and submits them for review. Sees no "
            "amounts, and nothing outside their areas."
        ),
        # Prepares and submits; the compliance manager or owner checks. Not
        # assign, rebuild, close, profile or premises edits: each of those
        # changes what somebody else owes or has to do.
        #
        # Note the absence of `finance.view`: this user sees an obligation's
        # title and due date but every amount is masked.
        permissions=_PARTICIPANT | _PREPARE,
        default_categories=("SAFETY_FIRE", "LABOUR", "ENVIRONMENT", "LICENSING"),
    ),
    RoleSpec(
        code="org-viewer",
        name="Viewer",
        tenant_type=Tenant.Type.ORGANISATION,
        rank=50,
        typical_holder="A statutory auditor, investor, or board observer.",
        description=(
            "Read-only access to the calendar and the compliance profile, for "
            "auditors and observers. Cannot comment or change anything, and sees "
            "no amounts."
        ),
        permissions=_READ_ONLY | {"core.audit.view"},
    ),
    # -----------------------------------------------------------------------
    # Professional firm — works for clients, through engagements
    #
    # Everything below applies on a client's entities only as far as that
    # client's engagement allows. A firm never holds the client's sign-off.
    # -----------------------------------------------------------------------
    RoleSpec(
        code="practice-partner",
        name="Partner",
        tenant_type=Tenant.Type.PRACTICE,
        rank=10,
        typical_holder="A partner or proprietor of the CA / CS firm.",
        description=(
            "Answerable for the firm: the whole client book, reviews, utilisation, "
            "work in progress and billing. May mark work done without review "
            "where the client allows it, recorded as such."
        ),
        permissions=_ENTITY_STEWARD
        | _CHECK
        | _EXCEPTIONS
        | _ANSWERABLE
        | _PEOPLE_AND_ACCESS
        | {
            "tenancy.tenant.manage",
            "engagements.invite",
            "engagements.scope.edit",
            "core.audit.export",
            "core.data.export",
            "finance.view",
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
        typical_holder="A senior or manager who owns a set of clients.",
        description=(
            "Owns a set of clients: assigns work, reviews staff's work, sends it "
            "for the client's sign-off and records filings. Cannot skip review "
            "or reopen a closed filing."
        ),
        permissions=_ENTITY_STEWARD
        | _CHECK
        | _EXCEPTIONS
        | {
            "accounts.user.view",
            "engagements.invite",
            "finance.view",
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
        typical_holder="An article clerk, assistant or junior accountant.",
        description=(
            "Prepares the work assigned to them and submits it for review, asks "
            "clients for information, and logs their own time. Sees no amounts."
        ),
        permissions=_PARTICIPANT
        | _PREPARE
        | {"tenancy.registration.view"}
        # Their own cards and their own time. Not `practice.work.manage`:
        # deciding who does what is a manager's call. Not
        # `practice.time.view_all`: seeing everybody's hours is a management
        # view, not a peer-comparison tool. And not `practice.wip.view`: what the
        # work is worth is a partner's question.
        | {"practice.work.progress", "practice.time.log"},
    ),
    # -----------------------------------------------------------------------
    # Dealer — a channel partner that sells and provisions subscriptions
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
        typical_holder="The owner of the reseller business.",
        description=(
            "Manages the client accounts the dealer has sold, its own team, and "
            "its commission ledger. Sees no client compliance data."
        ),
        permissions=frozenset(
            {
                "core.search",
                "accounts.security.manage",
                "accounts.profile.view",
                "tenancy.tenant.view",
                "notifications.view",
                "notifications.preferences.manage",
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
        typical_holder="A sales or support person at the reseller.",
        description=(
            "Onboards and supports the dealer's client accounts. Sees no client "
            "compliance data and no commission."
        ),
        permissions=frozenset(
            {
                "core.search",
                "accounts.security.manage",
                "accounts.profile.view",
                "tenancy.tenant.view",
                "notifications.view",
                "notifications.preferences.manage",
                "dealers.account.manage",
            }
        ),
    ),
)


def system_role(code: str) -> RoleSpec | None:
    """The built-in definition behind a role code, or ``None`` for a custom role."""
    return next((spec for spec in SYSTEM_ROLES if spec.code == code), None)
