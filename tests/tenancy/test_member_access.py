"""
Role and reach, chosen together and changed together — and never confused.

An invitation carries the reach the inviter chose (or the role's default), so a
department user does not start with everything and get narrowed a day later.
The edit-access screen changes both, is audited with the before and after, and
refuses the two changes nobody could undo: your own, and removing the last
person who can manage access.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.models import AuditLog
from stacos.core.scope import platform_scope, tenant_context
from stacos.tenancy.access import AccessError, AccessGrant, set_member_access
from stacos.tenancy.invitations import accept_invitation, invite_colleague
from stacos.tenancy.models import ComplianceCategory, Entity, Membership, Role, Tenant
from tests.conftest import _make_member, _system_role, sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


def _role(code: str, tenant: Tenant) -> Role:
    with platform_scope(reason="test"):
        return _system_role(code, tenant.type)


def _membership(user: User) -> Membership:
    with platform_scope(reason="test"):
        return Membership.objects.select_related("tenant", "role", "user").get(user=user)


def _newcomer(email: str, phone: str) -> User:
    with platform_scope(reason="test"):
        return User.objects.create_user(
            email=email,
            password="test-password-12345",
            full_name="New Person",
            phone_e164=phone,
            email_verified=True,
            phone_verified=True,
        )


@pytest.fixture
def manager(org: Tenant) -> User:
    return _make_member(
        org, "ramesh@acme.example", "Ramesh Patel", "+919800000041", "org-compliance-manager"
    )


# ---------------------------------------------------------------------------
# Invitations carry reach
# ---------------------------------------------------------------------------


def test_an_invitation_starts_from_the_roles_default_areas(org: Tenant, org_owner: User) -> None:
    with tenant_context(tenant_ids=org.id, reason="test"):
        invitation, _raw = invite_colleague(
            org,
            email="plant@acme.example",
            role=_role("org-department-user", org),
            inviter=org_owner,
        )
        newcomer = _newcomer("plant@acme.example", "+919800000042")
        membership = accept_invitation(invitation, user=newcomer)

    assert set(membership.categories) == {
        ComplianceCategory.SAFETY_FIRE,
        ComplianceCategory.LABOUR,
        ComplianceCategory.ENVIRONMENT,
        ComplianceCategory.LICENSING,
    }


def test_an_invitation_carries_the_entities_and_areas_chosen(
    org: Tenant, org_owner: User, entity_a: Entity, entity_b: Entity
) -> None:
    with tenant_context(tenant_ids=org.id, reason="test"):
        invitation, _raw = invite_colleague(
            org,
            email="branch@acme.example",
            role=_role("org-compliance-manager", org),
            inviter=org_owner,
            grant=AccessGrant(
                all_entities=False,
                entity_ids=(entity_a.pk,),
                categories=(ComplianceCategory.TAX_INDIRECT,),
            ),
        )
        membership = accept_invitation(
            invitation, user=_newcomer("branch@acme.example", "+919800000043")
        )
        entities = set(membership.entities.values_list("pk", flat=True))

    assert membership.all_entities is False
    assert entities == {entity_a.pk}
    assert membership.categories == [ComplianceCategory.TAX_INDIRECT]


def test_an_invitation_cannot_name_another_workspaces_entity(
    org: Tenant, org_owner: User, rival_entity: Entity
) -> None:
    from stacos.tenancy.invitations import InvitationError

    with tenant_context(tenant_ids=org.id, reason="test"), pytest.raises(InvitationError):
        invite_colleague(
            org,
            email="x@acme.example",
            role=_role("org-viewer", org),
            inviter=org_owner,
            grant=AccessGrant(all_entities=False, entity_ids=(rival_entity.pk,)),
        )


# ---------------------------------------------------------------------------
# Changing access
# ---------------------------------------------------------------------------


def test_changing_access_is_audited_with_before_and_after(
    org: Tenant, org_owner: User, manager: User, entity_a: Entity
) -> None:
    membership = _membership(manager)
    with tenant_context(tenant_ids=org.id, reason="test"):
        set_member_access(
            membership,
            role=_role("org-viewer", org),
            grant=AccessGrant(all_entities=False, entity_ids=(entity_a.pk,)),
            actor=org_owner,
        )

    with platform_scope(reason="test"):
        row = AuditLog.objects.filter(object_id=str(membership.pk)).latest("occurred_at")
    assert row.before["role"] == "org-compliance-manager"
    assert row.after["role"] == "org-viewer"
    assert row.after["entities"] == [str(entity_a.pk)]


def test_you_cannot_change_your_own_access(org: Tenant, org_owner: User) -> None:
    with tenant_context(tenant_ids=org.id, reason="test"), pytest.raises(AccessError):
        set_member_access(
            _membership(org_owner),
            role=_role("org-viewer", org),
            grant=AccessGrant(),
            actor=org_owner,
        )


def test_the_last_person_who_can_manage_access_is_kept(
    org: Tenant, org_owner: User, manager: User
) -> None:
    """The manager cannot manage access, so demoting the only owner is refused."""
    with platform_scope(reason="test"):
        # A second owner exists only to make the change; then they are removed.
        second = _make_member(
            org, "second@acme.example", "Second Owner", "+919800000044", "org-owner"
        )
    with tenant_context(tenant_ids=org.id, reason="test"):
        set_member_access(
            _membership(second), role=_role("org-viewer", org), grant=AccessGrant(), actor=org_owner
        )
        with pytest.raises(AccessError):
            set_member_access(
                _membership(org_owner),
                role=_role("org-viewer", org),
                grant=AccessGrant(),
                actor=second,
            )


def test_the_edit_access_page_renders_both_ways(
    client: Client, org_owner: User, manager: User
) -> None:
    # Changing somebody's role is sensitive: fresh authentication first.
    sign_in(client, org_owner, step_up=True)
    url = reverse("app:team_member_access", args=[_membership(manager).pk])

    page = client.get(url)
    fragment = client.get(url, headers=HTMX)

    assert page.status_code == 200 and "<html" in page.content.decode().lower()
    assert fragment.status_code == 200 and "<html" not in fragment.content.decode().lower()
    assert "Permissions" in fragment.content.decode()
    assert "Compliance areas" not in fragment.content.decode(), "areas are not chosen on a form"


def test_saving_access_from_the_screen_changes_the_membership(
    client: Client, org_owner: User, manager: User
) -> None:
    # Changing somebody's role is sensitive: fresh authentication first.
    sign_in(client, org_owner, step_up=True)
    membership = _membership(manager)
    viewer = _role("org-viewer", membership.tenant)

    response = client.post(
        reverse("app:team_member_access", args=[membership.pk]),
        {"role": str(viewer.pk)},
        headers=HTMX,
    )

    assert response.status_code == 200
    membership = _membership(manager)
    assert membership.role.code == "org-viewer"
    assert membership.categories == [], "saving the role leaves areas as they were"


def test_a_compliance_manager_cannot_change_access(
    client: Client, org_owner: User, manager: User
) -> None:
    sign_in(client, manager)
    response = client.get(reverse("app:team_member_access", args=[_membership(org_owner).pk]))
    assert response.status_code == 403


def test_another_workspaces_member_is_a_404(
    client: Client, org_owner: User, rival_owner: User
) -> None:
    # Changing somebody's role is sensitive: fresh authentication first.
    sign_in(client, org_owner, step_up=True)
    response = client.get(reverse("app:team_member_access", args=[_membership(rival_owner).pk]))
    assert response.status_code == 404


def test_the_people_list_shows_reach_without_a_query_per_row(
    client: Client, org: Tenant, org_owner: User, manager: User, entity_a: Entity
) -> None:
    for index in range(4):
        _make_member(
            org, f"p{index}@acme.example", f"Person {index}", f"+91980000006{index}", "org-viewer"
        )
    sign_in(client, org_owner)

    with CaptureQueriesContext(connection) as few:
        client.get(reverse("app:team"), headers=HTMX)
    for index in range(4, 8):
        _make_member(
            org, f"p{index}@acme.example", f"Person {index}", f"+91980000006{index}", "org-viewer"
        )
    with CaptureQueriesContext(connection) as more:
        response = client.get(reverse("app:team"), headers=HTMX)

    assert "All entities" in response.content.decode()
    assert len(more.captured_queries) == len(few.captured_queries)


# ---------------------------------------------------------------------------
# The work board: staff move their own cards, managers move anyone's
# ---------------------------------------------------------------------------


def _work_item(practice: Tenant, assignee: Any) -> Any:
    from stacos.practice.models import WorkItem

    with tenant_context(tenant_ids=practice.id, reason="test"):
        return WorkItem.objects.create(
            tenant=practice, title="GSTR-3B August", assigned_to=assignee
        )


def test_staff_can_move_only_their_own_card(client: Client, practice: Tenant) -> None:
    staff = _make_member(
        practice, "article@mehta.example", "Arjun Rao", "+919800000071", "practice-staff"
    )
    colleague = _make_member(
        practice, "article2@mehta.example", "Meera Iyer", "+919800000072", "practice-staff"
    )
    mine, theirs = _work_item(practice, staff), _work_item(practice, colleague)
    sign_in(client, staff)

    ok = client.post(
        reverse("practice:work_move", args=[mine.pk]), {"state": "IN_PROGRESS"}, headers=HTMX
    )
    refused = client.post(
        reverse("practice:work_move", args=[theirs.pk]), {"state": "IN_PROGRESS"}, headers=HTMX
    )

    assert ok.status_code == 200
    assert refused.status_code == 403


def test_staff_cannot_create_work(client: Client, practice: Tenant) -> None:
    staff = _make_member(
        practice, "article@mehta.example", "Arjun Rao", "+919800000073", "practice-staff"
    )
    sign_in(client, staff)
    response = client.get(reverse("practice:work_create"), headers=HTMX)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Individual permissions on top of the role
# ---------------------------------------------------------------------------

from stacos.core.permissions import permission_registry  # noqa: E402
from stacos.tenancy.access import set_member_permissions  # noqa: E402


def _owner_permissions(org: Tenant) -> frozenset[str]:
    return permission_registry.expand(_role("org-owner", org).permissions)


def _ticked(membership: Membership) -> set[str]:
    return set(membership.resolved_permissions())


def test_the_role_dropdown_is_labelled_role(client: Client, org_owner: User) -> None:
    sign_in(client, org_owner)
    body = client.get(reverse("app:team"), headers=HTMX).content.decode()
    assert "What can they do?" not in body
    assert ">Role" in body


def test_the_member_page_ticks_what_the_role_gives(
    client: Client, org_owner: User, manager: User
) -> None:
    sign_in(client, org_owner, step_up=True)
    body = client.get(
        reverse("app:team_member_access", args=[_membership(manager).pk]), headers=HTMX
    ).content.decode()

    def box(code: str) -> str:
        found = re.search(rf'<input[^>]*value="{re.escape(code)}"[^>]*>', body)
        assert found, code
        return found.group(0)

    assert "checked" in box("compliance.obligation.review")
    assert "From role" in body
    # Something the owner could give but the role does not: offered, unticked.
    assert "checked" not in box("compliance.obligation.approve")


def test_removing_and_adding_single_permissions(
    client: Client, org: Tenant, org_owner: User, manager: User
) -> None:
    membership = _membership(manager)
    ticked = _ticked(membership) - {"compliance.obligation.review"}
    ticked.add("compliance.obligation.defer")
    sign_in(client, org_owner, step_up=True)

    response = client.post(
        reverse("app:team_member_permissions", args=[membership.pk]),
        {"permissions": sorted(ticked)},
        headers=HTMX,
    )

    assert response.status_code == 200
    membership = _membership(manager)
    assert membership.revoked_permissions == ["compliance.obligation.review"]
    assert membership.extra_permissions == ["compliance.obligation.defer"]
    assert "compliance.obligation.review" not in membership.resolved_permissions()
    assert "compliance.obligation.defer" in membership.resolved_permissions()
    body = response.content.decode()
    assert "Removed from role" in body and "Added" in body


def test_a_permission_brings_what_it_depends_on(
    org: Tenant, org_owner: User, manager: User
) -> None:
    membership = _membership(manager)
    with tenant_context(tenant_ids=org.id, reason="test"):
        set_member_permissions(
            membership,
            granted=["compliance.obligation.file"],
            actor=org_owner,
            actor_permissions=_owner_permissions(org),
        )
    assert "compliance.obligation.view" in _membership(manager).resolved_permissions()


def test_nobody_can_give_a_permission_they_do_not_hold(
    org: Tenant, org_owner: User, manager: User
) -> None:
    membership = _membership(manager)
    limited = _owner_permissions(org) - {"compliance.obligation.approve"}
    with tenant_context(tenant_ids=org.id, reason="test"), pytest.raises(AccessError):
        set_member_permissions(
            membership,
            granted=[*_ticked(membership), "compliance.obligation.approve"],
            actor=org_owner,
            actor_permissions=limited,
        )


def test_what_the_administrator_does_not_hold_is_left_alone(
    org: Tenant, org_owner: User, manager: User
) -> None:
    """Unticking a box you were never shown does not take it away."""
    membership = _membership(manager)
    without_review = _owner_permissions(org) - {"compliance.obligation.review"}
    with tenant_context(tenant_ids=org.id, reason="test"):
        set_member_permissions(
            membership,
            granted=["compliance.obligation.view"],
            actor=org_owner,
            actor_permissions=without_review,
        )
    assert "compliance.obligation.review" in _membership(manager).resolved_permissions()


def test_the_last_access_manager_keeps_the_permission(org: Tenant, org_owner: User) -> None:
    second = _make_member(org, "second@acme.example", "Second", "+919800000081", "org-owner")
    with tenant_context(tenant_ids=org.id, reason="test"):
        set_member_permissions(
            _membership(second),
            granted=["compliance.obligation.view"],
            actor=org_owner,
            actor_permissions=_owner_permissions(org),
        )
        with pytest.raises(AccessError):
            set_member_permissions(
                _membership(org_owner),
                granted=["compliance.obligation.view"],
                actor=second,
                actor_permissions=_owner_permissions(org),
            )


def test_a_new_role_resets_individual_changes(org: Tenant, org_owner: User, manager: User) -> None:
    membership = _membership(manager)
    with tenant_context(tenant_ids=org.id, reason="test"):
        set_member_permissions(
            membership,
            granted=[*_ticked(membership), "compliance.obligation.defer"],
            actor=org_owner,
            actor_permissions=_owner_permissions(org),
        )
        set_member_access(
            _membership(manager),
            role=_role("org-viewer", org),
            grant=AccessGrant(),
            actor=org_owner,
        )
    membership = _membership(manager)
    assert membership.extra_permissions == [] and membership.revoked_permissions == []


def test_permission_changes_are_audited(org: Tenant, org_owner: User, manager: User) -> None:
    membership = _membership(manager)
    with tenant_context(tenant_ids=org.id, reason="test"):
        set_member_permissions(
            membership,
            granted=[*_ticked(membership), "compliance.obligation.defer"],
            actor=org_owner,
            actor_permissions=_owner_permissions(org),
        )
    with platform_scope(reason="test"):
        row = AuditLog.objects.filter(object_id=str(membership.pk)).latest("occurred_at")
    assert row.context["change"] == "permissions"
    assert row.before["extra_permissions"] == []
    assert row.after["extra_permissions"] == ["compliance.obligation.defer"]


def test_marking_done_without_review_cannot_be_given_to_one_person(
    org: Tenant, org_owner: User, manager: User
) -> None:
    membership = _membership(manager)
    with tenant_context(tenant_ids=org.id, reason="test"):
        set_member_permissions(
            membership,
            granted=[*_ticked(membership), "compliance.obligation.complete_unreviewed"],
            actor=org_owner,
            actor_permissions=_owner_permissions(org),
        )
    assert (
        "compliance.obligation.complete_unreviewed"
        not in _membership(manager).resolved_permissions()
    )


# ---------------------------------------------------------------------------
# Auto-assignment only picks someone who can open the work
# ---------------------------------------------------------------------------


def test_a_step_is_not_auto_assigned_to_someone_who_cannot_reach_the_entity(
    org: Tenant, org_owner: User, an_obligation: Any, entity_a: Entity
) -> None:
    from stacos.obligations.transitions import _resolve_default_assignee

    # The earliest owner is limited to another entity; the next one reaches it.
    with platform_scope(reason="test"):
        first = Membership.objects.get(user=org_owner)
        first.all_entities = False
        first.save()
        first.entities.set([entity_a])
    second = _make_member(org, "second@acme.example", "Second", "+919800000091", "org-owner")

    with tenant_context(tenant_ids=org.id, reason="test"):
        chosen = _resolve_default_assignee(an_obligation, "org-owner", "REVIEW")
    assert chosen == second


def test_a_step_is_not_auto_assigned_to_someone_whose_permission_was_removed(
    org: Tenant, org_owner: User, an_obligation: Any
) -> None:
    from stacos.obligations.transitions import _resolve_default_assignee

    with platform_scope(reason="test"):
        Membership.objects.filter(user=org_owner).update(
            revoked_permissions=["compliance.obligation.review"]
        )
    with tenant_context(tenant_ids=org.id, reason="test"):
        assert _resolve_default_assignee(an_obligation, "org-owner", "REVIEW") is None


def test_you_can_see_your_own_permissions_but_not_change_them(
    client: Client, org_owner: User
) -> None:
    sign_in(client, org_owner, step_up=True)
    team = client.get(reverse("app:team"), headers=HTMX).content.decode()
    assert "View my access" in team

    body = client.get(
        reverse("app:team_member_access", args=[_membership(org_owner).pk]), headers=HTMX
    ).content.decode()
    review = re.search(r'<input[^>]*value="compliance.obligation.review"[^>]*>', body)
    assert review and "checked" in review.group(0) and "disabled" in review.group(0)
    assert "Save permissions" not in body
