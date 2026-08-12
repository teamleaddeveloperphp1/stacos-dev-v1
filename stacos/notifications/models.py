"""
Telling people things.

Three modules in this product know a deadline is approaching and have, until
now, had no way to say so. That is the gap this closes — and the reason it is a
module rather than a `send_mail` call in each of them is that the hard parts are
all shared:

**Idempotence.** A reminder sweep runs every hour and must not send the same
message twice. Every notification carries a ``dedupe_key`` — "obligation:<id>:T-3"
— with a unique constraint behind it, so the sweep can be careless and the
mailbox stays clean. Without this, the first outage that causes a re-run
carpet-bombs every client the firm has.

**Delivery is recorded per channel.** "Did the client actually get told" is a
question this product must answer years later, during a dispute about who knew
what. A boolean `sent` cannot answer it; one row per channel attempt can.

**Urgency decides the channel, not the sender.** A caller says what happened and
how much it matters. Whether that becomes a WhatsApp message at 9am, an email
now, or a line in tomorrow's digest is the recipient's preference and this
module's business, and no caller should be making that decision.

One constraint from outside is worth stating plainly, because it has a lead time
measured in weeks: **business-initiated WhatsApp messages need templates approved
by Meta in advance**, and reminders are business-initiated by definition. The
templates are declared in `stacos.notifications.templates`; until each is
approved, WhatsApp delivery for that kind fails and the email still goes.
"""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.models import TenantScopedModel

__all__ = [
    "Channel",
    "Delivery",
    "DeliveryState",
    "DigestFrequency",
    "Notification",
    "NotificationKind",
    "NotificationPreference",
    "Severity",
    "SubjectType",
]


class NotificationKind(models.TextChoices):
    """What happened.

    Kept as a closed list rather than free text because a user's preferences are
    expressed in these terms — "stop telling me about invoices" — and because a
    kind maps to a WhatsApp template that had to be approved by name.
    """

    OBLIGATION_DUE = "OBLIGATION_DUE", _("A filing is coming up")
    OBLIGATION_OVERDUE = "OBLIGATION_OVERDUE", _("A filing is overdue")
    OBLIGATION_DATE_CHANGED = "OBLIGATION_DATE_CHANGED", _("A due date moved")
    REQUEST_SENT = "REQUEST_SENT", _("Information has been requested from you")
    REQUEST_REMINDER = "REQUEST_REMINDER", _("A reminder about information requested")
    REQUEST_ANSWERED = "REQUEST_ANSWERED", _("A client has answered a request")
    NOTICE_RECEIVED = "NOTICE_RECEIVED", _("A notice has been recorded")
    NOTICE_DEADLINE = "NOTICE_DEADLINE", _("A notice deadline is approaching")
    RETURN_REVIEW = "RETURN_REVIEW", _("A return is waiting for review")
    WORK_ASSIGNED = "WORK_ASSIGNED", _("Work has been assigned to you")
    INVOICE_ISSUED = "INVOICE_ISSUED", _("An invoice has been issued")
    INVOICE_OVERDUE = "INVOICE_OVERDUE", _("An invoice is overdue")
    DOCUMENT_QUARANTINED = "DOCUMENT_QUARANTINED", _("A file failed its virus scan")
    DIGEST = "DIGEST", _("Your summary")


class Severity(models.TextChoices):
    """How loudly to say it.

    The whole point of the scale is that ``URGENT`` ignores the digest setting.
    A user who asked for a daily summary still wants to hear today that a notice
    expires tomorrow; treating "batch my notifications" as "batch everything" is
    how a product becomes responsible for a missed statutory deadline.
    """

    INFO = "INFO", _("For information")
    ATTENTION = "ATTENTION", _("Needs attention")
    URGENT = "URGENT", _("Urgent")


class SubjectType(models.TextChoices):
    """What the notification is about, so the UI can link to it and the sweep can
    find whether one was already raised."""

    OBLIGATION = "OBLIGATION", _("Obligation")
    REQUEST = "REQUEST", _("Information request")
    NOTICE = "NOTICE", _("Notice")
    RETURN = "RETURN", _("Return")
    WORK_ITEM = "WORK_ITEM", _("Work item")
    INVOICE = "INVOICE", _("Invoice")
    DOCUMENT = "DOCUMENT", _("Document")
    NONE = "NONE", _("Nothing in particular")


class Channel(models.TextChoices):
    IN_APP = "IN_APP", _("In the app")
    EMAIL = "EMAIL", _("Email")
    WHATSAPP = "WHATSAPP", _("WhatsApp")


class DeliveryState(models.TextChoices):
    """What became of one attempt on one channel.

    ``SUPPRESSED`` and ``NOT_REACHABLE`` are not failures and must not be
    retried: the first is the user's own preference and the second is a phone
    with no WhatsApp account. Retrying either wastes messages the tenant pays for
    and, in the second case, can never succeed.
    """

    PENDING = "PENDING", _("Queued")
    SENT = "SENT", _("Sent")
    FAILED = "FAILED", _("Delivery failed")
    SUPPRESSED = "SUPPRESSED", _("Not sent — the recipient opted out")
    NOT_REACHABLE = "NOT_REACHABLE", _("Not sent — the recipient cannot be reached here")


