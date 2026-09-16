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
from stacos.notifications.models import NotificationKind, Severity, SubjectType
from stacos.notifications.services import raise_notification
from stacos.obligations.models import (
    DEFAULT_WORKFLOW_STEPS,
    ROLE_PERMISSION,
    ObligationEvent,
    ObligationInstance,
    ObligationStep,
    ObligationSuppression,
)

__all__ = [
    "TransitionError",
    "TransitionResult",
    "add_comment",
    "apply_transition",
    "assign",
    "assign_step",
    "attach_acknowledgement",
    "available_actions",
    "block_step",
    "complete_step",
    "ensure_steps",
    "nudge",
    "outstanding_mandatory_evidence",
    "record_completion",
    "record_pending",
    "reopen_step",
    "unblock_step",
]


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


def outstanding_mandatory_evidence(obligation: ObligationInstance) -> list[str]:
    """Labels of the evidence the current definition marks ``mandatory_for_close``
    that this obligation does not yet have on file.

    The register has exactly one place evidence can live today — the single
    acknowledgement upload — so "on file" means that document is attached.
    That is a coarser question than "is item X specifically attached", but it
    is the honest question this data model can answer; a per-requirement
    evidence store is a bigger change than a status-transition guard should
    make on its own.

    Deliberately a plain function rather than folded into
    :func:`available_actions` alone: :func:`apply_transition` needs the exact
    same check to actually enforce the guard, not merely hide a button.
    """
    if obligation.acknowledgement:
        return []
    from stacos.catalog.models import DefinitionVersion

    requirements = (
        DefinitionVersion.objects.filter(
            definition__code=obligation.definition_code,
            version=obligation.definition_version,
        )
        .values_list("evidence_requirements", flat=True)
        .first()
    )
    if not requirements:
        return []
    return [
        item["label"]
        for item in requirements
        if item.get("mandatory_for_close") and item.get("label")
    ]


