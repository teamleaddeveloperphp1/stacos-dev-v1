"""
Moving an obligation through its lifecycle.

One function does every state change, because a transition is four things that
have to happen together or not at all: the guard is checked, the row is updated,
the timeline gains an entry, and the audit log gains a row. Scattering those
across views produces state changes that are invisible to a regulator, which
defeats the purpose of the register.

The transition table itself is pure Python in
:mod:`stacos.engine.lifecycle`. This module is the part that needs a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.engine.lifecycle import State, Transition, allowed_transitions, transition_for
from stacos.obligations.models import ObligationEvent, ObligationInstance, ObligationSuppression

__all__ = ["TransitionError", "TransitionResult", "apply_transition", "available_actions"]


class TransitionError(Exception):
    """A transition that cannot be made, with a message fit to show a user."""

    def __init__(self, message: str, *, code: str = "illegal") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TransitionResult:
    obligation: ObligationInstance
    transition: Transition
    event: ObligationEvent


def available_actions(
    obligation: ObligationInstance,
    *,
    permissions: frozenset[str],
) -> tuple[Transition, ...]:
    """Moves this user could make on this obligation, right now.

    Used to render the action menu. Filtering here is a courtesy to the user, not
    a security boundary — :func:`apply_transition` re-checks everything, because a
    button that is merely absent from the page is not absent from the network.
    """
    if obligation.is_superseded or obligation.is_archived:
        return ()
    return allowed_transitions(obligation.state, permissions=permissions)


@transaction.atomic
def apply_transition(
    obligation: ObligationInstance,
    *,
    target: str,
    actor: Any,
    permissions: frozenset[str],
    note: str = "",
    filing_reference: str = "",
    filed_on: date | None = None,
    as_of: date | None = None,
) -> TransitionResult:
    """Move an obligation to ``target``, or explain why not.

    Every guard the transition declares is enforced here rather than in the view,
    so the API, the web app and a future bulk action cannot drift apart on what
    "close" requires.
    """
    if obligation.is_superseded:
        raise TransitionError(
            _("This obligation is no longer applicable and cannot be worked on."),
            code="superseded",
        )

    move = transition_for(obligation.state, target)
    if move is None:
        # An illegal transition usually means a colleague already moved this on
        # and the page is stale. That is an ordinary race, not an error worth a
        # stack trace.
        raise TransitionError(
            _("This obligation is now %(state)s, so that action no longer applies. "
              "Refresh to see where it has got to.")
            % {"state": obligation.get_state_display().lower()},
            code="stale",
        )

    if move.permission not in permissions:
        raise TransitionError(
            _("You do not have permission to do that."), code="forbidden"
        )

    note = note.strip()
    if move.requires_note and not note:
        raise TransitionError(
            _("Please say why — this decision is recorded and someone will read it later."),
            code="note_required",
        )

    if move.requires_filing_reference and not filing_reference.strip():
        raise TransitionError(
            _("Record the acknowledgement number. Without it the filing cannot be "
              "evidenced during an assessment."),
            code="reference_required",
        )

    previous_state = obligation.state
    now = timezone.now()
    today = as_of or timezone.localdate()

    obligation.state = target
    obligation.state_changed_at = now
    fields = ["state", "state_changed_at", "updated_at"]

    if target == State.FILED:
        obligation.filed_on = filed_on or today
        obligation.filing_reference = filing_reference.strip()
        fields += ["filed_on", "filing_reference"]
    elif previous_state == State.FILED and target == State.READY_TO_FILE:
        # Reversing a filing record clears the evidence of it. Leaving a stale
        # acknowledgement number behind would be worse than having none.
        obligation.filed_on = None
        obligation.filing_reference = ""
        fields += ["filed_on", "filing_reference"]

    if target == State.CLOSED:
        obligation.closed_at = now
        fields.append("closed_at")
    elif previous_state == State.CLOSED:
        obligation.closed_at = None
        fields.append("closed_at")

    obligation.save(update_fields=fields)

    # Dismissing or deferring has to survive the nightly rebuild, or the client
    # finds it back tomorrow morning and stops trusting the calendar.
    if target in {State.NOT_APPLICABLE, State.DEFERRED}:
        _record_suppression(obligation, target=target, reason=note, actor=actor)
    elif previous_state in {State.NOT_APPLICABLE, State.DEFERRED}:
        _revoke_suppression(obligation)

    event = ObligationEvent.objects.create(
        tenant_id=obligation.tenant_id,
        entity_id=obligation.entity_id,
        obligation=obligation,
        kind=ObligationEvent.Kind.TRANSITION,
        from_state=previous_state,
        to_state=target,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=note,
        context={
            "filing_reference": obligation.filing_reference,
            "label": move.label,
        },
    )

    record_event(
        action=AuditAction.TRANSITION,
        actor=actor,
        obj=obligation,
        before={"state": previous_state},
        after={"state": target, "filing_reference": obligation.filing_reference},
        context={"note": note, "transition": move.label},
    )

    return TransitionResult(obligation=obligation, transition=move, event=event)


def _record_suppression(
    obligation: ObligationInstance,
    *,
    target: str,
    reason: str,
    actor: Any,
) -> None:
    """Persist a dismissal as an input to future planning.

    Scoped to this period rather than to every period of the definition: "this
    month does not apply" and "this never applies to us" are different claims, and
    only the user knows which they mean. The broader form is offered separately in
    the UI, with its consequence spelled out.
    """
    ObligationSuppression.objects.update_or_create(
        entity_id=obligation.entity_id,
        definition_code=obligation.definition_code,
        scope_ref=obligation.scope_ref,
        period_key=obligation.period_key,
        defaults={
            "tenant_id": obligation.tenant_id,
            "kind": (
                ObligationSuppression.Kind.NOT_APPLICABLE
                if target == State.NOT_APPLICABLE
                else ObligationSuppression.Kind.DEFERRED
            ),
            "reason": reason,
            "created_by": actor if getattr(actor, "is_authenticated", False) else None,
            "revoked_at": None,
        },
    )


def _revoke_suppression(obligation: ObligationInstance) -> None:
    ObligationSuppression.objects.filter(
        entity_id=obligation.entity_id,
        definition_code=obligation.definition_code,
        scope_ref=obligation.scope_ref,
        period_key=obligation.period_key,
        revoked_at__isnull=True,
    ).update(revoked_at=timezone.now())


def _actor_label(actor: Any) -> str:
    if actor is None or not getattr(actor, "is_authenticated", False):
        return "STACOS"
    return (getattr(actor, "audit_label", None) or str(actor))[:200]


def assign(
    obligation: ObligationInstance,
    *,
    assignee: Any,
    actor: Any,
) -> ObligationEvent:
    """Hand an obligation to someone, and say so on the timeline."""
    previous = obligation.assigned_to
    obligation.assigned_to = assignee
    obligation.save(update_fields=["assigned_to", "updated_at"])

    event = ObligationEvent.objects.create(
        tenant_id=obligation.tenant_id,
        entity_id=obligation.entity_id,
        obligation=obligation,
        kind=ObligationEvent.Kind.ASSIGNED,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=(
            _("Assigned to %(name)s.") % {"name": assignee}
            if assignee is not None
            else _("Assignment cleared.")
        ),
        context={"from": str(previous) if previous else None, "to": str(assignee) if assignee else None},
    )

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=obligation,
        before={"assigned_to": str(previous) if previous else None},
        after={"assigned_to": str(assignee) if assignee else None},
    )
    return event
