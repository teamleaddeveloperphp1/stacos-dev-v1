"""
Plans, subscriptions, invoices and payments.

Money is the one part of a product where "mostly right" is not a grade. Three
things here exist because of that:

**Amounts are integer minor units.** Every figure is stored in paise, not in
rupees-with-decimals. ``Decimal`` would also be correct, but a single column type
that cannot be accidentally handed to a float is a stronger guarantee, and it
matches what every payment gateway actually sends and expects.

**An invoice is immutable once issued.** Correcting one means issuing a credit
note, because a tax invoice with a number is a document a client has filed
against and an auditor will ask about. Editing it in place makes two people's
records disagree with no way to reconcile them.

**Payments are idempotent on the provider's reference.** A gateway that retries a
webhook — and they all do — must not create a second payment. The unique
constraint is what makes that true rather than the handler being careful.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.ids import uuid7
from stacos.core.models import TenantScopedModel, TimeStampedModel

__all__ = [
    "Invoice",
    "InvoiceLine",
    "Payment",
    "Plan",
    "Subscription",
]


def to_minor(rupees: Decimal | int | str) -> int:
    """Rupees to paise. The only place a decimal becomes an integer."""
    return int((Decimal(str(rupees)) * 100).quantize(Decimal("1")))


def to_major(paise: int) -> Decimal:
    """Paise back to rupees, for display and for the ``inr`` filter."""
    return (Decimal(paise) / 100).quantize(Decimal("0.01"))


class Plan(TimeStampedModel):
    """A published price. Platform-owned reference data, like the catalog.

    Not tenant-scoped: every customer sees the same price list, and what differs
    is which plan they are on. A per-tenant price is a discount on a subscription,
    not a different plan — otherwise the price list becomes unenumerable and
    nobody can answer "what do we charge".
    """

    class Interval(models.TextChoices):
        MONTHLY = "MONTHLY", _("Monthly")
        ANNUAL = "ANNUAL", _("Annual")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    code = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)

    tenant_type = models.CharField(
        max_length=16,
        help_text=_("ORGANISATION, PRACTICE or DEALER — mirrors tenancy.Tenant.Type."),
    )
    interval = models.CharField(max_length=8, choices=Interval.choices, default=Interval.MONTHLY)
    #: In paise. See the module docstring.
    amount_minor = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="INR")

    included_entities = models.PositiveSmallIntegerField(default=1)
    included_users = models.PositiveSmallIntegerField(default=2)
    #: Charged per unit beyond what the plan includes, in paise.
    extra_entity_minor = models.PositiveIntegerField(default=0)
    extra_user_minor = models.PositiveIntegerField(default=0)

    features = models.JSONField(default=list, blank=True)
    is_public = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=100)

    objects = models.Manager()

    class Meta:
        ordering = ["tenant_type", "sort_order", "amount_minor"]

    def __str__(self) -> str:
        return self.name

    @property
    def amount(self) -> Decimal:
        return to_major(self.amount_minor)


class Subscription(TenantScopedModel):
    """What one customer is on, and until when."""

    class Status(models.TextChoices):
        TRIALING = "TRIALING", _("In trial")
        ACTIVE = "ACTIVE", _("Active")
        PAST_DUE = "PAST_DUE", _("Payment overdue")
        #: Data is never deleted for non-payment; the account goes read-only.
        SUSPENDED = "SUSPENDED", _("Suspended")
        CANCELLED = "CANCELLED", _("Cancelled")

    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.TRIALING, db_index=True
    )

    started_on = models.DateField(default=timezone.localdate)
    trial_ends_on = models.DateField(null=True, blank=True)
    current_period_start = models.DateField()
    current_period_end = models.DateField(db_index=True)
    cancelled_on = models.DateField(null=True, blank=True)

    #: Quantities, so an invoice can be recomputed rather than trusted.
    entity_count = models.PositiveSmallIntegerField(default=1)
    user_count = models.PositiveSmallIntegerField(default=1)

    #: A negotiated reduction, in basis points, so "12.5% off" is exact. A
    #: percentage in a float would drift by a rupee somewhere and nobody would
    #: find it.
    discount_bps = models.PositiveSmallIntegerField(
        default=0, help_text=_("Basis points. 1250 is 12.5%.")
    )

    #: Which dealer, if any, sold this. The commission ledger reads it; the
    #: dealer relationship grants no access to compliance data whatsoever.
    sold_by_dealer = models.ForeignKey(
        "tenancy.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sold_subscriptions",
        limit_choices_to={"type": "DEALER"},
    )

    provider = models.CharField(
        max_length=20,
        blank=True,
        help_text=_("stripe, payu, or empty for offline collection."),
    )
    provider_reference = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant"],
                condition=~Q(status="CANCELLED"),
                name="subscription_one_live_per_tenant",
            ),
            models.CheckConstraint(
                condition=Q(current_period_end__gt=models.F("current_period_start")),
                name="subscription_period_sane",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "current_period_end"], name="sub_status_period_idx"),
            models.Index(fields=["sold_by_dealer"], name="sub_dealer_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.plan} ({self.get_status_display()})"

    @property
    def is_live(self) -> bool:
        return self.status in {self.Status.TRIALING, self.Status.ACTIVE, self.Status.PAST_DUE}

    def compute_amount_minor(self) -> int:
        """What this period costs, in paise, before tax.

        Recomputed rather than stored, so a plan price change or a quantity
        change is picked up at the next invoice without a migration.
        """
        plan = self.plan
        gross = plan.amount_minor
        gross += max(0, self.entity_count - plan.included_entities) * plan.extra_entity_minor
        gross += max(0, self.user_count - plan.included_users) * plan.extra_user_minor
        discount = gross * self.discount_bps // 10_000
        return gross - discount


class Invoice(TenantScopedModel):
    """A tax invoice. Immutable once issued.

    Correcting one means a credit note. A tax invoice with a number is a document
    the client has filed against and an auditor will ask about; editing it in
    place makes two sets of records disagree with no way to reconcile them.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", _("Draft")
        ISSUED = "ISSUED", _("Issued")
        PAID = "PAID", _("Paid")
        PARTIALLY_PAID = "PARTIALLY_PAID", _("Partially paid")
        OVERDUE = "OVERDUE", _("Overdue")
        VOID = "VOID", _("Void")
        CREDITED = "CREDITED", _("Credited")

    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices",
    )

    number = models.CharField(
        max_length=40,
        blank=True,
        help_text=_("Assigned on issue, never reused. Blank while a draft."),
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT, db_index=True
    )

    issued_on = models.DateField(null=True, blank=True)
    due_on = models.DateField(null=True, blank=True, db_index=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    #: All in paise.
    subtotal_minor = models.PositiveIntegerField(default=0)
    tax_minor = models.PositiveIntegerField(default=0)
    total_minor = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=3, default="INR")

    #: GST on a SaaS subscription: 18% for a domestic supply. Stored per invoice
    #: rather than as a constant, because the rate is a fact about the invoice
    #: and it has changed before.
    tax_rate_bps = models.PositiveSmallIntegerField(default=1800)
    place_of_supply = models.CharField(max_length=12, blank=True)
    customer_gstin = models.CharField(max_length=20, blank=True)

    #: A void or credited invoice points at what replaced it.
    credited_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="credits"
    )
    void_reason = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["-issued_on", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["number"], condition=~Q(number=""), name="invoice_number_uniq"
            ),
            models.CheckConstraint(
                condition=Q(status="DRAFT") | Q(number__gt=""),
                name="invoice_issued_has_number",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "status"], name="invoice_tenant_status_idx"),
            models.Index(
                fields=["due_on"],
                name="invoice_unpaid_due_idx",
                condition=Q(status__in=["ISSUED", "PARTIALLY_PAID", "OVERDUE"]),
            ),
        ]

    def __str__(self) -> str:
        return self.number or f"Draft invoice {self.pk}"

    @property
    def total(self) -> Decimal:
        return to_major(self.total_minor)

    @property
    def is_editable(self) -> bool:
        return self.status == self.Status.DRAFT

    def paid_minor(self) -> int:
        total = self.payments.filter(status=Payment.Status.SUCCEEDED).aggregate(
            total=Sum("amount_minor")
        )["total"]
        return int(total or 0)

    @property
    def outstanding_minor(self) -> int:
        return max(0, self.total_minor - self.paid_minor())

    @property
    def is_overdue(self) -> bool:
        if self.due_on is None or self.status not in {
            self.Status.ISSUED,
            self.Status.PARTIALLY_PAID,
            self.Status.OVERDUE,
        }:
            return False
        return self.due_on < timezone.localdate() and self.outstanding_minor > 0


