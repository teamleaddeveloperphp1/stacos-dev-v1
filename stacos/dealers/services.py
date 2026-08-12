"""
Accruing, clawing back and paying commission.

Every function here writes a *ledger row*. Nothing accumulates a balance, because
a dealer disputing a payout needs to see which invoice each rupee came from, and
a balance cannot answer that.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

import structlog
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.core.scope import platform_scope
from stacos.dealers.models import CommissionEntry, CommissionPlan, Payout

logger = structlog.get_logger(__name__)

__all__ = ["DealerError", "accrue", "approve_payout", "claw_back", "mark_paid"]


class DealerError(Exception):
    """Something that cannot be done, phrased for a user."""


def accrue(
    *,
    dealer_tenant_id: UUID,
    client_tenant_id: UUID,
    invoice_id: UUID,
    invoice_number: str,
    base_amount_minor: int,
    period_start: date | None = None,
    period_end: date | None = None,
) -> CommissionEntry | None:
    """Book a dealer's share of one invoice. Idempotent per invoice.

    Runs under a platform scope because it writes into the *dealer's* tenant
    while the caller is inside the *client's* — a legitimate cross-tenant write,
    and one worth having audited every time it happens.

    Returns ``None`` when there is no active plan, or when this invoice has
    already been accrued: a rerun of the billing job must not pay twice.
    """
    with platform_scope(reason="commission-accrual"):
        plan = (
            CommissionPlan.objects.filter(
                tenant_id=dealer_tenant_id, is_active=True, valid_from__lte=timezone.localdate()
            )
            .order_by("-valid_from")
            .first()
        )
        if plan is None:
            logger.info("dealers.no_plan", dealer=str(dealer_tenant_id))
            return None

        amount_minor = base_amount_minor * plan.rate_bps // 10_000
        if amount_minor <= 0:
            return None

        try:
            with transaction.atomic():
                entry = CommissionEntry.objects.create(
                    tenant_id=dealer_tenant_id,
                    plan=plan,
                    client_tenant_id=client_tenant_id,
                    invoice_id=invoice_id,
                    invoice_number=invoice_number,
                    amount_minor=amount_minor,
                    base_amount_minor=base_amount_minor,
                    rate_bps=plan.rate_bps,
                    period_start=period_start,
                    period_end=period_end,
                )
        except IntegrityError:
            # The unique constraint did its job: this invoice was already
            # accrued, by a webhook replay or a rerun of the billing job.
            logger.info("dealers.accrual_duplicate_suppressed", invoice=invoice_number)
            return None

        logger.info(
            "dealers.accrued",
            dealer=str(dealer_tenant_id),
            invoice=invoice_number,
            amount_minor=amount_minor,
        )
        return entry


def claw_back(*, invoice_id: UUID, reason: str) -> CommissionEntry | None:
    """Reverse an accrual with a negative row, never by editing the positive one.

    A refunded or voided invoice should leave both the accrual and its reversal
    visible. Deleting the accrual would make the ledger add up while telling a
    dealer nothing about what happened.
    """
    with platform_scope(reason="commission-clawback"):
        original = CommissionEntry.objects.filter(
            invoice_id=invoice_id,
            status__in=[CommissionEntry.Status.ACCRUED, CommissionEntry.Status.APPROVED],
        ).first()
        if original is None:
            return None

        reversal = CommissionEntry.objects.create(
            tenant_id=original.tenant_id,
            plan=original.plan,
            client_tenant_id=original.client_tenant_id,
            invoice_id=invoice_id,
            invoice_number=original.invoice_number,
            amount_minor=-original.amount_minor,
            base_amount_minor=original.base_amount_minor,
            rate_bps=original.rate_bps,
            status=CommissionEntry.Status.CLAWED_BACK,
            note=reason[:250],
        )
        # The original moves to CLAWED_BACK too, which also releases the unique
        # constraint so a corrected invoice can accrue again.
        original.status = CommissionEntry.Status.CLAWED_BACK
        original.note = reason[:250]
        original.save(update_fields=["status", "note", "updated_at"])

        logger.info("dealers.clawed_back", invoice=str(invoice_id), reason=reason[:80])
        return reversal


@transaction.atomic
def approve_payout(payout: Payout, *, actor: Any = None) -> Payout:
    """Sweep the dealer's accrued entries into a payout and total it.

    The total is recomputed from the lines rather than accumulated, so the
    header and the detail cannot drift apart.
    """
    if payout.status != Payout.Status.DRAFT:
        raise DealerError(_("This payout has already been approved."))

    claimed = CommissionEntry.objects.filter(
        tenant_id=payout.tenant_id,
        status=CommissionEntry.Status.ACCRUED,
        created_at__date__lte=payout.period_end,
    )
    count = claimed.update(payout=payout, status=CommissionEntry.Status.APPROVED)
    if count == 0:
        raise DealerError(_("There is nothing to pay for this period."))

    payout.total_minor = payout.recompute_total()
    payout.status = Payout.Status.APPROVED
    payout.approved_by = actor if getattr(actor, "is_authenticated", False) else None
    payout.approved_at = timezone.now()
    payout.save(update_fields=["total_minor", "status", "approved_by", "approved_at", "updated_at"])

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=payout,
        after={"status": Payout.Status.APPROVED, "total_minor": payout.total_minor, "lines": count},
    )
    return payout


@transaction.atomic
def mark_paid(
    payout: Payout, *, reference: str, paid_on: date | None = None, actor: Any = None
) -> Payout:
    """Record that the money left. The reference is required.

    A payout marked paid with no bank reference cannot be matched to a statement,
    which is precisely the conversation the record exists to prevent.
    """
    if payout.status != Payout.Status.APPROVED:
        raise DealerError(_("This payout has not been approved."))
    if not reference.strip():
        raise DealerError(_("Record the bank reference so this can be matched to a statement."))

    payout.status = Payout.Status.PAID
    payout.payment_reference = reference.strip()[:120]
    payout.paid_on = paid_on or timezone.localdate()
    payout.save(update_fields=["status", "payment_reference", "paid_on", "updated_at"])

    payout.entries.update(status=CommissionEntry.Status.PAID, updated_at=timezone.now())

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=payout,
        after={"status": Payout.Status.PAID, "reference": payout.payment_reference},
    )
    return payout


def statement(*, dealer_tenant_id: UUID) -> dict[str, Any]:
    """What a dealer is owed, split by where it has got to.

    Totalled from the ledger on every call. Cheap — a dealer has hundreds of
    entries, not millions — and always right.
    """
    from django.db.models import Count, Sum

    rows = (
        CommissionEntry.objects.filter(tenant_id=dealer_tenant_id)
        .values("status")
        .annotate(total=Sum("amount_minor"), lines=Count("id"))
    )
    by_status = {row["status"]: row for row in rows}

    return {
        "accrued_minor": (by_status.get("ACCRUED") or {}).get("total") or 0,
        "approved_minor": (by_status.get("APPROVED") or {}).get("total") or 0,
        "paid_minor": (by_status.get("PAID") or {}).get("total") or 0,
        "clawed_back_minor": (by_status.get("CLAWED_BACK") or {}).get("total") or 0,
        "by_status": by_status,
    }
