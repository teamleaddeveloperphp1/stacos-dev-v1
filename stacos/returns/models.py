"""
Return preparation: the working papers behind a filing.

An obligation says *that* GSTR-3B for July is due. A preparation says *what is in
it*, who put it there, what it was reconciled against, and who approved it. The
two are separate on purpose — an obligation is generated and can be superseded by
the engine, while a preparation is human work that must never be.

**Maker-checker is enforced by a constraint, not by a convention.** The database
refuses a row where the approver is the preparer. Every firm says it separates
those roles; under deadline pressure at 11 p.m. on the 20th, the one person still
in the office does both, and a rule that only lives in a code path is a rule that
is quietly not followed on exactly the filings that matter most.

**Reconciliations are first-class rows.** "GSTR-2B against the purchase register"
produces a set of differences, each of which is either explained or carried
forward. Storing that as a PDF attachment loses the ability to ask "what is still
unexplained", which is the only question worth asking about it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.models import SoftDeleteModel, TenantScopedModel

__all__ = [
    "OPEN_PREPARATION_STATES",
    "PreparationState",
    "Reconciliation",
    "ReconciliationDifference",
    "ReturnPreparation",
]


class PreparationState(models.TextChoices):
    """Where the working papers have got to.

    Deliberately narrower than the obligation lifecycle: a preparation is only
    ever being made, checked, approved or filed. Everything else about the
    obligation — deferral, dispute, non-applicability — belongs on the obligation.
    """

    DRAFT = "DRAFT", _("Draft")
    #: The maker says it is finished. The checker has not looked yet.
    PREPARED = "PREPARED", _("Prepared")
    #: Sent back by the checker with findings.
    REWORK = "REWORK", _("Sent back for rework")
    #: The checker is satisfied. Awaiting the client's sign-off where required.
    REVIEWED = "REVIEWED", _("Reviewed")
    APPROVED = "APPROVED", _("Approved for filing")
    FILED = "FILED", _("Filed")


OPEN_PREPARATION_STATES = frozenset(
    {
        PreparationState.DRAFT,
        PreparationState.PREPARED,
        PreparationState.REWORK,
        PreparationState.REVIEWED,
        PreparationState.APPROVED,
    }
)


class ReturnPreparation(TenantScopedModel, SoftDeleteModel):
    """One return being prepared, for one obligation."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="return_preparations"
    )
    #: One preparation per obligation. The obligation carries the deadline and
    #: the scope; this carries the numbers.
    obligation = models.OneToOneField(
        "obligations.ObligationInstance",
        on_delete=models.CASCADE,
        related_name="preparation",
    )

    form_type = models.CharField(
        max_length=40,
        db_index=True,
        help_text=_("GSTR-3B, GSTR-1, 24Q, ITR-6 — denormalised from the definition."),
    )
    period_key = models.CharField(max_length=32, db_index=True)

    state = models.CharField(
        max_length=12,
        choices=PreparationState.choices,
        default=PreparationState.DRAFT,
        db_index=True,
    )

    #: The figures. A JSON document rather than a table per form, because there
    #: are forty form types and their shapes change every budget — a schema per
    #: form would be forty migrations a year and would still be behind.
    figures = models.JSONField(default=dict, blank=True)
    #: What the client will actually pay. Extracted from ``figures`` so it can be
    #: totalled, compared and shown without parsing JSON in SQL.
    tax_payable = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    interest_payable = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    late_fee = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    # -- Maker-checker ------------------------------------------------------
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returns_prepared",
    )
    prepared_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returns_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returns_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    review_notes = models.TextField(blank=True)

    # -- Filing -------------------------------------------------------------
    filed_on = models.DateField(null=True, blank=True)
    filing_reference = models.CharField(max_length=120, blank=True)
    filed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="returns_filed",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Maker-checker, enforced by PostgreSQL rather than by good
            # intentions. The one person still in the office at 11 p.m. on the
            # 20th cannot be both, whatever the code path says.
            models.CheckConstraint(
                condition=Q(reviewed_by__isnull=True)
                | Q(prepared_by__isnull=True)
                | ~Q(reviewed_by=models.F("prepared_by")),
                name="preparation_reviewer_is_not_preparer",
            ),
            models.CheckConstraint(
                condition=~Q(state="FILED") | Q(filed_on__isnull=False),
                name="preparation_filed_has_date",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "state"], name="prep_tenant_state_idx"),
            models.Index(fields=["entity", "form_type", "period_key"], name="prep_form_period_idx"),
            models.Index(fields=["prepared_by", "state"], name="prep_maker_state_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.form_type} {self.period_key}"

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_PREPARATION_STATES

    @property
    def total_payable(self) -> Decimal:
        return (
            (self.tax_payable or Decimal("0"))
            + (self.interest_payable or Decimal("0"))
            + (self.late_fee or Decimal("0"))
        )

    @property
    def unexplained_differences(self) -> int:
        """Differences nobody has accounted for yet.

        The number that decides whether a return is ready. A reconciliation with
        forty rows and none explained is not a reconciliation.
        """
        return sum(
            reconciliation.unexplained_count for reconciliation in self.reconciliations.all()
        )


class Reconciliation(TenantScopedModel):
    """One comparison run for a preparation.

    Kept as a row rather than an attachment so that "what is still unexplained"
    is a query. A PDF of a reconciliation answers nothing.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Kind(models.TextChoices):
        GSTR2B_VS_BOOKS = "GSTR2B_VS_BOOKS", _("GSTR-2B against purchase register")
        GSTR1_VS_BOOKS = "GSTR1_VS_BOOKS", _("GSTR-1 against sales register")
        GSTR3B_VS_GSTR1 = "GSTR3B_VS_GSTR1", _("GSTR-3B against GSTR-1")
        FORM26AS_VS_BOOKS = "FORM26AS_VS_BOOKS", _("Form 26AS against books")
        BOOKS_VS_BANK = "BOOKS_VS_BANK", _("Books against bank")
        OTHER = "OTHER", _("Other")

    preparation = models.ForeignKey(
        ReturnPreparation, on_delete=models.CASCADE, related_name="reconciliations"
    )
    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="reconciliations"
    )

    kind = models.CharField(max_length=24, choices=Kind.choices)
    #: What each side totalled. Two numbers and their difference is the summary a
    #: reviewer looks at before opening a single line.
    left_total = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    right_total = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))

    run_at = models.DateTimeField(default=timezone.now)
    run_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-run_at"]
        indexes = [
            models.Index(fields=["preparation", "-run_at"], name="recon_prep_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} ({self.variance})"

    @property
    def variance(self) -> Decimal:
        return self.left_total - self.right_total

    @property
    def unexplained_count(self) -> int:
        return sum(1 for row in self.differences.all() if not row.is_resolved)


class ReconciliationDifference(TenantScopedModel):
    """One line that does not agree, and what was decided about it."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Resolution(models.TextChoices):
        UNRESOLVED = "UNRESOLVED", _("Not yet explained")
        TIMING = "TIMING", _("Timing difference — will reverse")
        SUPPLIER_ERROR = "SUPPLIER_ERROR", _("Supplier has not filed")
        BOOKS_ERROR = "BOOKS_ERROR", _("Our books are wrong")
        ACCEPTED = "ACCEPTED", _("Accepted as a permanent difference")
        CARRIED_FORWARD = "CARRIED_FORWARD", _("Carried forward to the next period")

    reconciliation = models.ForeignKey(
        Reconciliation, on_delete=models.CASCADE, related_name="differences"
    )
    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="reconciliation_differences"
    )

    #: Whatever identifies the line on both sides — an invoice number, a GSTIN, a
    #: TAN. Free text because the two sides rarely agree on a key, which is
    #: usually the reason they do not reconcile.
    reference = models.CharField(max_length=120)
    counterparty = models.CharField(max_length=200, blank=True)
    left_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    right_amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))

    resolution = models.CharField(
        max_length=16, choices=Resolution.choices, default=Resolution.UNRESOLVED
    )
    resolution_note = models.CharField(max_length=250, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["reference"]
        indexes = [
            models.Index(fields=["reconciliation", "resolution"], name="recondiff_resolution_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.reference}: {self.difference}"

    @property
    def difference(self) -> Decimal:
        return self.left_amount - self.right_amount

    @property
    def is_resolved(self) -> bool:
        return self.resolution != self.Resolution.UNRESOLVED