class InvoiceLine(TenantScopedModel):
    """One line. Amounts in paise, quantity as a decimal."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=250)
    quantity = models.DecimalField(max_digits=9, decimal_places=2, default=Decimal("1"))
    unit_amount_minor = models.PositiveIntegerField(default=0)
    amount_minor = models.PositiveIntegerField(default=0)
    #: SAC code for a service under GST. Required on a compliant tax invoice.
    sac_code = models.CharField(max_length=12, blank=True)

    class Meta:
        ordering = ["invoice", "id"]

    def __str__(self) -> str:
        return self.description


class Payment(TenantScopedModel):
    """Money received, against an invoice.

    Idempotent on ``provider_reference``: gateways retry webhooks, and a second
    row for one payment is a refund conversation nobody wants to have.
    """

    class Method(models.TextChoices):
        CARD = "CARD", _("Card")
        UPI = "UPI", _("UPI")
        NETBANKING = "NETBANKING", _("Net banking")
        MANDATE = "MANDATE", _("Standing mandate")
        #: Cheque or transfer, reconciled by hand. Still the majority of Indian
        #: B2B collection, so it is a first-class rail rather than an afterthought.
        OFFLINE = "OFFLINE", _("Bank transfer or cheque")
        CREDIT = "CREDIT", _("Credit note applied")

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        SUCCEEDED = "SUCCEEDED", _("Succeeded")
        FAILED = "FAILED", _("Failed")
        REFUNDED = "REFUNDED", _("Refunded")

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    amount_minor = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    currency = models.CharField(max_length=3, default="INR")
    method = models.CharField(max_length=12, choices=Method.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)

    provider = models.CharField(max_length=20, blank=True)
    #: The gateway's own id. Unique where present, which is what makes a retried
    #: webhook a no-op instead of a duplicate.
    provider_reference = models.CharField(max_length=120, blank=True)
    #: For offline payments: the UTR, cheque number or transfer reference.
    external_reference = models.CharField(max_length=120, blank=True)

    received_on = models.DateField(default=timezone.localdate)
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    failure_reason = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["-received_on", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_reference"],
                condition=~Q(provider_reference=""),
                name="payment_provider_reference_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["invoice", "status"], name="payment_invoice_status_idx"),
            models.Index(fields=["tenant", "-received_on"], name="payment_tenant_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{to_major(self.amount_minor)} by {self.get_method_display()}"

    @property
    def amount(self) -> Decimal:
        return to_major(self.amount_minor)
