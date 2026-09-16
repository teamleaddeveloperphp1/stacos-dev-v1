"""
State changes: the guards, the audit trail, and the suppression side effect.

Every transition has to do four things together or none of them — check the
guard, update the row, write the timeline entry, write the audit row. These
assert that it does.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from stacos.core.models import AuditAction, AuditLog
from stacos.core.scope import platform_scope
from stacos.engine.lifecycle import State
from stacos.obligations.models import ObligationEvent, ObligationInstance, ObligationSuppression
from stacos.obligations.transitions import (
    TransitionError,
    apply_transition,
    attach_acknowledgement,
    available_actions,
)

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 8, 12)
ALL = frozenset(
    {
        "compliance.obligation.view",
        "compliance.obligation.prepare",
        "compliance.obligation.request_info",
        "compliance.obligation.review",
        "compliance.obligation.approve",
        "compliance.obligation.file",
        "compliance.obligation.close",
        "compliance.obligation.reopen",
        "compliance.obligation.defer",
        "compliance.obligation.dismiss",
        "compliance.obligation.dispute",
    }
)


def test_a_legal_transition_updates_state_and_writes_both_records(
    an_obligation: ObligationInstance,
) -> None:
    """One call, four consequences.

    Splitting these across a view is how a state change ends up invisible to a
    regulator, which defeats the point of keeping the register.
    """
    with platform_scope(reason="test"):
        before_audit = AuditLog.objects.count()

        result = apply_transition(
            an_obligation,
            target=State.IN_PREPARATION,
            actor=None,
            permissions=ALL,
            as_of=AS_OF,
        )

        an_obligation.refresh_from_db()
        timeline = ObligationEvent.objects.filter(
            obligation=an_obligation, kind=ObligationEvent.Kind.TRANSITION
        )

        assert an_obligation.state == State.IN_PREPARATION
        assert result.transition.target == State.IN_PREPARATION
        assert timeline.count() == 1
        assert AuditLog.objects.count() == before_audit + 1
        assert AuditLog.objects.filter(action=AuditAction.TRANSITION).exists()


def test_an_illegal_transition_is_refused_kindly(an_obligation: ObligationInstance) -> None:
    """A stale page is a race, not a crash.

    Somebody else advanced the obligation between render and click. The message
    has to say so; a traceback would not help anyone.
    """
    with platform_scope(reason="test"), pytest.raises(TransitionError) as exc:
        apply_transition(
            an_obligation, target=State.CLOSED, actor=None, permissions=ALL, as_of=AS_OF
        )
    assert exc.value.code == "stale"


def test_a_transition_without_permission_is_refused(an_obligation: ObligationInstance) -> None:
    """The button being absent from the page does not make the URL absent."""
    with platform_scope(reason="test"), pytest.raises(TransitionError) as exc:
        apply_transition(
            an_obligation,
            target=State.IN_PREPARATION,
            actor=None,
            permissions=frozenset({"compliance.obligation.view"}),
            as_of=AS_OF,
        )
    assert exc.value.code == "forbidden"


def test_filing_requires_an_acknowledgement_number(an_obligation: ObligationInstance) -> None:
    """A filing with no reference cannot be evidenced during an assessment."""
    with platform_scope(reason="test"):
        apply_transition(
            an_obligation, target=State.IN_PREPARATION, actor=None, permissions=ALL, as_of=AS_OF
        )
        apply_transition(
            an_obligation, target=State.PENDING_REVIEW, actor=None, permissions=ALL, as_of=AS_OF
        )
        apply_transition(
            an_obligation, target=State.READY_TO_FILE, actor=None, permissions=ALL, as_of=AS_OF
        )

        with pytest.raises(TransitionError) as exc:
            apply_transition(
                an_obligation, target=State.FILED, actor=None, permissions=ALL, as_of=AS_OF
            )
        assert exc.value.code == "reference_required"

        apply_transition(
            an_obligation,
            target=State.FILED,
            actor=None,
            permissions=ALL,
            filing_reference="AA240526000123X",
            as_of=AS_OF,
        )
        an_obligation.refresh_from_db()

    assert an_obligation.state == State.FILED
    assert an_obligation.filing_reference == "AA240526000123X"
    assert an_obligation.filed_on == AS_OF


def test_reversing_a_filing_clears_its_evidence(an_obligation: ObligationInstance) -> None:
    """A stale acknowledgement number is worse than none."""
    with platform_scope(reason="test"):
        for target, extra in (
            (State.IN_PREPARATION, {}),
            (State.PENDING_REVIEW, {}),
            (State.READY_TO_FILE, {}),
            (State.FILED, {"filing_reference": "AA240526000123X"}),
        ):
            apply_transition(
                an_obligation, target=target, actor=None, permissions=ALL, as_of=AS_OF, **extra
            )

        apply_transition(
            an_obligation,
            target=State.READY_TO_FILE,
            actor=None,
            permissions=ALL,
            note="Wrong acknowledgement number entered.",
            as_of=AS_OF,
        )
        an_obligation.refresh_from_db()

    assert an_obligation.filed_on is None
    assert an_obligation.filing_reference == ""


def test_a_judgement_call_requires_a_reason(an_obligation: ObligationInstance) -> None:
    """Deferring overrides the engine. Somebody will ask why in eighteen months."""
    with platform_scope(reason="test"), pytest.raises(TransitionError) as exc:
        apply_transition(
            an_obligation, target=State.DEFERRED, actor=None, permissions=ALL, as_of=AS_OF
        )
    assert exc.value.code == "note_required"


def test_dismissing_records_a_suppression(an_obligation: ObligationInstance) -> None:
    """So the nightly rebuild does not bring it straight back."""
    with platform_scope(reason="test"):
        apply_transition(
            an_obligation,
            target=State.NOT_APPLICABLE,
            actor=None,
            permissions=ALL,
            note="We do not trade in this state any more.",
            as_of=AS_OF,
        )
        suppression = ObligationSuppression.objects.filter(
            entity_id=an_obligation.entity_id,
            definition_code=an_obligation.definition_code,
            period_key=an_obligation.period_key,
        ).first()

    assert suppression is not None
    assert suppression.kind == ObligationSuppression.Kind.NOT_APPLICABLE
    assert suppression.is_live


def test_reinstating_revokes_the_suppression(an_obligation: ObligationInstance) -> None:
    with platform_scope(reason="test"):
        apply_transition(
            an_obligation,
            target=State.NOT_APPLICABLE,
            actor=None,
            permissions=ALL,
            note="Dismissed in error.",
            as_of=AS_OF,
        )
        apply_transition(
            an_obligation,
            target=State.NOT_STARTED,
            actor=None,
            permissions=ALL,
            note="Reinstated after review.",
            as_of=AS_OF,
        )
        live = ObligationSuppression.objects.filter(
            entity_id=an_obligation.entity_id,
            definition_code=an_obligation.definition_code,
            period_key=an_obligation.period_key,
            revoked_at__isnull=True,
        ).exists()

    assert not live


def test_available_actions_are_filtered_by_permission(
    an_obligation: ObligationInstance,
) -> None:
    with platform_scope(reason="test"):
        everything = available_actions(an_obligation, permissions=ALL)
        nothing = available_actions(an_obligation, permissions=frozenset())

    assert everything
    assert not nothing


def test_a_superseded_obligation_cannot_be_worked_on(
    an_obligation: ObligationInstance,
) -> None:
    with platform_scope(reason="test"):
        an_obligation.supersede(reason="No longer applicable after a profile change.")

        assert available_actions(an_obligation, permissions=ALL) == ()
        with pytest.raises(TransitionError) as exc:
            apply_transition(
                an_obligation,
                target=State.IN_PREPARATION,
                actor=None,
                permissions=ALL,
                as_of=AS_OF,
            )
        assert exc.value.code == "superseded"


def _file_it(an_obligation: ObligationInstance) -> None:
    """Walk to FILED the same way the happy path does, so the evidence tests
    below start from a realistic state rather than a hand-set one."""
    for target, extra in (
        (State.IN_PREPARATION, {}),
        (State.PENDING_REVIEW, {}),
        (State.READY_TO_FILE, {}),
        (State.FILED, {"filing_reference": "AA240526000123X"}),
    ):
        apply_transition(
            an_obligation, target=target, actor=None, permissions=ALL, as_of=AS_OF, **extra
        )


def test_closing_is_refused_without_the_required_evidence(
    an_obligation: ObligationInstance,
) -> None:
    """GSTR-3B marks two evidence items ``mandatory_for_close``. Filing alone —
    an acknowledgement *number* — is not the same claim as having the document,
    and ``Close`` must not be gameable by typing a number with nothing attached.
    """
    with platform_scope(reason="test"):
        _file_it(an_obligation)

        assert all(
            move.target != State.CLOSED
            for move in available_actions(an_obligation, permissions=ALL)
        )

        with pytest.raises(TransitionError) as exc:
            apply_transition(
                an_obligation, target=State.CLOSED, actor=None, permissions=ALL, as_of=AS_OF
            )
        assert exc.value.code == "evidence_required"

        an_obligation.refresh_from_db()
        assert an_obligation.state == State.FILED


def test_closing_succeeds_once_the_evidence_is_attached(
    an_obligation: ObligationInstance,
) -> None:
    with platform_scope(reason="test"):
        _file_it(an_obligation)
        attach_acknowledgement(
            an_obligation,
            upload=SimpleUploadedFile(
                "ack.pdf", b"%PDF-1.4 acknowledgement", content_type="application/pdf"
            ),
            actor=None,
        )

        assert any(
            move.target == State.CLOSED
            for move in available_actions(an_obligation, permissions=ALL)
        )

        apply_transition(
            an_obligation, target=State.CLOSED, actor=None, permissions=ALL, as_of=AS_OF
        )
        an_obligation.refresh_from_db()

    assert an_obligation.state == State.CLOSED
