"""
Corporate secretarial: meetings, resolutions, registers and the cap table.

This is the module a due-diligence exercise reads. Everything in it is shaped by
one question — can the company produce, on demand, an unbroken record of who
decided what, when, and with what authority?

Two things are less obvious:

**A meeting's date is what unblocks the calendar.** Recording an AGM here writes
an ``EntityEvent``, which is the anchor AOC-4 and MGT-7 compute their deadlines
from. Without that link, a company holds its AGM and its ROC filings stay
unscheduled — which is exactly the bug the engine's ``needs_input`` prompt exists
to surface.

**The cap table is a ledger, not a snapshot.** Holdings are derived by replaying
transactions, never stored as a current balance. A stored balance and a
transaction history disagree eventually, and when they do there is no way to tell
which is right — whereas a ledger can always be re-totalled.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import ClassVar

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.models import SoftDeleteModel, TenantScopedModel

__all__ = [
    "Meeting",
    "MeetingAttendee",
    "Resolution",
    "ShareTransaction",
    "Shareholder",
    "StatutoryRegister",
]


class Meeting(TenantScopedModel, SoftDeleteModel):
    """A board, committee or general meeting."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Kind(models.TextChoices):
        BOARD = "BOARD", _("Board meeting")
        AGM = "AGM", _("Annual general meeting")
        EGM = "EGM", _("Extraordinary general meeting")
        AUDIT_COMMITTEE = "AUDIT_COMMITTEE", _("Audit committee")
        CSR_COMMITTEE = "CSR_COMMITTEE", _("CSR committee")
        NRC = "NRC", _("Nomination and remuneration committee")
        OTHER = "OTHER", _("Other")

    class State(models.TextChoices):
        PLANNED = "PLANNED", _("Planned")
        NOTICE_ISSUED = "NOTICE_ISSUED", _("Notice issued")
        HELD = "HELD", _("Held")
        #: Minutes signed and entered in the minute book. The point at which the
        #: meeting becomes evidence rather than a diary entry.
        MINUTED = "MINUTED", _("Minuted")
        CANCELLED = "CANCELLED", _("Cancelled")

    entity = models.ForeignKey("tenancy.Entity", on_delete=models.CASCADE, related_name="meetings")
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PLANNED)

    serial_number = models.CharField(
        max_length=40,
        blank=True,
        help_text=_("The company's own numbering, e.g. '4th Board Meeting of FY 2026-27'."),
    )
    scheduled_for = models.DateField(db_index=True)
    held_on = models.DateField(null=True, blank=True)
    venue = models.CharField(max_length=250, blank=True)
    #: Video conferencing has been permitted for most business since 2020, and
    #: the minutes have to record which it was.
    is_video_conference = models.BooleanField(default=False)

    notice_issued_on = models.DateField(null=True, blank=True)
    quorum_required = models.PositiveSmallIntegerField(default=2)
    quorum_present = models.PositiveSmallIntegerField(null=True, blank=True)

    chairperson = models.CharField(max_length=200, blank=True)
    agenda = models.TextField(blank=True)
    minutes_signed_on = models.DateField(null=True, blank=True)

    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-scheduled_for"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(state__in=["HELD", "MINUTED"]) | Q(held_on__isnull=False),
                name="meeting_held_has_date",
            ),
        ]
        indexes = [
            models.Index(
                fields=["entity", "kind", "-scheduled_for"], name="meeting_entity_kind_idx"
            ),
            models.Index(fields=["tenant", "state"], name="meeting_tenant_state_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} — {self.held_on or self.scheduled_for}"

    @property
    def has_quorum(self) -> bool | None:
        """``None`` until attendance is recorded.

        Deliberately three-valued. "We do not know yet" and "quorum was not met"
        are very different facts about a meeting, and collapsing them would make
        an unrecorded meeting look invalid.
        """
        if self.quorum_present is None:
            return None
        return self.quorum_present >= self.quorum_required

    @property
    def event_key(self) -> str:
        """The calendar anchor this meeting supplies, if any.

        An AGM unblocks AOC-4, MGT-7 and ADT-1. A board meeting feeds the
        120-day interval rule. Everything else anchors nothing.
        """
        anchors: dict[str, str] = {
            str(self.Kind.AGM): "AGM_DATE",
            str(self.Kind.BOARD): "BOARD_MEETING",
            str(self.Kind.EGM): "EGM_DATE",
        }
        return anchors.get(str(self.kind), "")


class MeetingAttendee(TenantScopedModel):
    """Who was there, and in what capacity.

    Attendance is what proves quorum, and quorum is what makes a resolution
    valid. A minute book with no attendance register is not evidence of much.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Role(models.TextChoices):
        DIRECTOR = "DIRECTOR", _("Director")
        SHAREHOLDER = "SHAREHOLDER", _("Shareholder")
        AUDITOR = "AUDITOR", _("Auditor")
        COMPANY_SECRETARY = "COMPANY_SECRETARY", _("Company secretary")
        INVITEE = "INVITEE", _("Invitee")

    class Attendance(models.TextChoices):
        PRESENT = "PRESENT", _("Present")
        VIDEO = "VIDEO", _("Present by video")
        PROXY = "PROXY", _("By proxy")
        LEAVE = "LEAVE", _("Leave of absence")
        ABSENT = "ABSENT", _("Absent")

    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name="attendees")
    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="meeting_attendees"
    )

    name = models.CharField(max_length=200)
    din = models.CharField(
        max_length=20, blank=True, help_text=_("Director Identification Number.")
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.DIRECTOR)
    attendance = models.CharField(
        max_length=8, choices=Attendance.choices, default=Attendance.PRESENT
    )
    #: Recorded per meeting because interests change, and Section 184 requires
    #: an interested director to abstain from that item.
    is_interested = models.BooleanField(default=False)

    class Meta:
        ordering = ["meeting", "name"]
        constraints = [
            models.UniqueConstraint(fields=["meeting", "name"], name="attendee_meeting_name_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_attendance_display()})"

    @property
    def counts_towards_quorum(self) -> bool:
        return self.attendance in {self.Attendance.PRESENT, self.Attendance.VIDEO}


class Resolution(TenantScopedModel):
    """A decision taken at a meeting, or by circulation."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Kind(models.TextChoices):
        ORDINARY = "ORDINARY", _("Ordinary resolution")
        SPECIAL = "SPECIAL", _("Special resolution")
        BOARD = "BOARD", _("Board resolution")
        CIRCULAR = "CIRCULAR", _("Resolution by circulation")
        UNANIMOUS = "UNANIMOUS", _("Unanimous board resolution")

    meeting = models.ForeignKey(
        Meeting,
        on_delete=models.CASCADE,
        related_name="resolutions",
        null=True,
        blank=True,
        help_text=_("Null for a resolution passed by circulation."),
    )
    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="resolutions"
    )

    kind = models.CharField(max_length=12, choices=Kind.choices)
    item_number = models.CharField(max_length=20, blank=True)
    subject = models.CharField(max_length=300)
    text = models.TextField(blank=True)
    passed_on = models.DateField(db_index=True)

    votes_for = models.PositiveSmallIntegerField(null=True, blank=True)
    votes_against = models.PositiveSmallIntegerField(null=True, blank=True)
    abstentions = models.PositiveSmallIntegerField(null=True, blank=True)

    #: Several resolutions have to be filed with the Registrar within thirty
    #: days. Tracking it here rather than trusting somebody to remember is the
    #: difference between a clean file and a compounding application.
    requires_mgt14 = models.BooleanField(
        default=False, help_text=_("Special resolutions and certain board resolutions.")
    )
    mgt14_filed_on = models.DateField(null=True, blank=True)
    mgt14_srn = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-passed_on", "item_number"]
        indexes = [
            models.Index(fields=["entity", "-passed_on"], name="resolution_entity_date_idx"),
            models.Index(
                fields=["entity", "requires_mgt14"],
                name="resolution_mgt14_pending_idx",
                condition=Q(requires_mgt14=True) & Q(mgt14_filed_on__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return self.subject[:80]

    @property
    def mgt14_is_overdue(self) -> bool:
        """Filing due within thirty days of passing."""
        if not self.requires_mgt14 or self.mgt14_filed_on is not None:
            return False
        return timezone.localdate() > self.passed_on + timedelta(days=30)


class StatutoryRegister(TenantScopedModel):
    """One of the registers a company must keep, and when it was last checked.

    The entries themselves live in the modules that own them — members in the cap
    table, directors in the meeting attendees. This row is the *control*: which
    registers exist, where they are kept, and whether anybody has looked at them
    this year. That is what an inspection asks first.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Kind(models.TextChoices):
        MEMBERS = "MEMBERS", _("Register of members")
        DIRECTORS = "DIRECTORS", _("Register of directors and KMP")
        CHARGES = "CHARGES", _("Register of charges")
        CONTRACTS = "CONTRACTS", _("Register of contracts with related parties")
        LOANS = "LOANS", _("Register of loans, guarantees and investments")
        SBO = "SBO", _("Register of significant beneficial owners")
        RENEWED_SHARE_CERTIFICATES = "SHARE_CERTIFICATES", _("Register of share certificates")
        DEPOSITS = "DEPOSITS", _("Register of deposits")

    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="statutory_registers"
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    is_maintained = models.BooleanField(default=False)
    kept_at = models.CharField(max_length=250, blank=True)
    last_reviewed_on = models.DateField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["entity", "kind"]
        constraints = [
            models.UniqueConstraint(fields=["entity", "kind"], name="register_entity_kind_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} — {self.entity_id}"

    @property
    def review_is_stale(self) -> bool:
        if self.last_reviewed_on is None:
            return True
        return timezone.localdate() - self.last_reviewed_on > timedelta(days=365)


class Shareholder(TenantScopedModel):
    """A person or body holding shares.

    Identity only. What they hold is derived from the transaction ledger, never
    stored here — see the module docstring.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Kind(models.TextChoices):
        INDIVIDUAL = "INDIVIDUAL", _("Individual")
        BODY_CORPORATE = "BODY_CORPORATE", _("Body corporate")
        TRUST = "TRUST", _("Trust")
        FOREIGN = "FOREIGN", _("Foreign investor")

    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="shareholders"
    )
    name = models.CharField(max_length=250)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.INDIVIDUAL)
    folio_number = models.CharField(max_length=40, blank=True)
    pan = models.CharField(max_length=20, blank=True)
    #: Drives FC-GPR reporting and the foreign-shareholding fact the compliance
    #: engine reads. Stored rather than inferred from ``kind`` because an Indian
    #: body corporate can itself be foreign-owned.
    is_non_resident = models.BooleanField(default=False)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)

    class Meta:
        ordering = ["entity", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "folio_number"],
                condition=~Q(folio_number=""),
                name="shareholder_folio_uniq",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def holding_as_of(self, day: date | None = None) -> int:
        """Shares held on a date, by replaying the ledger.

        Re-totalled rather than read from a balance. A stored balance and a
        transaction history disagree eventually, and when they do nobody can tell
        which is right.
        """
        transactions = self.transactions.all()
        if day is not None:
            transactions = transactions.filter(executed_on__lte=day)
        total = transactions.aggregate(total=Sum("quantity"))["total"]
        return int(total or 0)


class ShareTransaction(TenantScopedModel):
    """One movement of shares. The cap table is the sum of these.

    ``quantity`` is signed: an allotment or a transfer in is positive, a transfer
    out or a buy-back is negative. One signed column beats two columns and a
    direction flag, which invites a row that is somehow both.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Kind(models.TextChoices):
        ALLOTMENT = "ALLOTMENT", _("Allotment")
        TRANSFER_IN = "TRANSFER_IN", _("Transfer in")
        TRANSFER_OUT = "TRANSFER_OUT", _("Transfer out")
        BUYBACK = "BUYBACK", _("Buy-back")
        BONUS = "BONUS", _("Bonus issue")
        SPLIT = "SPLIT", _("Split or consolidation")

    class ShareClass(models.TextChoices):
        EQUITY = "EQUITY", _("Equity")
        PREFERENCE = "PREFERENCE", _("Preference")
        CCPS = "CCPS", _("Compulsorily convertible preference")
        CCD = "CCD", _("Compulsorily convertible debenture")

    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="share_transactions"
    )
    shareholder = models.ForeignKey(
        Shareholder, on_delete=models.PROTECT, related_name="transactions"
    )
    resolution = models.ForeignKey(
        Resolution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="share_transactions",
        help_text=_("The board or shareholder resolution that authorised it."),
    )

    kind = models.CharField(max_length=16, choices=Kind.choices)
    share_class = models.CharField(
        max_length=12, choices=ShareClass.choices, default=ShareClass.EQUITY
    )
    #: Signed. Negative for anything leaving the holder.
    quantity = models.BigIntegerField()
    face_value = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    premium = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))
    consideration = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0"))

    executed_on = models.DateField(db_index=True)
    certificate_number = models.CharField(max_length=40, blank=True)
    distinctive_from = models.BigIntegerField(null=True, blank=True)
    distinctive_to = models.BigIntegerField(null=True, blank=True)

    #: Set for an allotment to a non-resident, which must be reported to the RBI
    #: in FC-GPR within thirty days.
    requires_fcgpr = models.BooleanField(default=False)
    fcgpr_filed_on = models.DateField(null=True, blank=True)

    note = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["entity", "executed_on", "created_at"]
        constraints = [
            models.CheckConstraint(condition=~Q(quantity=0), name="sharetxn_quantity_nonzero"),
        ]
        indexes = [
            models.Index(fields=["entity", "executed_on"], name="sharetxn_entity_date_idx"),
            models.Index(fields=["shareholder", "executed_on"], name="sharetxn_holder_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.quantity} on {self.executed_on}"
