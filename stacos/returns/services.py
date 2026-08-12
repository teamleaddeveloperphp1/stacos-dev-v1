"""
Moving a return through preparation, review, approval and filing.

The maker-checker rule is enforced twice: here, with a message a user can act on,
and by a database constraint that cannot be talked out of it. Belt and braces is
right for the one control a firm's professional liability rests on.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.obligations.models import ObligationInstance
from stacos.obligations.transitions import apply_transition
from stacos.returns.models import PreparationState, Reconciliation, ReturnPreparation

logger = structlog.get_logger(__name__)

__all__ = [
    "PreparationError",
    "approve",
    "file_return",
    "get_or_create_for",
    "mark_prepared",
    "review",
    "send_back",
]


class PreparationError(Exception):
    """Something a user did that cannot be done, phrased for a user."""


@transaction.atomic
def get_or_create_for(
    obligation: ObligationInstance, *, actor: Any = None
) -> tuple[ReturnPreparation, bool]:
    """Open working papers for an obligation, or return the existing set."""
    preparation, created = ReturnPreparation.objects.get_or_create(
        obligation=obligation,
        defaults={
            "tenant_id": obligation.tenant_id,
            "entity_id": obligation.entity_id,
            "form_type": obligation.definition_code.rsplit("-", 1)[-1][:40],
            "period_key": obligation.period_key,
        },
    )
    if created:
        record_event(
            action=AuditAction.CREATE,
            actor=actor,
            obj=preparation,
            after={"form_type": preparation.form_type, "period": preparation.period_key},
        )
    return preparation, created


@transaction.atomic
def mark_prepared(
    preparation: ReturnPreparation,
    *,
    actor: Any,
    figures: dict[str, Any] | None = None,
    tax_payable: Decimal | None = None,
) -> ReturnPreparation:
    """The maker says it is finished.

    Refuses while a reconciliation still has unexplained lines. A return
    submitted for review with forty unexplained differences wastes the reviewer's
    time and, more often, gets waved through.
    """
    if preparation.state not in {PreparationState.DRAFT, PreparationState.REWORK}:
        raise PreparationError(_("This return has already been submitted for review."))

    outstanding = preparation.unexplained_differences
    if outstanding:
        raise PreparationError(
            _("%(count)d reconciliation difference(s) are still unexplained. Resolve them first.")
            % {"count": outstanding}
        )

    if figures is not None:
        preparation.figures = figures
    if tax_payable is not None:
        preparation.tax_payable = tax_payable

    preparation.state = PreparationState.PREPARED
    preparation.prepared_by = actor if getattr(actor, "is_authenticated", False) else None
    preparation.prepared_at = timezone.now()
    preparation.save(
        update_fields=[
            "figures",
            "tax_payable",
            "state",
            "prepared_by",
            "prepared_at",
            "updated_at",
        ]
    )

    record_event(
        action=AuditAction.TRANSITION,
        actor=actor,
        obj=preparation,
        after={"state": PreparationState.PREPARED},
    )
    return preparation


@transaction.atomic
def review(preparation: ReturnPreparation, *, actor: Any, notes: str = "") -> ReturnPreparation:
    """The checker is satisfied.

    The maker cannot be the checker. Enforced here so the message is useful, and
    again by a check constraint so the rule survives a code path nobody
    remembered.
    """
    if preparation.state != PreparationState.PREPARED:
        raise PreparationError(_("This return is not waiting for review."))

    actor_id = getattr(actor, "pk", None)
    if actor_id is not None and actor_id == preparation.prepared_by_id:
        raise PreparationError(_("You prepared this return, so somebody else has to review it."))

    preparation.state = PreparationState.REVIEWED
    preparation.reviewed_by = actor if getattr(actor, "is_authenticated", False) else None
    preparation.reviewed_at = timezone.now()
    preparation.review_notes = notes
    preparation.save(
        update_fields=["state", "reviewed_by", "reviewed_at", "review_notes", "updated_at"]
    )

    record_event(
        action=AuditAction.TRANSITION,
        actor=actor,
        obj=preparation,
        after={"state": PreparationState.REVIEWED},
        context={"notes": notes},
    )
    return preparation


@transaction.atomic
def send_back(preparation: ReturnPreparation, *, actor: Any, notes: str) -> ReturnPreparation:
    """Return it to the maker with findings.

    The findings are required. "Sent back" with no explanation produces the same
    return again, and a second wasted review.
    """
    if not notes.strip():
        raise PreparationError(_("Say what needs changing — otherwise the same return comes back."))
    if preparation.state != PreparationState.PREPARED:
        raise PreparationError(_("This return is not waiting for review."))

    preparation.state = PreparationState.REWORK
    preparation.review_notes = notes.strip()
    preparation.save(update_fields=["state", "review_notes", "updated_at"])

    record_event(
        action=AuditAction.TRANSITION,
        actor=actor,
        obj=preparation,
        after={"state": PreparationState.REWORK},
        context={"notes": notes[:250]},
    )
    return preparation


@transaction.atomic
def approve(preparation: ReturnPreparation, *, actor: Any) -> ReturnPreparation:
    """Client sign-off. The point where the business takes responsibility."""
    if preparation.state != PreparationState.REVIEWED:
        raise PreparationError(_("This return has not been reviewed yet."))

    preparation.state = PreparationState.APPROVED
    preparation.approved_by = actor if getattr(actor, "is_authenticated", False) else None
    preparation.approved_at = timezone.now()
    preparation.save(update_fields=["state", "approved_by", "approved_at", "updated_at"])

    record_event(
        action=AuditAction.TRANSITION,
        actor=actor,
        obj=preparation,
        after={"state": PreparationState.APPROVED},
    )
    return preparation


@transaction.atomic
def file_return(
    preparation: ReturnPreparation,
    *,
    actor: Any,
    reference: str,
    filed_on: date | None = None,
    permissions: frozenset[str] = frozenset(),
) -> ReturnPreparation:
    """Record the filing, and move the obligation with it.

    The two are updated together. A preparation marked filed while its obligation
    still says "ready to file" is the kind of split-brain that makes a compliance
    dashboard untrustworthy — and the dashboard is the product.
    """
    if preparation.state != PreparationState.APPROVED:
        raise PreparationError(_("This return has not been approved for filing."))
    if not reference.strip():
        raise PreparationError(
            _(
                "Record the acknowledgement number — a filing that cannot be evidenced is not a filing."
            )
        )

    filed_on = filed_on or timezone.localdate()

    preparation.state = PreparationState.FILED
    preparation.filed_on = filed_on
    preparation.filing_reference = reference.strip()[:120]
    preparation.filed_by = actor if getattr(actor, "is_authenticated", False) else None
    preparation.save(
        update_fields=["state", "filed_on", "filing_reference", "filed_by", "updated_at"]
    )

    _carry_the_obligation(preparation, actor=actor, permissions=permissions, filed_on=filed_on)

    record_event(
        action=AuditAction.TRANSITION,
        actor=actor,
        obj=preparation,
        after={"state": PreparationState.FILED, "reference": preparation.filing_reference},
    )
    return preparation


def _carry_the_obligation(
    preparation: ReturnPreparation,
    *,
    actor: Any,
    permissions: frozenset[str],
    filed_on: date,
) -> None:
    """Walk the obligation to FILED, through whatever states lie between.

    An obligation nobody touched sits at ``NOT_STARTED``, and there is no direct
    move from there to ``FILED`` — nor should there be. The preparation *is* the
    workflow, so the route is walked here rather than demanded of the user, who
    has already done the work the intermediate states describe.

    Best-effort: a filing genuinely happened whether or not the register caught
    up, so a failure here is logged rather than rolled back. Every step carries
    the same note so the timeline explains itself.
    """
    from stacos.engine.lifecycle import path_to
    from stacos.obligations.transitions import TransitionError

    obligation = preparation.obligation
    route = path_to(obligation.state, "FILED")
    if not route:
        logger.info(
            "returns.obligation_unreachable",
            preparation_id=str(preparation.pk),
            state=obligation.state,
        )
        return

    granted = permissions | {move.permission for move in route}
    note = _("Filed through return preparation %(form)s %(period)s.") % {
        "form": preparation.form_type,
        "period": preparation.period_key,
    }

    for move in route:
        try:
            apply_transition(
                obligation,
                target=move.target,
                actor=actor,
                permissions=granted,
                note=note if move.requires_note else "",
                filing_reference=preparation.filing_reference,
                filed_on=filed_on,
            )
        except TransitionError as exc:
            logger.info(
                "returns.obligation_not_moved",
                preparation_id=str(preparation.pk),
                target=move.target,
                reason=str(exc),
            )
            return


@transaction.atomic
def resolve_difference(
    difference: Any, *, resolution: str, note: str = "", actor: Any = None
) -> Any:
    """Explain one reconciliation line."""
    difference.resolution = resolution
    difference.resolution_note = note.strip()[:250]
    difference.resolved_by = actor if getattr(actor, "is_authenticated", False) else None
    difference.resolved_at = timezone.now()
    difference.save(
        update_fields=[
            "resolution",
            "resolution_note",
            "resolved_by",
            "resolved_at",
            "updated_at",
        ]
    )
    return difference


def summarise(reconciliation: Reconciliation) -> dict[str, Any]:
    """The one-line summary a reviewer reads before opening any line."""
    rows = list(reconciliation.differences.all())
    return {
        "variance": reconciliation.variance,
        "lines": len(rows),
        "unexplained": sum(1 for row in rows if not row.is_resolved),
        "largest": max((abs(row.difference) for row in rows), default=Decimal("0")),
    }
