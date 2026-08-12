"""
The notice tracker.

A notice from a tax authority has a response deadline that is shorter and harder
than any recurring filing, and missing it forfeits the argument rather than
costing a fee. Everything here is shaped by that: a notice is dated the moment it
is recorded, its deadline is derived from the notice itself rather than from a
catalog rule, and it never leaves the register.

**On automated retrieval.** The adapter interface in :mod:`stacos.notices.portals`
exists and has no implementations. Automated retrieval from the income tax, GST
and MCA portals would require holding client credentials and driving a session
against terms of service that do not contemplate it — both legal exposures, and
neither worth taking before there is a product to protect. Notices reach STACOS
by **manual entry** and by **email ingestion**; the adapter interface is there so
that a written legal position, or an official API, becomes an adapter rather than
a rewrite. See ``docs/decisions.md``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import ClassVar

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.models import SoftDeleteModel, TenantScopedModel

__all__ = ["Notice", "NoticeEvent", "NoticeState", "NoticeType"]


class NoticeType(models.TextChoices):
    """Broad kind. The specific section lives in ``statutory_reference``."""

    SCRUTINY = "SCRUTINY", _("Scrutiny or assessment")
    DEMAND = "DEMAND", _("Demand")
    SHOW_CAUSE = "SHOW_CAUSE", _("Show cause")
    DEFECTIVE_RETURN = "DEFECTIVE_RETURN", _("Defective return")
    MISMATCH = "MISMATCH", _("Mismatch or intimation")
    PENALTY = "PENALTY", _("Penalty")
    REFUND = "REFUND", _("Refund")
    SUMMONS = "SUMMONS", _("Summons")
    INSPECTION = "INSPECTION", _("Inspection or survey")
    OTHER = "OTHER", _("Other")


class NoticeState(models.TextChoices):
    RECEIVED = "RECEIVED", _("Received")
    UNDER_REVIEW = "UNDER_REVIEW", _("Under review")
    INFO_REQUESTED = "INFO_REQUESTED", _("Information requested from client")
    DRAFTING = "DRAFTING", _("Drafting a response")
    PENDING_APPROVAL = "PENDING_APPROVAL", _("Pending client approval")
    RESPONDED = "RESPONDED", _("Responded")
    #: The authority came back. Common, and the reason a notice needs a timeline
    #: rather than a single response date.
    FOLLOW_UP = "FOLLOW_UP", _("Further correspondence received")
    CLOSED = "CLOSED", _("Closed")
    ESCALATED = "ESCALATED", _("Escalated to appeal")


OPEN_NOTICE_STATES = frozenset(
    {
        NoticeState.RECEIVED,
        NoticeState.UNDER_REVIEW,
        NoticeState.INFO_REQUESTED,
        NoticeState.DRAFTING,
        NoticeState.PENDING_APPROVAL,
        NoticeState.FOLLOW_UP,
    }
)


class Notice(TenantScopedModel, SoftDeleteModel):
    """One communication from an authority, and the work it created."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Source(models.TextChoices):
        MANUAL = "MANUAL", _("Entered by a user")
        EMAIL = "EMAIL", _("Ingested from email")
        #: Reserved. No portal adapter is implemented — see the module docstring.
        PORTAL = "PORTAL", _("Retrieved from a portal")

    class Risk(models.TextChoices):
        LOW = "LOW", _("Low")
        MEDIUM = "MEDIUM", _("Medium")
        HIGH = "HIGH", _("High")

    entity = models.ForeignKey("tenancy.Entity", on_delete=models.CASCADE, related_name="notices")
    authority = models.ForeignKey(
        "jurisdictions.Authority", on_delete=models.PROTECT, related_name="notices"
    )

    reference_number = models.CharField(
        max_length=120,
        help_text=_("The authority's own reference — DIN, notice number, order number."),
    )
    notice_type = models.CharField(max_length=20, choices=NoticeType.choices, db_index=True)
    statutory_reference = models.CharField(
        max_length=200, blank=True, help_text=_("e.g. 'Section 143(2) Income-tax Act'.")
    )
    subject = models.CharField(max_length=300)
    summary = models.TextField(blank=True)

    #: The period under examination, where the notice names one. Links a notice
    #: to the filings it is about without a foreign key that would break when the
    #: notice covers three years.
    period_key = models.CharField(max_length=32, blank=True, db_index=True)
    financial_years = models.CharField(max_length=120, blank=True)

    issued_on = models.DateField(null=True, blank=True)
    received_on = models.DateField(db_index=True)
    #: Read off the notice, not computed. A catalog rule cannot know that this
    #: particular officer gave fifteen days rather than thirty.
    respond_by = models.DateField(null=True, blank=True, db_index=True)

    demand_amount = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    #: What the client actually agrees is owed, once it has been looked at. The
    #: gap between this and ``demand_amount`` is the disputed figure, and it is
    #: the number a board asks about.
    accepted_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    state = models.CharField(
        max_length=20, choices=NoticeState.choices, default=NoticeState.RECEIVED, db_index=True
    )
    risk = models.CharField(max_length=8, choices=Risk.choices, default=Risk.MEDIUM)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.MANUAL)

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_notices",
    )
    responded_on = models.DateField(null=True, blank=True)
    response_reference = models.CharField(max_length=120, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.TextField(blank=True)

    class Meta:
        ordering = ["respond_by", "-received_on"]
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "authority", "reference_number"],
                condition=Q(archived_at__isnull=True),
                name="notice_reference_uniq",
            ),
            models.CheckConstraint(
                condition=Q(issued_on__isnull=True) | Q(received_on__gte=models.F("issued_on")),
                name="notice_received_after_issued",
            ),
        ]
        indexes = [
            # The screen everybody opens: what is open, soonest deadline first.
            models.Index(
                fields=["tenant", "respond_by"],
                name="notice_open_deadline_idx",
                condition=Q(state__in=sorted(str(s) for s in OPEN_NOTICE_STATES))
                & Q(archived_at__isnull=True),
            ),
            models.Index(fields=["entity", "-received_on"], name="notice_entity_recv_idx"),
            models.Index(fields=["assigned_to", "respond_by"], name="notice_assignee_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.reference_number} — {self.subject[:60]}"

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_NOTICE_STATES

    @property
    def days_remaining(self) -> int | None:
        if self.respond_by is None:
            return None
        return (self.respond_by - timezone.localdate()).days

    @property
    def is_overdue(self) -> bool:
        days = self.days_remaining
        return bool(self.is_open and days is not None and days < 0)

    @property
    def disputed_amount(self) -> object:
        """Demand less what has been accepted. ``None`` when there is no demand."""
        if self.demand_amount is None:
            return None
        return self.demand_amount - (self.accepted_amount or 0)

    def suggested_response_deadline(self) -> object:
        """A fallback deadline when the notice does not state one.

        Thirty days from receipt, flagged as a *suggestion* rather than written
        into ``respond_by``. Guessing a statutory deadline and presenting it as
        fact is exactly the mistake the calendar engine refuses to make, and this
        module holds the same line.
        """
        return self.received_on + timedelta(days=30)


class NoticeEvent(TenantScopedModel):
    """The correspondence trail. Append-only.

    A notice is rarely one exchange: the authority writes, the client responds,
    the authority writes again. Each step is a row, and the whole trail is what
    gets produced if the matter reaches appeal.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"
    ENFORCE_WRITE_SCOPE: ClassVar[bool] = False

    class Kind(models.TextChoices):
        RECEIVED = "RECEIVED", _("Notice received")
        STATE_CHANGED = "STATE_CHANGED", _("State changed")
        INFO_REQUESTED = "INFO_REQUESTED", _("Information requested")
        RESPONSE_DRAFTED = "RESPONSE_DRAFTED", _("Response drafted")
        RESPONSE_FILED = "RESPONSE_FILED", _("Response filed")
        FOLLOW_UP = "FOLLOW_UP", _("Further correspondence")
        HEARING = "HEARING", _("Hearing attended")
        ORDER = "ORDER", _("Order received")
        NOTE = "NOTE", _("Note")

    notice = models.ForeignKey(Notice, on_delete=models.CASCADE, related_name="events")
    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="notice_events"
    )

    kind = models.CharField(max_length=20, choices=Kind.choices)
    from_state = models.CharField(max_length=20, blank=True)
    to_state = models.CharField(max_length=20, blank=True)
    occurred_on = models.DateField(
        default=timezone.localdate,
        help_text=_("The date it happened, which is often not the date it was recorded."),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    actor_label = models.CharField(max_length=200, blank=True)
    note = models.TextField(blank=True)
    context = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["notice", "-occurred_at"], name="noticeevent_timeline_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} on {self.occurred_on}"
