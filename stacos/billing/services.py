"""
Issuing invoices and taking money.

Invoice numbering, tax and idempotent payment recording all live here so there is
exactly one implementation of each. The rails — Stripe, PayU, offline — differ
only in how a payment *arrives*; what it does to an invoice is identical, and
that is deliberate.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import structlog
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext as _

from stacos.billing.models import Invoice, InvoiceLine, Payment, Subscription
from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.core.scope import platform_scope

logger = structlog.get_logger(__name__)

__all__ = [
    "BillingError",
    "issue_invoice",
    "next_invoice_number",
    "record_payment",
    "renew",
    "void_invoice",
]

#: Net 15. Long enough to be reasonable for a business, short enough that a
#: missed payment surfaces inside the period it relates to.
PAYMENT_TERMS_DAYS = 15

#: SAC code for online information and database access or retrieval services,
#: which is what a compliance SaaS subscription is. On the invoice because a
#: compliant tax invoice needs it.
SAC_CODE = "998315"


class BillingError(Exception):
    """Something that cannot be done, phrased for a user."""


def next_invoice_number(*, prefix: str, issued_on: date) -> str:
    """The next number in the series for a financial year.

    Sequential per financial year and never reused, because the tax authority
    treats gaps as missing invoices and reuse as two documents with one identity.
    Derived from the maximum rather than a counter table, so a rolled-back
    transaction does not burn a number.
    """
    # India's financial year, and the only place in this module that knows it.
    fy_start_year = issued_on.year if issued_on.month >= 4 else issued_on.year - 1
    series = f"{prefix}/{fy_start_year % 100:02d}{(fy_start_year + 1) % 100:02d}/"

    # **Platform-wide, not tenant-scoped.** These are STACOS's own sales
    # invoices: one issuer, one series, and `invoice_number_uniq` is a global
    # constraint to match. Taking the maximum through the tenant-scoped manager
    # would restart the sequence for every customer and collide on the second
    # tenant to be billed in a financial year — an integrity error at the worst
    # possible moment, on the billing run.
    with platform_scope(reason="billing:invoice-number"):
        highest = Invoice.objects.filter(number__startswith=series).aggregate(top=Max("number"))[
            "top"
        ]

    sequence = int(highest.rsplit("/", 1)[-1]) + 1 if highest else 1
    return f"{series}{sequence:05d}"


@transaction.atomic
def issue_invoice(
    subscription: Subscription,
    *,
    issued_on: date | None = None,
    actor: Any = None,
    prefix: str = "INV",
) -> Invoice:
    """Raise and issue an invoice for the subscription's current period.

    The amount is recomputed from the plan and the quantities rather than copied
    from anywhere, so a price or seat change is picked up automatically and there
    is only one arithmetic to be wrong.
    """
    issued_on = issued_on or timezone.localdate()
    net_minor = subscription.compute_amount_minor()
    tax_minor = net_minor * 1800 // 10_000

    invoice = Invoice.objects.create(
        tenant_id=subscription.tenant_id,
        subscription=subscription,
        status=Invoice.Status.DRAFT,
        period_start=subscription.current_period_start,
        period_end=subscription.current_period_end,
        subtotal_minor=net_minor,
        tax_minor=tax_minor,
        total_minor=net_minor + tax_minor,
        tax_rate_bps=1800,
    )

    InvoiceLine.objects.create(
        tenant_id=subscription.tenant_id,
        invoice=invoice,
        description=(
            f"{subscription.plan.name} — {subscription.current_period_start:%d %b %Y} "
            f"to {subscription.current_period_end:%d %b %Y}"
        ),
        quantity=Decimal("1"),
        unit_amount_minor=net_minor,
        amount_minor=net_minor,
        sac_code=SAC_CODE,
    )

    invoice.number = next_invoice_number(prefix=prefix, issued_on=issued_on)
    invoice.status = Invoice.Status.ISSUED
    invoice.issued_on = issued_on
    invoice.due_on = issued_on + timedelta(days=PAYMENT_TERMS_DAYS)
    invoice.save(update_fields=["number", "status", "issued_on", "due_on", "updated_at"])

    record_event(
        action=AuditAction.CREATE,
        actor=actor,
        obj=invoice,
        after={"number": invoice.number, "total_minor": invoice.total_minor},
    )
    _accrue_commission(invoice, subscription)
    return invoice


def _accrue_commission(invoice: Invoice, subscription: Subscription) -> None:
    """Book the dealer's share, if the subscription was sold by one.

    Called from here rather than from a nightly job so the accrual and the
    invoice are one transaction: a commission that exists without its invoice, or
    an invoice whose commission was missed, are both reconciliation problems
    nobody enjoys.
    """
    if subscription.sold_by_dealer_id is None:
        return

    from stacos.dealers.services import accrue

    try:
        accrue(
            dealer_tenant_id=subscription.sold_by_dealer_id,
            client_tenant_id=subscription.tenant_id,
            invoice_id=invoice.pk,
            invoice_number=invoice.number,
            base_amount_minor=invoice.subtotal_minor,
            period_start=invoice.period_start,
            period_end=invoice.period_end,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("billing.commission_accrual_failed", invoice=invoice.number, error=str(exc))


@transaction.atomic
def record_payment(
    invoice: Invoice,
    *,
    amount_minor: int,
    method: str,
    provider: str = "",
    provider_reference: str = "",
    external_reference: str = "",
    received_on: date | None = None,
    actor: Any = None,
) -> tuple[Payment, bool]:
    """Record money received. Idempotent on the provider's reference.

    Returns ``(payment, created)``. Gateways retry webhooks — all of them — and a
    second row for one payment is a refund conversation nobody wants.
    """
    if invoice.status == Invoice.Status.VOID:
        raise BillingError(_("This invoice has been voided."))
    if amount_minor <= 0:
        raise BillingError(_("A payment has to be for more than nothing."))

    if provider_reference:
        existing = Payment.objects.filter(
            provider=provider, provider_reference=provider_reference
        ).first()
        if existing is not None:
            logger.info("billing.payment_replayed", reference=provider_reference)
            return existing, False

    try:
        payment = Payment.objects.create(
            tenant_id=invoice.tenant_id,
            invoice=invoice,
            amount_minor=amount_minor,
            method=method,
            status=Payment.Status.SUCCEEDED,
            provider=provider,
            provider_reference=provider_reference,
            external_reference=external_reference,
            received_on=received_on or timezone.localdate(),
            reconciled_by=actor if getattr(actor, "is_authenticated", False) else None,
        )
    except IntegrityError:
        # Lost the race with a concurrent replay of the same webhook. The other
        # writer won; return theirs.
        existing = Payment.objects.get(provider=provider, provider_reference=provider_reference)
        return existing, False

    _settle(invoice)
    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=invoice,
        after={"paid_minor": invoice.paid_minor(), "status": invoice.status},
        context={"method": method, "amount_minor": amount_minor},
    )
    return payment, True


def _settle(invoice: Invoice) -> None:
    """Re-derive an invoice's status from what has actually been received."""
    outstanding = invoice.outstanding_minor
    if outstanding == 0:
        invoice.status = Invoice.Status.PAID
    elif invoice.paid_minor() > 0:
        invoice.status = Invoice.Status.PARTIALLY_PAID
    elif invoice.due_on and invoice.due_on < timezone.localdate():
        invoice.status = Invoice.Status.OVERDUE
    else:
        invoice.status = Invoice.Status.ISSUED
    invoice.save(update_fields=["status", "updated_at"])


