"""
Reach, enforced: what a member's scope and a firm's engagement actually stop.

Role and reach are separate. A role says what somebody may do; their membership's
scope and — for a firm — the client's engagement say where. Two gaps closed here
were both silent: a category limit that was computed and never applied to a
query, and an engagement that *added* to a firm member's role instead of capping
it. Each test below would have passed against neither.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from stacos.core.scope import _current, platform_scope, tenant_context
from stacos.engagements.models import Engagement
from stacos.engine.lifecycle import State
from stacos.obligations.models import ObligationInstance
from stacos.obligations.services import materialise
from stacos.obligations.transitions import TransitionError, apply_transition
from stacos.tenancy.models import ComplianceCategory, Entity, Membership, Tenant
from stacos.tenancy.scope_resolver import resolve_scope_for_membership
from tests.conftest import _make_member, sign_in

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 8, 12)


def _bind(membership: Membership) -> Any:
    scope = resolve_scope_for_membership(membership, reason="test")
    return _current.set(scope), scope


def _engage(practice: Tenant, entity: Entity, **fields: Any) -> Engagement:
    with platform_scope(reason="test-fixture"):
        Engagement.objects.filter(practice_tenant=practice, entity=entity).delete()
        return Engagement.objects.create(
            tenant=entity.tenant,
            practice_tenant=practice,
            entity=entity,
            status=Engagement.Status.ACTIVE,
            initiated_by_side=Engagement.Side.ORGANISATION,
            starts_on=date(2024, 4, 1),
            **fields,
        )


def _membership(user: Any) -> Membership:
    with platform_scope(reason="test"):
        return Membership.objects.select_related("tenant", "role").get(user=user)


@pytest.fixture
def partner(practice: Tenant) -> Any:
    return _make_member(
        practice, "partner@mehta.example", "Sunita Mehta", "+919800000211", "practice-partner"
    )


@pytest.fixture
def staff(practice: Tenant) -> Any:
    return _make_member(
        practice, "article@mehta.example", "Arjun Rao", "+919800000212", "practice-staff"
    )


# ---------------------------------------------------------------------------
# The engagement is a ceiling
# ---------------------------------------------------------------------------


def test_a_partner_cannot_file_where_the_client_only_allowed_viewing(
    practice: Tenant, partner: Any, materialised: Entity, an_obligation: ObligationInstance
) -> None:
    _engage(practice, materialised, permissions=["tenancy.entity.view"])
    token, scope = _bind(_membership(partner))
    try:
        assert "compliance.obligation.complete_unreviewed" in scope.permissions, "the role has it"
        assert "compliance.obligation.complete_unreviewed" not in scope.permissions_for(
            an_obligation.entity_id
        )
        with pytest.raises(TransitionError) as caught:
            apply_transition(
                an_obligation,
                target=State.FILED,
                actor=partner,
                permissions=scope.permissions,
                filing_reference="AA1",
            )
        assert caught.value.code == "forbidden"
    finally:
        _current.reset(token)


def test_an_engagement_does_not_raise_staff_to_a_partners_authority(
    practice: Tenant, staff: Any, materialised: Entity, an_obligation: ObligationInstance
) -> None:
    """The client trusting the firm with review does not make an article a reviewer."""
    _engage(
        practice,
        materialised,
        permissions=["compliance.obligation.prepare", "compliance.obligation.review"],
    )
    token, scope = _bind(_membership(staff))
    try:
        on_client = scope.permissions_for(an_obligation.entity_id)
        assert "compliance.obligation.prepare" in on_client
        assert "compliance.obligation.review" not in on_client
    finally:
        _current.reset(token)


def test_no_engagement_can_grant_the_clients_sign_off_or_administration(
    practice: Tenant, partner: Any, entity_a: Entity
) -> None:
    _engage(
        practice,
        entity_a,
        permissions=[
            "compliance.obligation.approve",
            "accounts.user.invite",
            "billing.subscription.manage",
            "tenancy.registration.manage",
        ],
    )
    token, scope = _bind(_membership(partner))
    try:
        on_client = scope.permissions_for(entity_a.id)
        for code in (
            "compliance.obligation.approve",
            "accounts.user.invite",
            "billing.subscription.manage",
            "tenancy.registration.manage",
        ):
            assert code not in on_client, code
        assert "compliance.obligation.view" in on_client, "the read floor always holds"
    finally:
        _current.reset(token)


def test_one_clients_engagement_does_not_reach_anothers_entity(
    practice: Tenant, partner: Any, entity_a: Entity, entity_b: Entity
) -> None:
    _engage(practice, entity_a, permissions=["compliance.obligation.file"])
    _engage(practice, entity_b, permissions=[])
    token, scope = _bind(_membership(partner))
    try:
        assert "compliance.obligation.file" in scope.permissions_for(entity_a.id)
        assert "compliance.obligation.file" not in scope.permissions_for(entity_b.id)
    finally:
        _current.reset(token)


def test_the_view_refuses_what_the_engagement_does_not_allow(
    client: Client, practice: Tenant, partner: Any, materialised: Entity
) -> None:
    _engage(practice, materialised, permissions=["compliance.obligation.prepare"])
    sign_in(client, partner)

    response = client.post(
        reverse("compliance:rebuild", args=[materialised.pk]), headers={"HX-Request": "true"}
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Category limits are applied to queries
# ---------------------------------------------------------------------------


def test_a_member_limited_to_direct_tax_sees_only_direct_tax(
    org: Tenant, materialised: Entity
) -> None:
    with platform_scope(reason="test"):
        categories = set(
            ObligationInstance.objects.filter(entity=materialised).values_list(
                "category", flat=True
            )
        )
    assert {ComplianceCategory.TAX_DIRECT, ComplianceCategory.TAX_INDIRECT} <= categories

    with tenant_context(
        tenant_ids=org.id, reason="test", categories=[ComplianceCategory.TAX_DIRECT]
    ):
        seen = set(ObligationInstance.objects.values_list("category", flat=True))
    assert seen == {ComplianceCategory.TAX_DIRECT}


def test_an_engagements_categories_limit_only_its_own_entity(
    org: Tenant, materialised: Entity, entity_a: Entity
) -> None:
    with tenant_context(
        tenant_ids=org.id,
        reason="test",
        entity_categories={materialised.id: frozenset({ComplianceCategory.TAX_INDIRECT})},
    ):
        seen = set(
            ObligationInstance.objects.filter(entity=materialised).values_list(
                "category", flat=True
            )
        )
    assert seen == {ComplianceCategory.TAX_INDIRECT}


def test_rebuilding_under_a_category_limit_does_not_duplicate_the_rest(
    org: Tenant, materialised: Entity
) -> None:
    """The planner sees the whole entity, or it would plan the hidden rows again."""
    with platform_scope(reason="test"):
        before = ObligationInstance.objects.filter(entity=materialised).count()

    with tenant_context(
        tenant_ids=org.id, reason="test", categories=[ComplianceCategory.TAX_DIRECT]
    ):
        materialise(materialised, as_of=AS_OF, trigger="MANUAL")

    with platform_scope(reason="test"):
        after = ObligationInstance.objects.filter(entity=materialised).count()
    assert after == before


def test_a_department_users_calendar_follows_their_areas(
    client: Client, org: Tenant, materialised: Entity
) -> None:
    """Invited with the role's defaults, a department user sees no tax filings."""
    user = _make_member(
        org, "hitesh@acme.example", "Hitesh Shah", "+919800000213", "org-department-user"
    )
    with platform_scope(reason="test"):
        Membership.objects.filter(user=user).update(
            categories=[ComplianceCategory.SAFETY_FIRE, ComplianceCategory.LABOUR]
        )
        gst = ObligationInstance.objects.filter(
            entity=materialised, category=ComplianceCategory.TAX_INDIRECT
        ).first()
    assert gst is not None
    sign_in(client, user)

    response = client.get(reverse("compliance:detail", args=[gst.pk]))

    assert response.status_code == 404
