"""
Information requests — the accountant's daily screen.

The single most common activity in a practice is asking a client for something
and then asking again. This module exists to make the asking structured, the
chasing automatic, and the answer land somewhere it can be used.

Two shapes matter:

**A request is a list, not a message.** "Send me the bank statements, the
purchase register and the TDS working" is three things with three states, and
tracking it as one blob means a client who sends two of them is neither done nor
not-done. :class:`RequestItem` is what makes partial progress visible and what
makes the reminder say *what is still missing* rather than "please respond".

**Requests attach to obligations, but do not require one.** Most requests exist
because a filing needs input; some exist because an auditor asked a question. A
mandatory foreign key would force the second case to invent an obligation.
"""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.ids import uuid7
from stacos.core.models import SoftDeleteModel, TenantScopedModel

__all__ = [
    "InformationRequest",
    "RequestEvent",
    "RequestItem",
    "RequestState",
    "ResponderToken",
]


class RequestState(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    SENT = "SENT", _("Sent")
    PARTIALLY_ANSWERED = "PARTIALLY_ANSWERED", _("Partially answered")
    ANSWERED = "ANSWERED", _("Answered")
    #: The preparer looked at what came back and it was not what was needed.
    CLARIFICATION_NEEDED = "CLARIFICATION_NEEDED", _("Clarification needed")
    CLOSED = "CLOSED", _("Closed")
    CANCELLED = "CANCELLED", _("Cancelled")


OPEN_REQUEST_STATES = frozenset(
    {
        RequestState.SENT,
        RequestState.PARTIALLY_ANSWERED,
        RequestState.CLARIFICATION_NEEDED,
    }
)


class InformationRequest(TenantScopedModel, SoftDeleteModel):
    """One ask, addressed to somebody, with a list of things in it."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Priority(models.TextChoices):
        LOW = "LOW", _("Low")
        NORMAL = "NORMAL", _("Normal")
        URGENT = "URGENT", _("Urgent")

    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="information_requests"
    )
    #: Null when the request is not about a specific filing. Set, it is what makes
    #: an obligation show "waiting on the client" rather than "not started".
    obligation = models.ForeignKey(
        "obligations.ObligationInstance",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="information_requests",
    )

    title = models.CharField(max_length=250)
    message = models.TextField(blank=True)
    category = models.CharField(max_length=32, blank=True, db_index=True)
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.NORMAL)

    state = models.CharField(
        max_length=24, choices=RequestState.choices, default=RequestState.DRAFT, db_index=True
    )

    #: A date, like every other deadline in this product. "Respond by 12
    #: September" is a day, not an instant.
    due_on = models.DateField(null=True, blank=True, db_index=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requests_raised",
    )
    #: Who owes the answer. A user where the client has an account; an email
    #: address where they do not, because a practice frequently deals with a
    #: bookkeeper who will never log in.
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requests_received",
    )
    assigned_email = models.EmailField(blank=True)

    sent_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    #: Reminders already sent, so escalation can step up rather than repeat. The
    #: schedule itself is a tenant setting; this is only the counter.
    reminder_count = models.PositiveSmallIntegerField(default=0)
    last_reminder_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_on", "-created_at"]
        constraints = [
            # A sent request has to have been sent at some point. Without this a
            # reminder job has no date to compute a schedule from.
            models.CheckConstraint(
                condition=Q(state="DRAFT") | Q(state="CANCELLED") | Q(sent_at__isnull=False),
                name="request_sent_has_timestamp",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "state", "due_on"], name="rfi_tenant_state_due_idx"),
            models.Index(fields=["assigned_to", "state"], name="rfi_assignee_state_idx"),
            models.Index(fields=["obligation"], name="rfi_obligation_idx"),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_REQUEST_STATES

    @property
    def is_overdue(self) -> bool:
        return bool(self.is_open and self.due_on and self.due_on < timezone.localdate())

    def progress(self) -> tuple[int, int]:
        """``(answered, total)`` across the items.

        Read from prefetched items when available so a list of fifty requests
        does not become fifty extra queries — the list view prefetches, and this
        method must not undo that by issuing its own.
        """
        items = list(self.items.all())
        return sum(1 for item in items if item.is_answered), len(items)


class RequestItem(TenantScopedModel):
    """One thing being asked for, with its own state.

    The unit of progress. A request for six documents that comes back with five
    is not "answered", and the reminder for it should name the sixth rather than
    repeating the whole list.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Kind(models.TextChoices):
        DOCUMENT = "DOCUMENT", _("A document")
        DATA = "DATA", _("A number or a date")
        CONFIRMATION = "CONFIRMATION", _("A yes or no")

    request = models.ForeignKey(InformationRequest, on_delete=models.CASCADE, related_name="items")
    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="request_items"
    )

    label = models.CharField(max_length=250)
    help_text = models.CharField(max_length=250, blank=True)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.DOCUMENT)
    is_mandatory = models.BooleanField(default=True)
    ordinal = models.PositiveSmallIntegerField(default=0)

    #: For DATA and CONFIRMATION items. Documents answer through the vault, so
    #: this stays empty for them and the link is the answer.
    response_value = models.CharField(max_length=500, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    #: Set when a preparer rejects what came back. Distinct from "not answered":
    #: the client did something, it was not enough, and they need to know why.
    rejection_reason = models.CharField(max_length=250, blank=True)
    #: Denormalised from the vault link count, so a list render does not have to
    #: count links per item.
    document_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["request", "ordinal"]
        indexes = [
            models.Index(fields=["request", "ordinal"], name="rfiitem_request_ord_idx"),
        ]

    def __str__(self) -> str:
        return self.label

    @property
    def is_answered(self) -> bool:
        if self.rejection_reason:
            return False
        if self.kind == self.Kind.DOCUMENT:
            return self.document_count > 0
        return bool(self.response_value)


class RequestEvent(TenantScopedModel):
    """The timeline of one request. Append-only."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"
    ENFORCE_WRITE_SCOPE: ClassVar[bool] = False

    class Kind(models.TextChoices):
        CREATED = "CREATED", _("Created")
        SENT = "SENT", _("Sent")
        REMINDED = "REMINDED", _("Reminder sent")
        RESPONDED = "RESPONDED", _("Client responded")
        REJECTED = "REJECTED", _("Response rejected")
        STATE_CHANGED = "STATE_CHANGED", _("State changed")
        NOTE = "NOTE", _("Note")

    request = models.ForeignKey(InformationRequest, on_delete=models.CASCADE, related_name="events")
    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="request_events"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
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
            models.Index(fields=["request", "-occurred_at"], name="rfievent_timeline_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.kind} at {self.occurred_at:%Y-%m-%d %H:%M}"


class ResponderToken(models.Model):
    """A link that lets one named outsider answer one request, and nothing else.

    A practice routinely needs something from a bookkeeper at a client who will
    never have a STACOS account. The product already modelled that — a request
    carries ``assigned_email`` for exactly this — but the road stopped there:
    ``services.notify`` returns early for such a recipient with a comment saying
    the email "goes out through the request's own delivery path", and no such
    path existed. The result was a request that could be addressed to somebody
    and never answered by them.

    **Deliberately not tenant-scoped**, the same exception and for the same
    reason as ``engagements.EngagementInvitation``: the holder is not a member of
    the tenant and has no scope of their own, so a tenant column would be a
    filter nothing could satisfy. Access is controlled by a single hashed token,
    an expiry, and the fact that the token names exactly one request. Listed in
    ``core.checks`` beside the invitation.

    **The token is never stored.** Only its HMAC lives here, so a database dump
    does not hand anybody a working link — the same treatment as a trusted-device
    secret and an engagement invitation.

    What it grants is stated in one place, ``services.responder_scope``: read the
    items of this one request, answer them, attach files to them. Not the other
    requests of the same entity, not the vault, not the calendar.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Active")
        USED = "USED", _("Used")
        REVOKED = "REVOKED", _("Revoked")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    request = models.ForeignKey(
        InformationRequest, on_delete=models.CASCADE, related_name="responder_tokens"
    )
    #: Who the link was sent to. Not a credential — anybody holding the link can
    #: use it — but it is what the audit trail names as the actor, and what makes
    #: revoking the right link possible.
    email = models.EmailField()

    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE)

    expires_at = models.DateTimeField()
    #: Bumped on every use rather than set once: the link stays usable until it
    #: expires, because "upload the bank statement, oh and the TDS working too"
    #: is one conversation and a single-use link makes it two support calls.
    last_used_at = models.DateTimeField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="responder_tokens_issued",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        indexes = [
            models.Index(fields=["request", "status"], name="rfitoken_request_idx"),
            models.Index(fields=["expires_at"], name="rfitoken_expiry_idx"),
        ]

    def __str__(self) -> str:
        return f"Responder link for {self.email}"

    @property
    def is_live(self) -> bool:
        return self.status == self.Status.ACTIVE and timezone.now() < self.expires_at