@transaction.atomic
def void_invoice(invoice: Invoice, *, reason: str, actor: Any = None) -> Invoice:
    """Void an invoice that was never paid.

    A paid invoice cannot be voided — that is a credit note, which is a different
    document with its own number. Conflating them leaves the client's books and
    ours disagreeing about a payment that genuinely happened.
    """
    if invoice.paid_minor() > 0:
        raise BillingError(
            _("This invoice has been paid. Raise a credit note rather than voiding it.")
        )
    if not reason.strip():
        raise BillingError(_("Say why — a voided invoice number has to be explainable."))

    invoice.status = Invoice.Status.VOID
    invoice.void_reason = reason.strip()[:250]
    invoice.save(update_fields=["status", "void_reason", "updated_at"])

    from stacos.dealers.services import claw_back

    claw_back(invoice_id=invoice.pk, reason=f"Invoice {invoice.number} voided")

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=invoice,
        after={"status": Invoice.Status.VOID},
        context={"reason": reason[:250]},
    )
    return invoice


@transaction.atomic
def renew(subscription: Subscription, *, actor: Any = None) -> Invoice:
    """Roll a subscription into its next period and invoice it."""
    if not subscription.is_live:
        raise BillingError(_("This subscription is not active."))

    length = subscription.current_period_end - subscription.current_period_start
    subscription.current_period_start = subscription.current_period_end + timedelta(days=1)
    subscription.current_period_end = subscription.current_period_start + length
    if subscription.status == Subscription.Status.TRIALING:
        subscription.status = Subscription.Status.ACTIVE
    subscription.save(
        update_fields=["current_period_start", "current_period_end", "status", "updated_at"]
    )

    return issue_invoice(subscription, actor=actor)


def mark_overdue(*, as_of: date) -> int:
    """Flag invoices past their date. Returns how many moved.

    A status change rather than a suspension: data is never made unavailable for
    non-payment, and an account going read-only is a separate, later, deliberate
    decision.
    """
    moved = Invoice.objects.filter(
        status__in=[Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID],
        due_on__lt=as_of,
    ).update(status=Invoice.Status.OVERDUE, updated_at=timezone.now())
    if moved:
        logger.info("billing.marked_overdue", count=moved)
    return int(moved)
