"""
The Roles & Permissions guide, built from the role bundles rather than written beside them.

The guide is what a business reads before deciding who to invite as what. A
hand-written one drifted from the code within a week — it promised modules this
fork does not have and missed permissions it does — so this one is rendered from
``SYSTEM_ROLES``, the permission registry and the engagement allowlist, and a
test fails when the checked-in copy is stale.

What is written by hand here is the *grouping*: which permissions a customer
thinks of as one capability, and in what order. A test checks every permission a
role holds is covered, so a new permission cannot ship missing from the guide.

``manage.py roles_guide`` writes ``docs/roles-and-permissions.html`` — a single
self-contained page that prints cleanly to PDF.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.template.loader import render_to_string

from stacos.core.permissions import permission_registry
from stacos.engagements.grants import ENGAGEMENT_GRANTABLE, ENGAGEMENT_READ_FLOOR
from stacos.tenancy.models import ComplianceCategory, Tenant
from stacos.tenancy.system_roles import SYSTEM_ROLES, RoleSpec

__all__ = ["BASICS", "CAPABILITIES", "GUIDE_PATH", "Capability", "render_guide"]

GUIDE_PATH = Path(settings.BASE_DIR) / "docs" / "roles-and-permissions.html"
TEMPLATE = "docs/roles_guide.html"


@dataclass(frozen=True, slots=True)
class Capability:
    """One row of the comparison: something a customer thinks of as one thing."""

    label: str
    codes: tuple[str, ...]
    note: str = ""
    #: ``False`` when the permission exists but this version has no screen for it
    #: yet. Shown as such, so the guide never promises what the product lacks.
    available: bool = True


#: What every signed-in person has, stated once in prose rather than as a row of
#: ticks in every column.
BASICS: frozenset[str] = frozenset(
    {
        "core.search",
        "accounts.security.manage",
        "accounts.profile.view",
        "tenancy.tenant.view",
        "tenancy.entity.view",
        "tenancy.profile.view",
        "catalog.view",
        "notifications.view",
        "notifications.preferences.manage",
    }
)

CAPABILITIES: tuple[tuple[str, tuple[Capability, ...]], ...] = (
    (
        "Seeing",
        (
            Capability(
                "See the compliance calendar and library",
                ("compliance.obligation.view", "compliance.library.view"),
            ),
            Capability(
                "See amounts, tax values and fees",
                ("finance.view",),
                "Without it, titles and due dates show and every amount is masked.",
            ),
            Capability("Comment on an obligation", ("compliance.obligation.comment",)),
            Capability("See registrations and tax IDs", ("tenancy.registration.view",)),
            Capability("See the audit trail", ("core.audit.view",), available=False),
        ),
    ),
    (
        "Preparing",
        (
            Capability(
                "Prepare a filing and submit it for review",
                ("compliance.obligation.prepare",),
            ),
            Capability("Ask for information", ("compliance.obligation.request_info",)),
            Capability(
                "Record event dates (AGM, licence issue)",
                ("compliance.event.record",),
                "Dates that unblock a due date the calendar cannot compute alone.",
            ),
            Capability("Assign obligations to people", ("compliance.obligation.assign",)),
        ),
    ),
    (
        "Checking",
        (
            Capability(
                "Review a colleague's work",
                ("compliance.obligation.review",),
                "Never your own: whoever submitted it for review cannot pass it.",
            ),
            Capability(
                "Record a filing once it is approved",
                ("compliance.obligation.file",),
                "With the acknowledgement number. Asks you to re-confirm who you are.",
            ),
            Capability("Close a completed obligation", ("compliance.obligation.close",)),
        ),
    ),
    (
        "Client sign-off",
        (
            Capability(
                "Sign off a filing as the client",
                ("compliance.obligation.approve",),
                "The business taking responsibility for what is filed in its name. "
                "A firm can never hold it.",
            ),
        ),
    ),
    (
        "Exceptions — each needs a reason, and is recorded",
        (
            Capability(
                "Defer, dispute, or mark not applicable",
                (
                    "compliance.obligation.defer",
                    "compliance.obligation.dispute",
                    "compliance.obligation.dismiss",
                ),
            ),
            Capability("Withdraw a recorded event date", ("compliance.event.withdraw",)),
            Capability("Reopen a closed filing", ("compliance.obligation.reopen",)),
            Capability(
                "Mark done without review",
                ("compliance.obligation.complete_unreviewed",),
                "The one exception to maker-checker. The timeline and audit trail "
                "record it as completed without review.",
            ),
        ),
    ),
    (
        "Entities and the calendar",
        (
            Capability(
                "Edit an entity, its premises and its compliance profile",
                ("tenancy.entity.edit", "tenancy.premises.manage", "tenancy.profile.edit"),
                "The profile decides what the entity owes.",
            ),
            Capability(
                "Rebuild the calendar; add or remove library items",
                ("compliance.calendar.rebuild", "compliance.library.manage"),
            ),
            Capability("Add an entity", ("tenancy.entity.create",)),
            Capability("Archive an entity", ("tenancy.entity.archive",)),
            Capability("Manage registrations and tax IDs", ("tenancy.registration.manage",)),
            Capability("Change organisation settings", ("tenancy.tenant.manage",)),
        ),
    ),
    (
        "People and access",
        (
            Capability(
                "Invite people, choosing their role and reach",
                ("accounts.user.invite", "accounts.user.view"),
            ),
            Capability(
                "Change someone's role, reach and individual permissions",
                ("accounts.user.role.change",),
                "Add or remove single permissions on top of the role — only ones you "
                "hold yourself. Never your own, and never the last person who can.",
            ),
            Capability("Deactivate a user", ("accounts.user.deactivate",), available=False),
            Capability("Build custom roles", ("accounts.role.manage",), available=False),
        ),
    ),
    (
        "Firms and clients",
        (
            Capability("See engagements", ("engagements.view",), available=False),
            Capability("Invite a firm, or a client", ("engagements.invite",), available=False),
            Capability(
                "Change what an engagement covers",
                ("engagements.scope.edit",),
                available=False,
            ),
            Capability(
                "Grant or end a firm's access",
                ("engagements.grant", "engagements.revoke"),
                available=False,
            ),
        ),
    ),
    (
        "The firm's own work",
        (
            Capability("See the work board", ("practice.work.view",)),
            Capability(
                "Move your own work along; log your time",
                ("practice.work.progress", "practice.time.log"),
            ),
            Capability("Create and assign work", ("practice.work.manage",)),
            Capability("See everybody's time", ("practice.time.view_all",)),
            Capability("See work-in-progress value and profitability", ("practice.wip.view",)),
            Capability("Set charge-out rates", ("practice.rates.manage",), available=False),
        ),
    ),
    (
        "Billing and data",
        (
            Capability("See the subscription and invoices", ("billing.view",)),
            Capability("Record a payment", ("billing.payment.record",)),
            Capability("Change the plan", ("billing.subscription.manage",), available=False),
            Capability(
                "Export the audit trail or all data",
                ("core.audit.export", "core.data.export"),
                available=False,
            ),
        ),
    ),
    (
        "Dealer",
        (
            Capability(
                "Manage the client accounts the dealer sold",
                ("dealers.account.manage",),
                available=False,
            ),
            Capability("See the commission ledger", ("dealers.commission.view",)),
        ),
    ),
)

_GROUPS: tuple[tuple[str, str, str], ...] = (
    (
        Tenant.Type.ORGANISATION,
        "Business",
        "The organisation that owes the compliance. Its people prepare, check and "
        "sign off; it decides which firm may work on which of its entities.",
    ),
    (
        Tenant.Type.PRACTICE,
        "Professional firm",
        "A CA or CS firm working for clients. On a client's entity a firm member may "
        "do what their role allows and the client's engagement allows — never more "
        "than either.",
    ),
    (
        Tenant.Type.DEALER,
        "Dealer",
        "A channel partner that sells and supports subscriptions. It never sees a "
        "client's compliance, financial or document data.",
    ),
)


def _label(code: str) -> str:
    return permission_registry.get(code).label if code in permission_registry else code


def _holders(roles: list[RoleSpec], grants: dict[str, frozenset[str]], code: str) -> list[str]:
    return [spec.name for spec in roles if code in grants[spec.code]]


def guide_context() -> dict[str, Any]:
    """Everything the template needs, derived from the code."""
    roles = list(SYSTEM_ROLES)
    grants = {spec.code: permission_registry.expand(spec.permissions) for spec in roles}
    categories = dict(ComplianceCategory.choices)

    groups = [
        {
            "title": title,
            "intro": intro,
            "roles": [
                {
                    "name": spec.name,
                    "holder": spec.typical_holder,
                    "description": spec.description,
                    "default_areas": [str(categories[c]) for c in spec.default_categories],
                }
                for spec in roles
                if spec.tenant_type == tenant_type
            ],
        }
        for tenant_type, title, intro in _GROUPS
    ]

    header = [
        {"title": title, "span": sum(1 for spec in roles if spec.tenant_type == tenant_type)}
        for tenant_type, title, _intro in _GROUPS
    ]

    sections = [
        {
            "title": title,
            "rows": [
                {
                    "label": capability.label,
                    "note": capability.note,
                    "available": capability.available,
                    "cells": [
                        {
                            "role": spec.name,
                            "yes": all(code in grants[spec.code] for code in capability.codes),
                            "group_start": index == 0
                            or roles[index - 1].tenant_type != spec.tenant_type,
                        }
                        for index, spec in enumerate(roles)
                    ],
                }
                for capability in rows
            ],
        }
        for title, rows in CAPABILITIES
    ]

    workflow = [
        {
            "step": "Prepare",
            "what": "Do the work and submit it for review.",
            "who": _holders(roles, grants, "compliance.obligation.prepare"),
        },
        {
            "step": "Review",
            "what": "A colleague — never the preparer — checks it and approves it for "
            "filing, or sends it for the client's sign-off.",
            "who": _holders(roles, grants, "compliance.obligation.review"),
        },
        {
            "step": "Client sign-off",
            "what": "When the firm asks for it, the business signs off in its own name.",
            "who": _holders(roles, grants, "compliance.obligation.approve"),
        },
        {
            "step": "Record filing",
            "what": "Enter the submission date and acknowledgement number, then close.",
            "who": _holders(roles, grants, "compliance.obligation.file"),
        },
    ]

    appendix = [
        {
            "category": category,
            "permissions": [
                {
                    "label": permission.label,
                    "code": permission.code,
                    "description": permission.description,
                    "sensitive": permission.is_sensitive,
                    "holders": _holders(roles, grants, permission.code),
                }
                for permission in sorted(
                    (p for p in permission_registry if p.category == category),
                    key=lambda p: p.label,
                )
                if any(permission.code in held for held in grants.values())
            ],
        }
        for category in sorted({p.category for p in permission_registry})
    ]

    return {
        "groups": groups,
        "header": header,
        "roles": [spec.name for spec in roles],
        "sections": sections,
        "workflow": workflow,
        "unreviewed_holders": _holders(roles, grants, "compliance.obligation.complete_unreviewed"),
        "basics": sorted(_label(code) for code in BASICS),
        "engagement_floor": sorted(_label(code) for code in ENGAGEMENT_READ_FLOOR),
        "engagement_grantable": sorted(
            _label(code) for code in ENGAGEMENT_GRANTABLE - ENGAGEMENT_READ_FLOOR
        ),
        "department_areas": [
            str(categories[c])
            for spec in roles
            if spec.code == "org-department-user"
            for c in spec.default_categories
        ],
        "appendix": [section for section in appendix if section["permissions"]],
    }


def render_guide() -> str:
    """The guide as one self-contained HTML page."""
    return render_to_string(TEMPLATE, guide_context())
