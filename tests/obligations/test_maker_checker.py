"""
Preparation, review and client sign-off, kept apart.

The register is only worth defending in an assessment if it can show that the
person who prepared a filing is not the person who passed it, and that anything
recorded without a review says so on its face. These tests hold four promises:

1. A filing is recorded through review — ``READY_TO_FILE → FILED`` — unless the
   actor holds the one explicit exception, ``complete_unreviewed``.
2. The exception is recorded as such, on the timeline and in the audit trail.
3. Whoever submitted work for review cannot approve it, or sign it off as the
   client. Sending it back is still theirs to do.
4. The firm's review and the client's sign-off are different acts, held by
   different permissions, and a firm can never hold the second.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.models import AuditLog
from stacos.core.scope import platform_scope, tenant_context
from stacos.engine.lifecycle import State, transition_for
from stacos.obligations.models import ObligationEvent, ObligationInstance, ObligationStep
from stacos.obligations.transitions import (
    TransitionError,
    apply_transition,
    available_actions,
    complete_step,
    ensure_steps,
)
from stacos.tenancy.models import Tenant
from stacos.tenancy.system_roles import system_role
from tests.conftest import _make_member, sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


def _grants(code: str) -> frozenset[str]:
    from stacos.core.permissions import permission_registry

    spec = system_role(code)
    assert spec is not None
    return permission_registry.expand(spec.permissions)


OWNER = _grants("org-owner")
MANAGER = _grants("org-compliance-manager")


@pytest.fixture
def compliance_manager(org: Tenant) -> User:
    return _make_member(
        org, "ramesh@acme.example", "Ramesh Patel", "+919800000031", "org-compliance-manager"
    )


def _move(
    obligation: ObligationInstance, target: str, actor: Any, permissions: frozenset[str], **kw: Any
) -> None:
    with tenant_context(tenant_ids=obligation.tenant_id, reason="test:move"):
        apply_transition(obligation, target=target, actor=actor, permissions=permissions, **kw)


def _to_review(obligation: ObligationInstance, maker: User) -> None:
    _move(obligation, State.IN_PREPARATION, maker, MANAGER)
    _move(obligation, State.PENDING_REVIEW, maker, MANAGER)


# ---------------------------------------------------------------------------
# 1 and 2 — the reviewed route, and the recorded exception
# ---------------------------------------------------------------------------


def test_the_lifecycle_records_a_filing_only_after_approval() -> None:
    assert transition_for(State.READY_TO_FILE, State.FILED).permission == (
        "compliance.obligation.file"
    )
    for source in (State.NOT_STARTED, State.IN_PREPARATION, State.PENDING_REVIEW):
        move = transition_for(source, State.FILED)
        assert move is not None
        assert move.permission == "compliance.obligation.complete_unreviewed"
        assert move.skips_review


def test_recording_a_filing_before_review_is_refused_to_a_compliance_manager(
    an_obligation: ObligationInstance, compliance_manager: User
) -> None:
    with pytest.raises(TransitionError) as caught:
        _move(
            an_obligation,
            State.FILED,
            compliance_manager,
            MANAGER,
            filing_reference="AA1",
            filed_on=date(2026, 8, 10),
        )
    assert caught.value.code == "forbidden"


def test_marking_done_without_review_is_recorded_as_exactly_that(
    an_obligation: ObligationInstance, org_owner: User
) -> None:
    _move(
        an_obligation,
        State.FILED,
        org_owner,
        OWNER,
        filing_reference="AA240810123456X",
        filed_on=date(2026, 8, 10),
    )

    with platform_scope(reason="test"):
        event = ObligationEvent.objects.filter(obligation=an_obligation, to_state=State.FILED).get()
        audit = AuditLog.objects.filter(
            object_id=str(an_obligation.pk), after__state=State.FILED
        ).get()
    assert event.context["completed_without_review"] is True
    assert event.context["label"] == "Mark done without review"
    assert audit.context["completed_without_review"] is True


def test_a_reviewed_filing_is_not_flagged(
    an_obligation: ObligationInstance, org_owner: User, compliance_manager: User
) -> None:
    _to_review(an_obligation, compliance_manager)
    _move(an_obligation, State.READY_TO_FILE, org_owner, OWNER)
    _move(
        an_obligation,
        State.FILED,
        compliance_manager,
        MANAGER,
        filing_reference="AA1",
        filed_on=date(2026, 8, 10),
    )

    with platform_scope(reason="test"):
        event = ObligationEvent.objects.filter(obligation=an_obligation, to_state=State.FILED).get()
    assert event.context["completed_without_review"] is False


# ---------------------------------------------------------------------------
# 3 — the checker is not the maker
# ---------------------------------------------------------------------------


def test_the_person_who_submitted_it_cannot_approve_it(
    an_obligation: ObligationInstance, org_owner: User
) -> None:
    """Even an owner, who holds every permission, cannot pass their own work."""
    _move(an_obligation, State.IN_PREPARATION, org_owner, OWNER)
    _move(an_obligation, State.PENDING_REVIEW, org_owner, OWNER)

    with pytest.raises(TransitionError) as caught:
        _move(an_obligation, State.READY_TO_FILE, org_owner, OWNER)
    assert caught.value.code == "four_eyes"


def test_a_colleague_can_approve_it(
    an_obligation: ObligationInstance, org_owner: User, compliance_manager: User
) -> None:
    _to_review(an_obligation, compliance_manager)
    _move(an_obligation, State.READY_TO_FILE, org_owner, OWNER)

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
    assert an_obligation.state == State.READY_TO_FILE


def test_the_submitter_may_still_take_their_own_work_back(
    an_obligation: ObligationInstance, org_owner: User
) -> None:
    _move(an_obligation, State.IN_PREPARATION, org_owner, OWNER)
    _move(an_obligation, State.PENDING_REVIEW, org_owner, OWNER)
    _move(an_obligation, State.IN_PREPARATION, org_owner, OWNER, note="Wrong period")

    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
    assert an_obligation.state == State.IN_PREPARATION


def test_the_submitter_cannot_sign_it_off_as_the_client(
    an_obligation: ObligationInstance, org_owner: User, compliance_manager: User
) -> None:
    _move(an_obligation, State.IN_PREPARATION, org_owner, OWNER)
    _move(an_obligation, State.PENDING_REVIEW, org_owner, OWNER)
    _move(an_obligation, State.PENDING_CLIENT_APPROVAL, compliance_manager, MANAGER)

    with pytest.raises(TransitionError) as caught:
        _move(an_obligation, State.READY_TO_FILE, org_owner, OWNER)
    assert caught.value.code == "four_eyes"


def test_the_submitter_is_not_offered_the_approval(
    an_obligation: ObligationInstance, org_owner: User, compliance_manager: User
) -> None:
    _to_review(an_obligation, compliance_manager)

    with tenant_context(tenant_ids=an_obligation.tenant_id, reason="test"):
        an_obligation.refresh_from_db()
        mine = {
            m.target
            for m in available_actions(an_obligation, permissions=OWNER, actor=compliance_manager)
        }
        theirs = {
            m.target for m in available_actions(an_obligation, permissions=OWNER, actor=org_owner)
        }

    assert State.READY_TO_FILE not in mine
    assert State.IN_PREPARATION in mine, "sending it back stays available"
    assert State.READY_TO_FILE in theirs


def test_the_checklist_applies_the_same_rule(
    an_obligation: ObligationInstance, org_owner: User, compliance_manager: User
) -> None:
    with tenant_context(tenant_ids=an_obligation.tenant_id, reason="test"):
        steps = {step.role: step for step in ensure_steps(an_obligation)}
        complete_step(
            steps[ObligationStep.Role.PREPARE], actor=compliance_manager, permissions=MANAGER
        )

        with pytest.raises(TransitionError) as caught:
            complete_step(
                steps[ObligationStep.Role.REVIEW], actor=compliance_manager, permissions=MANAGER
            )
        assert caught.value.code == "four_eyes"

        complete_step(steps[ObligationStep.Role.REVIEW], actor=org_owner, permissions=OWNER)


# ---------------------------------------------------------------------------
# 4 — the client's sign-off is the client's
# ---------------------------------------------------------------------------


def test_sign_off_and_review_are_different_permissions() -> None:
    review = transition_for(State.PENDING_REVIEW, State.PENDING_CLIENT_APPROVAL)
    sign_off = transition_for(State.PENDING_CLIENT_APPROVAL, State.READY_TO_FILE)
    assert review is not None and sign_off is not None
    assert review.permission == "compliance.obligation.review"
    assert sign_off.permission == "compliance.obligation.approve"
    assert sign_off.label == "Sign off as the client"


@pytest.mark.parametrize("code", ["practice-partner", "practice-manager", "practice-staff"])
def test_no_firm_role_holds_the_clients_sign_off(code: str) -> None:
    assert "compliance.obligation.approve" not in _grants(code)


def test_a_firm_membership_cannot_be_given_sign_off_by_hand(
    practice_membership: Any,
) -> None:
    with platform_scope(reason="test"):
        practice_membership.extra_permissions = ["compliance.obligation.approve"]
        practice_membership.save()
        assert "compliance.obligation.approve" not in practice_membership.resolved_permissions()


# ---------------------------------------------------------------------------
# The web: whoever cannot skip review works the chain; the owner keeps one question
# ---------------------------------------------------------------------------


def _detail(obligation: ObligationInstance) -> str:
    return reverse("compliance:detail", args=[obligation.pk])


def _transition(obligation: ObligationInstance) -> str:
    return reverse("compliance:transition", args=[obligation.pk])


def test_a_compliance_manager_works_through_review(
    client: Client, an_obligation: ObligationInstance, compliance_manager: User
) -> None:
    sign_in(client, compliance_manager)
    client.post(_transition(an_obligation), {"target": State.IN_PREPARATION}, headers=HTMX)

    body = client.get(_detail(an_obligation)).content.decode()
    assert "Submit for review" in body
    assert "Yes, submitted" not in body, "nothing to record until it has been approved"

    client.post(_transition(an_obligation), {"target": State.PENDING_REVIEW}, headers=HTMX)
    body = client.get(_detail(an_obligation)).content.decode()
    assert "a colleague has to review and approve it" in body
    assert "Approve for filing" not in body


def test_an_owner_keeps_the_single_question(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    body = signed_in.get(_detail(an_obligation)).content.decode()
    assert "Yes, submitted" in body
    assert "Record it as done without review" in body
    assert "Submit for review" not in body


def test_marking_done_without_review_asks_the_owner_to_reconfirm(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    signed_in.post(
        reverse("compliance:status", args=[an_obligation.pk]),
        {"answer": "yes", "filed_on": "2026-08-10", "filing_reference": "AA1"},
        headers=HTMX,
    )
    with platform_scope(reason="test"):
        an_obligation.refresh_from_db()
    assert an_obligation.state == State.NOT_STARTED


@pytest.mark.usefixtures("stepped_up")
def test_the_timeline_says_it_was_completed_without_review(
    signed_in: Client, an_obligation: ObligationInstance
) -> None:
    signed_in.post(
        reverse("compliance:status", args=[an_obligation.pk]),
        {"answer": "yes", "filed_on": "2026-08-10", "filing_reference": "AA1"},
        headers=HTMX,
    )
    body = signed_in.get(_detail(an_obligation)).content.decode()
    assert "Completed without review" in body
