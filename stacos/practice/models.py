"""
Practice management: the work board, time, and what the work was worth.

This is the module a firm's partners actually look at. Everything else in the
product tells a client whether they are compliant; this tells a practice whether
it is solvent.

Two decisions carry the weight:

**Work items belong to the practice, not to the client.** A ``WorkItem`` is owned
by the practice tenant and *points at* a client's obligation. That is not a
technicality: a client must never see the practice's internal estimate, its staff
assignment, or what it is charging — and the tenant boundary is what guarantees
that rather than a view filter somebody might forget.

**Work in progress is derived from time entries, never stored.** A stored WIP
balance and a time ledger disagree eventually, and when they do the number a
partner bills from is the wrong one. Rates change, entries get corrected, and a
recomputed total is always right.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.models import SoftDeleteModel, TenantScopedModel

__all__ = ["OPEN_WORK_STATES", "TimeEntry", "WorkItem", "WorkItemState"]


class WorkItemState(models.TextChoices):
    BACKLOG = "BACKLOG", _("Backlog")
    ASSIGNED = "ASSIGNED", _("Assigned")
    IN_PROGRESS = "IN_PROGRESS", _("In progress")
    BLOCKED = "BLOCKED", _("Blocked")
    IN_REVIEW = "IN_REVIEW", _("In review")
    DONE = "DONE", _("Done")


OPEN_WORK_STATES = frozenset(
    {
        WorkItemState.BACKLOG,
        WorkItemState.ASSIGNED,
        WorkItemState.IN_PROGRESS,
        WorkItemState.BLOCKED,
        WorkItemState.IN_REVIEW,
    }
)


class WorkItem(TenantScopedModel, SoftDeleteModel):
    """One card on the practice's board.

    Tenant-scoped to the **practice**, which is the point. The client tenant owns
    the obligation; the practice owns its own view of the work, its estimate and
    its assignment, and the tenant boundary keeps those apart without anybody
    having to remember a filter.
    """

    class Priority(models.TextChoices):
        LOW = "LOW", _("Low")
        NORMAL = "NORMAL", _("Normal")
        HIGH = "HIGH", _("High")
        CRITICAL = "CRITICAL", _("Critical")

    title = models.CharField(max_length=250)
    description = models.TextField(blank=True)
    state = models.CharField(
        max_length=12, choices=WorkItemState.choices, default=WorkItemState.BACKLOG, db_index=True
    )
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.NORMAL)

    #: The client this is for. A tenant rather than an entity, because a piece of
    #: work often spans several of a group's entities.
    client_tenant = models.ForeignKey(
        "tenancy.Tenant",
        on_delete=models.CASCADE,
        related_name="practice_work_items",
        null=True,
        blank=True,
    )
    #: Loose references rather than foreign keys: the target lives in another
    #: tenant's tables, and a hard FK across that boundary is a cascade waiting to
    #: happen. The board renders from its own columns and follows the link only
    #: when a user opens the card.
    obligation_id = models.UUIDField(null=True, blank=True, db_index=True)
    obligation_title = models.CharField(max_length=200, blank=True)
    due_on = models.DateField(null=True, blank=True, db_index=True)

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_items",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_items_reviewing",
    )

    estimated_hours = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    #: What the client is charged, where the work is billed at a fixed price
    #: rather than at hourly rates. Null means "bill the time".
    fixed_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    blocked_reason = models.CharField(max_length=250, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_on", "-priority", "-created_at"]
        indexes = [
            models.Index(fields=["tenant", "state", "due_on"], name="work_tenant_state_due_idx"),
            models.Index(fields=["assigned_to", "state"], name="work_assignee_state_idx"),
            models.Index(fields=["client_tenant", "state"], name="work_client_state_idx"),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_WORK_STATES

    @property
    def is_overdue(self) -> bool:
        return bool(self.is_open and self.due_on and self.due_on < timezone.localdate())

    def hours_logged(self) -> Decimal:
        total = self.time_entries.aggregate(total=Sum("hours"))["total"]
        return Decimal(total or 0)

    def wip_value(self) -> Decimal:
        """Unbilled value, recomputed from the time ledger.

        Never stored. Rates change and entries get corrected; a stored balance
        would be the number a partner bills from and the wrong one.
        """
        total = self.time_entries.filter(is_billable=True, invoiced_at__isnull=True).aggregate(
            total=Sum(models.F("hours") * models.F("rate"), output_field=models.DecimalField())
        )["total"]
        return Decimal(total or 0)

    @property
    def is_over_estimate(self) -> bool:
        if self.estimated_hours is None:
            return False
        return self.hours_logged() > self.estimated_hours


class TimeEntry(TenantScopedModel):
    """One block of time, at the rate that applied when it was worked.

    The rate is copied onto the row rather than read from the person's current
    rate card. A rate rise in April must not silently reprice work done in March,
    and re-reading it live is exactly how that happens.
    """

    work_item = models.ForeignKey(
        WorkItem, on_delete=models.CASCADE, related_name="time_entries", null=True, blank=True
    )
    #: Denormalised so time can be logged against a client without a card, and so
    #: the client profitability report is one query.
    client_tenant = models.ForeignKey(
        "tenancy.Tenant",
        on_delete=models.CASCADE,
        related_name="practice_time_entries",
        null=True,
        blank=True,
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="time_entries"
    )
    worked_on = models.DateField(db_index=True)
    hours = models.DecimalField(
        max_digits=5, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    #: Frozen at the moment of entry. See the class docstring.
    rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    is_billable = models.BooleanField(default=True)
    narrative = models.CharField(
        max_length=300,
        blank=True,
        help_text=_("What was done. Appears on the client's invoice."),
    )

    #: Set when the entry is swept into an invoice. Its presence is what makes
    #: an entry stop counting towards work in progress.
    invoiced_at = models.DateTimeField(null=True, blank=True)
    invoice_id = models.UUIDField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-worked_on", "-created_at"]
        constraints = [
            # A billable hour with no rate is a hole in the WIP number that
            # nobody notices until the month-end report is short.
            models.CheckConstraint(
                condition=Q(is_billable=False) | Q(rate__gt=0),
                name="timeentry_billable_has_rate",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "worked_on"], name="time_tenant_date_idx"),
            models.Index(fields=["user", "worked_on"], name="time_user_date_idx"),
            models.Index(
                fields=["client_tenant", "worked_on"],
                name="time_unbilled_idx",
                condition=Q(invoiced_at__isnull=True) & Q(is_billable=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.hours}h on {self.worked_on}"

    @property
    def value(self) -> Decimal:
        return self.hours * self.rate if self.is_billable else Decimal("0")


class RateCard(TenantScopedModel):
    """What a person's time is worth, over a validity window.

    Effective-dated rather than a single column on the user, so that a rate rise
    applies from a date and historical entries keep the rate that was in force.
    The alternative — one current rate — silently reprices last quarter every
    time somebody gets a raise.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rate_cards"
    )
    rate = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["user", "-valid_from"]
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gte=models.F("valid_from")),
                name="ratecard_window_sane",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-valid_from"], name="ratecard_user_from_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user} at {self.rate} from {self.valid_from}"