class DigestFrequency(models.TextChoices):
    IMMEDIATE = "IMMEDIATE", _("As things happen")
    DAILY = "DAILY", _("Once a day")
    WEEKLY = "WEEKLY", _("Once a week")
    OFF = "OFF", _("In the app only")


class Notification(TenantScopedModel):
    """One thing worth telling one person."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    entity = models.ForeignKey(
        "tenancy.Entity",
        on_delete=models.CASCADE,
        related_name="notifications",
        null=True,
        blank=True,
    )

    kind = models.CharField(max_length=32, choices=NotificationKind.choices, db_index=True)
    severity = models.CharField(
        max_length=10, choices=Severity.choices, default=Severity.INFO, db_index=True
    )

    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    #: Where the notification takes you. Relative, because an email rendering it
    #: absolute needs the site's own base URL and a stored absolute URL goes
    #: stale the moment a deployment moves.
    url = models.CharField(max_length=400, blank=True)

    subject_type = models.CharField(
        max_length=16, choices=SubjectType.choices, default=SubjectType.NONE
    )
    subject_id = models.UUIDField(null=True, blank=True)

    #: The idempotency key. Unique per tenant, and the reason a reminder sweep can
    #: run every hour without anybody's inbox suffering for it.
    dedupe_key = models.CharField(max_length=200)

    read_at = models.DateTimeField(null=True, blank=True)
    #: Set when the notification has been folded into a digest, so tomorrow's
    #: digest does not repeat it.
    digested_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "recipient", "dedupe_key"],
                name="notification_dedupe_uniq",
            ),
        ]
        indexes = [
            # The bell counter: unread, newest first, for one person. Partial, so
            # the index stays small as read notifications accumulate forever.
            models.Index(
                fields=["recipient", "-created_at"],
                condition=Q(read_at__isnull=True),
                name="notification_unread_idx",
            ),
            models.Index(fields=["tenant", "-created_at"], name="notification_tenant_time_idx"),
            models.Index(fields=["subject_type", "subject_id"], name="notification_subject_idx"),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def mark_read(self) -> None:
        if self.read_at is None:
            self.read_at = timezone.now()
            self.save(update_fields=["read_at", "updated_at"])


class Delivery(TenantScopedModel):
    """One attempt to get one notification to one person on one channel.

    Exists so that "we told them" is a claim with evidence behind it. During a
    dispute about a missed deadline, the question is not whether the platform
    raised a notification — it is whether the message left the building, and on
    which channel, and when.
    """

    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="deliveries"
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    state = models.CharField(
        max_length=14, choices=DeliveryState.choices, default=DeliveryState.PENDING
    )
    #: Where it went — an email address or an E.164 number. Recorded at send time
    #: because a user changing their email later must not rewrite history.
    destination = models.CharField(max_length=200, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "channel"], name="delivery_channel_uniq"
            ),
        ]
        indexes = [
            models.Index(fields=["state", "created_at"], name="delivery_state_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.channel} → {self.state}"


class NotificationPreference(TenantScopedModel):
    """How one person wants to be told, in one tenant.

    Per tenant rather than per user: somebody who is a partner in a practice and
    a director of their own company reasonably wants everything from the first
    and only urgent things from the second.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences"
    )

    email_enabled = models.BooleanField(default=True)
    #: Off by default, and deliberately so. WhatsApp costs money per message and
    #: is the most intrusive channel here; making a customer opt in is both the
    #: polite default and the one that does not surprise them with a bill.
    whatsapp_enabled = models.BooleanField(default=False)

    digest = models.CharField(
        max_length=10, choices=DigestFrequency.choices, default=DigestFrequency.IMMEDIATE
    )
    #: Local hour the digest is sent. 07:00 puts it in front of somebody before
    #: the working day rather than during it.
    digest_hour = models.PositiveSmallIntegerField(default=7)

    #: Kinds this user has switched off entirely. Urgent notifications are still
    #: raised in-app: a preference is not permission to withhold a statutory
    #: deadline, only to stop mailing about it.
    muted_kinds = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "user"], name="notification_prefs_uniq"),
        ]

    def __str__(self) -> str:
        return f"Notification preferences for {self.user_id}"

    def allows(self, *, kind: str, severity: str, channel: str) -> bool:
        """Whether this channel may carry this notification.

        In-app is never suppressed. It is the record that the platform raised the
        matter, and a user who has muted their email still has to be able to find
        out why their filing was late.
        """
        if channel == Channel.IN_APP:
            return True
        if kind in (self.muted_kinds or []) and severity != Severity.URGENT:
            return False
        if channel == Channel.EMAIL:
            return self.email_enabled
        if channel == Channel.WHATSAPP:
            return self.whatsapp_enabled
        return False

    def defers_to_digest(self, *, severity: str) -> bool:
        """Whether this waits for the digest instead of going out now."""
        if severity == Severity.URGENT:
            return False
        return self.digest in {DigestFrequency.DAILY, DigestFrequency.WEEKLY}