def available_actions(
    obligation: ObligationInstance,
    *,
    permissions: frozenset[str],
) -> tuple[Transition, ...]:
    """Moves this user could make on this obligation, right now.

    Used to render the action menu. Filtering here is a courtesy to the user, not
    a security boundary — :func:`apply_transition` re-checks everything, because a
    button that is merely absent from the page is not absent from the network.

    A move whose evidence is still outstanding is dropped rather than shown and
    left to fail: offering "Close" only to reject it is a worse experience than
    not offering it, and the "What you'll need" card on the detail page already
    says what is missing.
    """
    if obligation.is_superseded or obligation.is_archived:
        return ()
    moves = allowed_transitions(obligation.state, permissions=permissions)
    if any(move.requires_mandatory_evidence for move in moves) and outstanding_mandatory_evidence(
        obligation
    ):
        moves = tuple(move for move in moves if not move.requires_mandatory_evidence)
    return moves


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
    defer_until: date | None = None,
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
            _(
                "This obligation is now %(state)s, so that action no longer applies. "
                "Refresh to see where it has got to."
            )
            % {"state": obligation.get_state_display().lower()},
            code="stale",
        )

    if move.permission not in permissions:
        raise TransitionError(_("You do not have permission to do that."), code="forbidden")

    note = note.strip()
    if move.requires_note and not note:
        raise TransitionError(
            _("Please say why — this decision is recorded and someone will read it later."),
            code="note_required",
        )

    if move.requires_filing_reference and not filing_reference.strip():
        raise TransitionError(
            _(
                "Record the acknowledgement number. Without it the filing cannot be "
                "evidenced during an assessment."
            ),
            code="reference_required",
        )

    if move.requires_mandatory_evidence:
        missing = outstanding_mandatory_evidence(obligation)
        if missing:
            raise TransitionError(
                _("Attach the required evidence first: %(items)s.") % {"items": ", ".join(missing)},
                code="evidence_required",
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
        _record_suppression(
            obligation, target=target, reason=note, actor=actor, expires_on=defer_until
        )
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
    expires_on: date | None = None,
) -> None:
    """Persist a dismissal as an input to future planning.

    Scoped to this period rather than to every period of the definition: "this
    month does not apply" and "this never applies to us" are different claims, and
    only the user knows which they mean. The broader form is offered separately in
    the UI, with its consequence spelled out.

    ``expires_on`` is the planned return date for a deferral — the model has
    carried it since ``ObligationSuppression`` was written, but nothing ever
    passed one through, so every "Defer" was indefinite regardless of what the
    user actually meant by it. Meaningless for ``NOT_APPLICABLE``: there is no
    "until" to a rule not applying, so a caller passing one there is simply
    ignored rather than silently accepted as if it meant something.
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
            "expires_on": expires_on if target == State.DEFERRED else None,
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
        context={
            "from": str(previous) if previous else None,
            "to": str(assignee) if assignee else None,
        },
    )

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=obligation,
        before={"assigned_to": str(previous) if previous else None},
        after={"assigned_to": str(assignee) if assignee else None},
    )
    return event


def add_comment(
    obligation: ObligationInstance,
    *,
    actor: Any,
    note: str,
) -> ObligationEvent:
    """A free-standing remark, not tied to any state change.

    Uses :attr:`ObligationEvent.Kind.NOTE` — defined alongside every other kind
    but, until this, never written. Deliberately no :func:`record_event` call:
    a comment changes nothing about the obligation, so it has no before/after
    for a regulator's audit log, only a line on the colleague-facing timeline.
    """
    return ObligationEvent.objects.create(
        tenant_id=obligation.tenant_id,
        entity_id=obligation.entity_id,
        obligation=obligation,
        kind=ObligationEvent.Kind.NOTE,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=note,
    )


def record_completion(
    obligation: ObligationInstance,
    *,
    actor: Any,
    permissions: frozenset[str],
    filed_on: date,
    filing_reference: str,
    acknowledgement: Any = None,
    as_of: date | None = None,
) -> TransitionResult:
    """ "Yes, it is done" — the whole of it, in one call.

    Goes through :func:`apply_transition` rather than around it. The temptation
    is obvious — this knows the target state already — but the permission check,
    the legality check, the timeline entry and the audit row all live in there,
    and a second way to set ``filed_on`` is a second way to set it without any
    of them.

    The document is saved *after* the transition rather than in the same
    ``save()``: writing bytes to disk inside the transaction would leave an
    orphaned file behind if anything later rolled back, and the transition is
    worth recording with or without the attachment.
    """
    result = apply_transition(
        obligation,
        target=State.FILED,
        actor=actor,
        permissions=permissions,
        filing_reference=filing_reference,
        filed_on=filed_on,
        as_of=as_of,
    )

    # The pending answer described a filing that had not happened. Leaving it on
    # the row would show "waiting on the client's bank statement" underneath a
    # completed filing for the rest of the obligation's life.
    if obligation.pending_reason or obligation.expected_completion_date:
        obligation.pending_reason = ""
        obligation.expected_completion_date = None
        obligation.pending_reported_at = None
        obligation.save(
            update_fields=[
                "pending_reason",
                "expected_completion_date",
                "pending_reported_at",
                "updated_at",
            ]
        )

    if acknowledgement is not None:
        attach_acknowledgement(obligation, upload=acknowledgement, actor=actor)

    return result


def attach_acknowledgement(
    obligation: ObligationInstance,
    *,
    upload: Any,
    actor: Any,
) -> ObligationEvent:
    """Store the acknowledgement document and say so on the timeline.

    Replacing an existing one deletes the old bytes. That is deliberate and it
    is the one destructive act in this module: the alternative is a directory
    that grows a copy every time somebody re-uploads a corrected scan, with no
    way to tell from the row which of them the obligation actually points at.
    The *event* recording the replacement survives, which is what an auditor
    reads.
    """
    previous = obligation.acknowledgement.name or ""

    obligation.acknowledgement.save(str(upload.name), upload, save=False)
    obligation.acknowledgement_name = str(upload.name)[:255]
    obligation.save(update_fields=["acknowledgement", "acknowledgement_name", "updated_at"])

    if previous and previous != obligation.acknowledgement.name:
        obligation.acknowledgement.storage.delete(previous)

    event = ObligationEvent.objects.create(
        tenant_id=obligation.tenant_id,
        entity_id=obligation.entity_id,
        obligation=obligation,
        kind=ObligationEvent.Kind.EVIDENCE_ADDED,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=_("Acknowledgement attached: %(name)s.") % {"name": obligation.acknowledgement_name},
        context={"filename": obligation.acknowledgement_name, "replaced": bool(previous)},
    )

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=obligation,
        before={"acknowledgement": previous},
        after={"acknowledgement": obligation.acknowledgement.name},
    )
    return event


@transaction.atomic
def record_pending(
    obligation: ObligationInstance,
    *,
    actor: Any,
    permissions: frozenset[str],
    reason: str,
    expected_on: date | None,
) -> ObligationEvent:
    """ "Not yet" — why not, and when it is expected.

    **This changes no state.** An obligation someone has explained is still
    owed, still dated and still going overdue on schedule; the register would
    stop being worth reading the day an explanation began to count as progress.
    Deferring exists for the case where the *decision* is to postpone, and it
    suppresses the row precisely because somebody took responsibility for that.

    The answer is stamped with when it was given, so "next week" written two
    months ago reads as stale rather than as current.
    """
    if "compliance.obligation.prepare" not in permissions:
        raise TransitionError(_("You do not have permission to do that."), code="forbidden")

    if obligation.is_superseded:
        raise TransitionError(
            _("This obligation is no longer applicable and cannot be worked on."),
            code="superseded",
        )

    reason = reason.strip()
    if not reason:
        raise TransitionError(
            _("Please say why — this is what the next person to pick it up will read."),
            code="note_required",
        )

    before = {
        "pending_reason": obligation.pending_reason,
        "expected_completion_date": (
            obligation.expected_completion_date.isoformat()
            if obligation.expected_completion_date
            else None
        ),
    }

    obligation.pending_reason = reason
    obligation.expected_completion_date = expected_on
    obligation.pending_reported_at = timezone.now()
    obligation.save(
        update_fields=[
            "pending_reason",
            "expected_completion_date",
            "pending_reported_at",
            "updated_at",
        ]
    )

    event = ObligationEvent.objects.create(
        tenant_id=obligation.tenant_id,
        entity_id=obligation.entity_id,
        obligation=obligation,
        kind=ObligationEvent.Kind.NOTE,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=(
            _("Still pending: %(reason)s Expected by %(date)s.")
            % {"reason": reason, "date": expected_on.isoformat()}
            if expected_on
            else _("Still pending: %(reason)s") % {"reason": reason}
        ),
        context={
            "pending_reason": reason,
            "expected_completion_date": expected_on.isoformat() if expected_on else None,
        },
    )

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=obligation,
        before=before,
        after={
            "pending_reason": reason,
            "expected_completion_date": expected_on.isoformat() if expected_on else None,
        },
    )
    return event


def nudge(
    obligation: ObligationInstance,
    *,
    person: Any,
    actor: Any,
    as_of: date,
    url: str,
) -> ObligationEvent:
    """Re-raise the reminder for whoever is holding this up.

    ``person`` is usually ``obligation.assigned_to``, but a checklist step can
    have its own assignee — someone specifically blocking *that* step rather
    than the filing as a whole — so it is a parameter rather than read off the
    obligation here. The view only offers the button when there is somebody to
    nudge.

    Reuses the same approved WhatsApp/email templates the automated reminder
    sweep sends (``OBLIGATION_OVERDUE`` / ``OBLIGATION_DUE``), rather than
    inventing a new notification kind: a kind is also a WhatsApp template that
    needs Meta's approval by name, and a manual nudge says exactly the same
    thing an automated one would.

    ``dedupe_key`` carries the current timestamp so a manual nudge is never
    silently swallowed by the sweep's own dedupe key for the same day.
    """
    overdue = obligation.due_date is not None and obligation.due_date < as_of
    kind = NotificationKind.OBLIGATION_OVERDUE if overdue else NotificationKind.OBLIGATION_DUE

    raise_notification(
        tenant_id=obligation.tenant_id,
        recipient=person,
        kind=kind,
        title=_("%(obligation)s needs you") % {"obligation": obligation.title},
        body=_("%(actor)s nudged you about this filing.") % {"actor": _actor_label(actor)},
        url=url,
        severity=Severity.ATTENTION,
        entity=obligation.entity,
        subject_type=SubjectType.OBLIGATION,
        subject_id=obligation.pk,
        dedupe_key=f"nudge:{obligation.pk}:{person.pk}:{timezone.now().isoformat()}",
        context={
            "entity": obligation.entity.name,
            "obligation": obligation.title,
            "date": obligation.due_date.isoformat() if obligation.due_date else "",
        },
    )

    return ObligationEvent.objects.create(
        tenant_id=obligation.tenant_id,
        entity_id=obligation.entity_id,
        obligation=obligation,
        kind=ObligationEvent.Kind.NOTE,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=_("Nudged %(person)s.") % {"person": person},
    )


# ---------------------------------------------------------------------------
# The checklist — opt-in detail behind the obligations whose definition has a
# workflow_steps template. See ObligationStep's own docstring for why this
# exists beside, not instead of, the lifecycle state.
# ---------------------------------------------------------------------------


def ensure_steps(obligation: ObligationInstance) -> list[ObligationStep]:
    """The obligation's checklist, creating it from a template on first read.

    Every obligation gets one: the catalog definition's own
    ``workflow_steps`` when it has written one (Form 24Q does), otherwise
    ``DEFAULT_WORKFLOW_STEPS`` — the same four stages the lifecycle stepper
    has always shown, as real per-item work instead of a status-only bar.
    There is no "no checklist" case on the happy path any more; the detail
    page still suppresses the section entirely off it (deferred, disputed,
    not-applicable), the same way it always suppressed the stepper.

    Idempotent and self-healing, the same shape as
    ``tests.conftest._system_role``: called on every detail-page view, it is a
    no-op once the rows exist, and there is nothing to reconcile if it is
    called twice concurrently — ``ignore_conflicts`` on the identity
    constraint means the loser of a race simply does not insert its copies.
    """
    existing = list(obligation.steps.select_related("assigned_to", "completed_by"))
    if existing:
        return existing

    from stacos.catalog.models import DefinitionVersion

    version = (
        DefinitionVersion.objects.filter(
            definition__code=obligation.definition_code, version=obligation.definition_version
        )
        .only("workflow_steps")
        .first()
    )
    templates = (version.workflow_steps if version else None) or DEFAULT_WORKFLOW_STEPS

    ObligationStep.objects.bulk_create(
        [
            ObligationStep(
                tenant_id=obligation.tenant_id,
                entity_id=obligation.entity_id,
                obligation=obligation,
                key=template["key"],
                order=index,
                label=template["label"],
                role=template["role"],
                assigned_to=_resolve_default_assignee(
                    obligation.entity, template.get("default_owner_role", "")
                ),
                days_before_due=template.get("days_before_due"),
                requires_evidence=bool(template.get("requires_evidence", False)),
            )
            for index, template in enumerate(templates)
        ],
        ignore_conflicts=True,
    )
    return list(obligation.steps.select_related("assigned_to", "completed_by"))


def _resolve_default_assignee(entity: Any, role_code: str) -> Any | None:
    """Whoever holds ``role_code`` in this entity's tenant, deterministically.

    Several members can hold the same role, so this is not "the" holder in
    any absolute sense — it is the earliest-joined active one, picked
    consistently rather than arbitrarily. An unresolvable or empty code is
    not an error: the step is simply left unassigned, the same outcome as a
    definition that names no default at all. Not entity-scoped (does not
    check ``Membership.all_entities``/``entities``) — a person scoped away
    from this specific entity could still be picked; a v1 simplification.
    """
    if not role_code:
        return None

    from stacos.tenancy.models import Membership

    membership = (
        Membership.objects.filter(
            tenant_id=entity.tenant_id,
            role__code=role_code,
            status=Membership.Status.ACTIVE,
        )
        .select_related("user")
        .order_by("created_at")
        .first()
    )
    return membership.user if membership else None


def _require_step_permission(step: ObligationStep, permissions: frozenset[str]) -> None:
    if ROLE_PERMISSION[step.role] not in permissions:
        raise TransitionError(_("You do not have permission to do that."), code="forbidden")


def complete_step(
    step: ObligationStep, *, actor: Any, permissions: frozenset[str], evidence_note: str = ""
) -> ObligationStep:
    """Mark one checklist item done.

    ``evidence_note`` is required, not optional, when the step's own template
    asked for one (``requires_evidence``) — the same shape as a blocked
    step's reason: a judgement call gets a sentence recorded with it, not a
    silent click.
    """
    _require_step_permission(step, permissions)

    evidence_note = evidence_note.strip()
    if step.requires_evidence and not evidence_note:
        raise TransitionError(
            _("Say what evidence confirms this — it is recorded with the step."),
            code="note_required",
        )

    step.state = ObligationStep.State.DONE
    step.completed_at = timezone.now()
    step.completed_by = actor if getattr(actor, "is_authenticated", False) else None
    step.blocked_reason = ""
    if evidence_note:
        step.evidence_note = evidence_note
    step.save(
        update_fields=[
            "state",
            "completed_at",
            "completed_by",
            "blocked_reason",
            "evidence_note",
            "updated_at",
        ]
    )
    ObligationEvent.objects.create(
        tenant_id=step.tenant_id,
        entity_id=step.entity_id,
        obligation_id=step.obligation_id,
        kind=ObligationEvent.Kind.STEP_COMPLETED,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=_('Completed "%(label)s".') % {"label": step.label},
    )
    return step


def reopen_step(step: ObligationStep, *, actor: Any, permissions: frozenset[str]) -> ObligationStep:
    """Undo a completed step — it becomes pending again, evidence and all."""
    _require_step_permission(step, permissions)

    step.state = ObligationStep.State.PENDING
    step.completed_at = None
    step.completed_by = None
    step.save(update_fields=["state", "completed_at", "completed_by", "updated_at"])
    ObligationEvent.objects.create(
        tenant_id=step.tenant_id,
        entity_id=step.entity_id,
        obligation_id=step.obligation_id,
        kind=ObligationEvent.Kind.STEP_REOPENED,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=_('Reopened "%(label)s".') % {"label": step.label},
    )
    return step


def block_step(
    step: ObligationStep, *, actor: Any, permissions: frozenset[str], reason: str
) -> ObligationStep:
    """Say what a step is waiting on, so "blocked" means something specific."""
    _require_step_permission(step, permissions)
    reason = reason.strip()
    if not reason:
        raise TransitionError(
            _("Say what this is waiting on — it is shown to whoever can unblock it."),
            code="note_required",
        )

    step.state = ObligationStep.State.BLOCKED
    step.blocked_reason = reason
    step.save(update_fields=["state", "blocked_reason", "updated_at"])
    ObligationEvent.objects.create(
        tenant_id=step.tenant_id,
        entity_id=step.entity_id,
        obligation_id=step.obligation_id,
        kind=ObligationEvent.Kind.STEP_BLOCKED,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=_('Blocked "%(label)s" — %(reason)s') % {"label": step.label, "reason": reason},
    )
    return step


def unblock_step(
    step: ObligationStep, *, actor: Any, permissions: frozenset[str]
) -> ObligationStep:
    """Clear a block without marking the step done — the thing it was waiting
    on came through, the work itself has not."""
    _require_step_permission(step, permissions)

    step.state = ObligationStep.State.PENDING
    step.blocked_reason = ""
    step.save(update_fields=["state", "blocked_reason", "updated_at"])
    ObligationEvent.objects.create(
        tenant_id=step.tenant_id,
        entity_id=step.entity_id,
        obligation_id=step.obligation_id,
        kind=ObligationEvent.Kind.STEP_UNBLOCKED,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=_('Unblocked "%(label)s".') % {"label": step.label},
    )
    return step


def assign_step(step: ObligationStep, *, assignee: Any, actor: Any) -> ObligationStep:
    """Hand one checklist item to someone — independent of who owns the
    obligation as a whole."""
    step.assigned_to = assignee
    step.save(update_fields=["assigned_to", "updated_at"])
    ObligationEvent.objects.create(
        tenant_id=step.tenant_id,
        entity_id=step.entity_id,
        obligation_id=step.obligation_id,
        kind=ObligationEvent.Kind.STEP_ASSIGNED,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=_actor_label(actor),
        note=(
            _('Assigned "%(label)s" to %(name)s.') % {"label": step.label, "name": assignee}
            if assignee is not None
            else _('Cleared the assignee on "%(label)s".') % {"label": step.label}
        ),
    )
    return step
