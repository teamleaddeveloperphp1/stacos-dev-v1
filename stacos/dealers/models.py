"""
Channel partners: commission plans, the ledger, and payouts.

**Note what is absent from this module: any access to compliance data.** A dealer
sells subscriptions and manages accounts. It does not see a client's obligations,
documents, notices or returns. That requires an explicit, time-boxed,
client-approved grant modelled as an ordinary
:class:`~stacos.engagements.models.Engagement`, exactly like a professional firm's
access — and the dealer relationship on ``Tenant`` grants nothing by itself.

This is not a limitation to relax later. It is the difference between a trusted
platform and a data-leak headline, and the reason nothing here has a foreign key
to an obligation.

**Commission is a ledger, not a balance.** Each accrual is a row that names the
subscription and the period it arose from, so a payout can be reconciled line by
line and a clawback is a negative entry rather than an edit. A dealer disputing a
payout is a conversation that a stored balance cannot survive.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.models import TenantScopedModel

__all__ = ["CommissionEntry", "CommissionPlan", "Payout"]


class CommissionPlan(TenantScopedModel):
    """What a dealer earns, and for how long.

    Tenant-scoped to the **dealer**, so a dealer can see their own terms and not
    another's. Rates in basis points because "12.5%" has to be exact.
    """

    class Basis(models.TextChoices):
        #: A share of every invoice, for as long as the customer pays.
        RECURRING = "RECURRING", _("Share of every invoice")
        #: Paid once, on the first invoice.
        FIRST_INVOICE = "FIRST_INVOICE", _("First invoice only")
        #: A share for a fixed number of months from signup.
        LIMITED_TERM = "LIMITED_TERM", _("Limited term")

    name = models.CharField(max_length=120)
    basis = models.CharField(max_length=16, choices=Basis.choices, default=Basis.RECURRING)
    rate_bps = models.PositiveSmallIntegerField(
        help_text=_("Basis points of net invoice value. 1500 is 15%.")
    )
    #: For LIMITED_TERM. Ignored otherwise.
    term_months = models.PositiveSmallIntegerField(default=12)

    #: A master distributor's share of a sub-dealer's commission. Nested one
    #: level only: a deeper tree is a multi-level marketing scheme, not a channel
    #: programme, and the accounting for it is not worth having.
    parent_share_bps = models.PositiveSmallIntegerField(default=0)

    valid_from = models.DateField(default=timezone.localdate)
    valid_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["tenant", "-valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=models.F("valid_from")),
                name="commissionplan_window_sane",
            ),
            models.CheckConstraint(
                condition=Q(rate_bps__lte=10_000), name="commissionplan_rate_sane"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.rate_bps / 100:.2f}%)"

    @property
    def rate(self) -> Decimal:
        return Decimal(self.rate_bps) / 10_000


class CommissionEntry(TenantScopedModel):
    """One accrual or clawback, naming exactly what it arose from.

    A ledger rather than a balance: a dealer disputing a payout needs to see
    which invoice each rupee came from, and a stored balance cannot answer that.
    A clawback on a refund is a negative row, never an edit to a positive one.
    """

    class Status(models.TextChoices):
        ACCRUED = "ACCRUED", _("Accrued")
        #: Included in a payout that has been approved but not yet paid.
        APPROVED = "APPROVED", _("Approved for payout")
        PAID = "PAID", _("Paid")
        #: Reversed because the underlying invoice was refunded or credited.
        CLAWED_BACK = "CLAWED_BACK", _("Clawed back")

    plan = models.ForeignKey(CommissionPlan, on_delete=models.PROTECT, related_name="entries")
    #: The customer this arose from. A tenant reference, and nothing more —
    #: naming the customer is not access to the customer.
    client_tenant = models.ForeignKey(
        "tenancy.Tenant", on_delete=models.CASCADE, related_name="commission_entries"
    )
    #: Loose reference to the invoice, which lives in the *client's* tenant. A
    #: hard foreign key across that boundary would let a dealer's row keep a
    #: client's invoice alive, and would invite a join that leaks.
    invoice_id = models.UUIDField(db_index=True)
    invoice_number = models.CharField(max_length=40, blank=True)

    #: In paise, signed. Negative for a clawback.
    amount_minor = models.BigIntegerField()
    #: What it was computed from, kept so the arithmetic can be re-checked years
    #: later without reconstructing the invoice.
    base_amount_minor = models.PositiveIntegerField(default=0)
    rate_bps = models.PositiveSmallIntegerField(default=0)

    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.ACCRUED, db_index=True
    )
    payout = models.ForeignKey(
        "Payout", on_delete=models.SET_NULL, null=True, blank=True, related_name="entries"
    )
    note = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One accrual per dealer per invoice. A webhook replay or a rerun of
            # the accrual job must not pay twice.
            models.UniqueConstraint(
                fields=["tenant", "invoice_id"],
                condition=Q(status__in=["ACCRUED", "APPROVED", "PAID"]),
                name="commission_one_accrual_per_invoice",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"], name="commission_tenant_status_idx"),
            models.Index(fields=["client_tenant"], name="commission_client_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.amount_minor / 100:.2f} on {self.invoice_number or self.invoice_id}"


class Payout(TenantScopedModel):
    """A batch of commission entries paid together."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        APPROVED = "APPROVED", _("Approved")
        PAID = "PAID", _("Paid")
        CANCELLED = "CANCELLED", _("Cancelled")

    reference = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)

    period_start = models.DateField()
    period_end = models.DateField()
    #: Recomputed from the entries on approval rather than accumulated as they
    #: are added, so the total and the lines cannot drift apart.
    total_minor = models.BigIntegerField(default=0)

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payouts_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_on = models.DateField(null=True, blank=True)
    payment_reference = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-period_end"]
        constraints = [
            models.CheckConstraint(
                condition=Q(period_end__gte=models.F("period_start")),
                name="payout_period_sane",
            ),
            models.CheckConstraint(
                condition=~Q(status="PAID") | Q(payment_reference__gt=""),
                name="payout_paid_has_reference",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"], name="payout_tenant_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.reference or 'Payout'} {self.period_start}–{self.period_end}"

    def recompute_total(self) -> int:
        """Total from the lines. Never accumulated as entries are attached."""
        total = self.entries.aggregate(total=Sum("amount_minor"))["total"]
        return int(total or 0)
