"""
The system role bundles, held to what the Roles & Permissions guide promises.

The guide is what a customer reads before deciding who to invite as what, and it
is generated from these bundles. Each test here is one sentence of it that would
be a breach of trust if a later edit quietly stopped being true.
"""

from __future__ import annotations

import pytest

from stacos.core.permissions import permission_registry
from stacos.tenancy.models import Tenant
from stacos.tenancy.system_roles import SYSTEM_ROLES, RoleSpec, system_role

APPROVE = "compliance.obligation.approve"
UNREVIEWED = "compliance.obligation.complete_unreviewed"
ACCESS = {
    "accounts.user.invite",
    "accounts.user.role.change",
    "accounts.user.deactivate",
    "accounts.role.manage",
    "engagements.grant",
    "engagements.revoke",
}


def _grants(code: str) -> frozenset[str]:
    spec = system_role(code)
    assert spec is not None, code
    return permission_registry.expand(spec.permissions)


def test_every_role_in_the_guide_ships() -> None:
    assert {(spec.tenant_type, spec.code) for spec in SYSTEM_ROLES} == {
        (Tenant.Type.ORGANISATION, "org-owner"),
        (Tenant.Type.ORGANISATION, "org-compliance-manager"),
        (Tenant.Type.ORGANISATION, "org-department-user"),
        (Tenant.Type.ORGANISATION, "org-viewer"),
        (Tenant.Type.PRACTICE, "practice-partner"),
        (Tenant.Type.PRACTICE, "practice-manager"),
        (Tenant.Type.PRACTICE, "practice-staff"),
        (Tenant.Type.DEALER, "dealer-principal"),
        (Tenant.Type.DEALER, "dealer-staff"),
    }


@pytest.mark.parametrize("spec", SYSTEM_ROLES, ids=lambda spec: spec.code)
def test_every_role_names_only_registered_permissions(spec: RoleSpec) -> None:
    assert sorted(c for c in spec.permissions if c not in permission_registry) == []


@pytest.mark.parametrize("spec", SYSTEM_ROLES, ids=lambda spec: spec.code)
def test_every_role_holds_only_permissions_that_exist_for_its_kind_of_workspace(
    spec: RoleSpec,
) -> None:
    """A firm role holding the client's sign-off would be silently filtered at
    runtime — and the guide would still claim it. Catch it here instead."""
    misplaced = sorted(
        code
        for code in permission_registry.expand(spec.permissions)
        if spec.tenant_type not in permission_registry.get(code).tenant_types
    )
    assert misplaced == []


@pytest.mark.parametrize("spec", SYSTEM_ROLES, ids=lambda spec: spec.code)
def test_every_role_can_read_its_own_notifications(spec: RoleSpec) -> None:
    assert {"notifications.view", "notifications.preferences.manage"} <= _grants(spec.code)


# --- Business ---------------------------------------------------------------


def test_only_the_owner_signs_off_as_the_client() -> None:
    holders = {spec.code for spec in SYSTEM_ROLES if APPROVE in _grants(spec.code)}
    assert holders == {"org-owner"}


def test_only_the_owner_and_the_partner_may_skip_review() -> None:
    holders = {spec.code for spec in SYSTEM_ROLES if UNREVIEWED in _grants(spec.code)}
    assert holders == {"org-owner", "practice-partner"}


def test_the_compliance_manager_checks_but_cannot_sign_off_or_change_access() -> None:
    grants = _grants("org-compliance-manager")
    assert {"compliance.obligation.review", "compliance.obligation.file"} <= grants
    assert APPROVE not in grants
    assert UNREVIEWED not in grants
    assert "compliance.obligation.dismiss" not in grants
    assert not ACCESS & grants


def test_a_department_user_prepares_and_submits_and_nothing_more() -> None:
    grants = _grants("org-department-user")
    assert {"compliance.obligation.prepare", "compliance.event.record"} <= grants
    for code in (
        "compliance.obligation.assign",
        "compliance.obligation.review",
        "compliance.obligation.file",
        "compliance.obligation.close",
        "compliance.calendar.rebuild",
        "tenancy.profile.edit",
        "tenancy.premises.manage",
        "finance.view",
    ):
        assert code not in grants, code
    assert system_role("org-department-user").default_categories  # type: ignore[union-attr]


def test_a_viewer_changes_nothing_but_their_own_settings() -> None:
    own_settings = {"accounts.security.manage", "notifications.preferences.manage"}
    writes = {
        code
        for code in _grants("org-viewer") - own_settings
        if not code.endswith((".view", ".search"))
    }
    assert writes == set()


# --- Professional firm ------------------------------------------------------


def test_staff_prepare_but_do_not_assign_review_or_reshape_a_client() -> None:
    grants = _grants("practice-staff")
    assert {
        "compliance.obligation.prepare",
        "practice.work.progress",
        "practice.time.log",
    } <= grants
    for code in (
        "compliance.obligation.assign",
        "compliance.obligation.review",
        "compliance.calendar.rebuild",
        "tenancy.profile.edit",
        "practice.work.manage",
        "practice.time.view_all",
        "practice.wip.view",
        "finance.view",
    ):
        assert code not in grants, code


def test_a_manager_reviews_but_cannot_skip_review_or_reopen() -> None:
    grants = _grants("practice-manager")
    assert "compliance.obligation.review" in grants
    assert UNREVIEWED not in grants
    assert "compliance.obligation.reopen" not in grants


# --- Dealer -----------------------------------------------------------------


@pytest.mark.parametrize("code", ["dealer-principal", "dealer-staff"])
def test_a_dealer_holds_no_compliance_permission(code: str) -> None:
    grants = _grants(code)
    assert not {c for c in grants if c.startswith(("compliance.", "catalog.", "finance."))}
