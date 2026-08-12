"""
Engagements: the only route by which one tenant sees another's data.

Everything cross-tenant in STACOS flows through this model and nothing else.
That is a deliberate constraint rather than an implementation detail — it means
the answer to "how can this firm see this client's filings?" is always a single
row someone approved, with a start date, an end date, and a scope.

The organisation owns its data. A practice's access is granted, scoped, and
revocable; when the engagement ends the access ends the same second, and the
organisation keeps everything.

**Who owns the row?** The client organisation's tenant. The practice can read it,
but the organisation is the data owner, so it is the organisation's tenant id on
the row and the organisation who can always revoke.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.ids import uuid7
from stacos.core.models import TenantScopedModel, TimeStampedModel
from stacos.tenancy.models import ComplianceCategory, Entity, Tenant

__all__ = ["Engagement", "EngagementInvitation"]


class Engagement(TenantScopedModel):
    """A practice's scoped access to one entity of one organisation."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Status(models.TextChoices):
        INVITED = "INVITED", _("Invited")
        ACTIVE = "ACTIVE", _("Active")
        SUSPENDED = "SUSPENDED", _("Suspended")
        ENDED = "ENDED", _("Ended")
        DECLINED = "DECLINED", _("Declined")

    class Side(models.TextChoices):
        ORGANISATION = "ORGANISATION", _("The business invited the firm")
        PRACTICE = "PRACTICE", _("The firm invited the business")

    #: `tenant` (from TenantScopedModel) is the CLIENT organisation's tenant.
    practice_tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="engagements",
        limit_choices_to={"type": Tenant.Type.PRACTICE},
    )
    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="engagements")

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INVITED)
    initiated_by_side = models.CharField(max_length=16, choices=Side.choices)

    #: Empty means every category — used for a full-service retainer. A scoped
    #: engagement (a GST specialist, a labour-law consultant) lists only theirs.
    categories = ArrayField(
        models.CharField(max_length=32, choices=ComplianceCategory.choices),
        default=list,
        blank=True,
    )
    #: Permission codes granted to the practice's members for this entity. Never
    #: inherited from the practice's own roles — the client decides the ceiling.
    permissions = models.JSONField(default=list, blank=True)

    starts_on = models.DateField(default=timezone.localdate)
    ends_on = models.DateField(null=True, blank=True)

    engagement_letter_ref = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)

    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="engagements_invited",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="engagements_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_reason = models.CharField(max_length=250, blank=True)
    # "Who revoked this firm's access, and when" is one of the first questions
    # asked in a dispute, so it is recorded on the row rather than left to be
    # reconstructed from the audit log.
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="engagements_ended",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One live engagement per (practice, entity). A firm re-engaged after
            # a gap gets a new row, so the history of who had access when is
            # preserved rather than overwritten.
            models.UniqueConstraint(
                fields=["practice_tenant", "entity"],
                condition=models.Q(status__in=["INVITED", "ACTIVE", "SUSPENDED"]),
                name="engagement_live_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(ends_on__isnull=True)
                | models.Q(ends_on__gte=models.F("starts_on")),
                name="engagement_window_sane",
            ),
        ]
        indexes = [
            # The bootstrap query: "which client entities can this practice see?"
            models.Index(
                fields=["practice_tenant", "status"], name="engagement_practice_status_idx"
            ),
            models.Index(fields=["tenant", "status"], name="engagement_client_status_idx"),
            models.Index(fields=["entity", "status"], name="engagement_entity_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.practice_tenant} ↔ {self.entity}"

    def clean(self) -> None:
        super().clean()
        if self.practice_tenant_id and self.practice_tenant.type != Tenant.Type.PRACTICE:
            raise ValidationError(
                {"practice_tenant": "Only a professional firm can hold an engagement."}
            )
        if self.entity_id and self.tenant_id and self.entity.tenant_id != self.tenant_id:
            raise ValidationError(
                {"entity": "The engagement's tenant must be the entity's owning organisation."}
            )

    @property
    def is_live(self) -> bool:
        """Active *and* inside its date window.

        Both halves matter: an engagement that ran out yesterday is still
        ``ACTIVE`` until something ends it, and access must stop on the date the
        client agreed to, not whenever a scheduled job next runs.
        """
        if self.status != self.Status.ACTIVE:
            return False
        today = timezone.localdate()
        if self.starts_on and today < self.starts_on:
            return False
        return not (self.ends_on and today > self.ends_on)

    def end(self, *, reason: str = "", by: Any = None) -> None:
        """End the engagement now.

        ``ends_on`` is pulled back to today as well as setting the status, so the
        date window and the status agree — otherwise a future-dated ``ends_on``
        would leave ``is_live`` disagreeing with ``status`` for anything that
        checks only one of them.
        """
        self.status = self.Status.ENDED
        self.ended_at = timezone.now()
        self.ended_reason = reason[:250]
        self.ended_by = by
        if self.ends_on is None or self.ends_on > timezone.localdate():
            self.ends_on = timezone.localdate()
        self.save(
            update_fields=[
                "status",
                "ended_at",
                "ended_reason",
                "ended_by",
                "ends_on",
                "updated_at",
            ]
        )


class EngagementInvitation(TimeStampedModel):
    """An invitation that may precede either party having an account.

    Both directions are first-class: a business invites its CA, or a CA invites
    its client. Either invitee may not exist in STACOS yet, so the invitation
    holds an email and phone rather than a user, and binds to whoever completes
    dual-channel verification on those identifiers.

    Not tenant-scoped: at creation time the receiving tenant frequently does not
    exist, and the sending tenant must be able to see its own outbound
    invitations regardless.
    """

    class Status(models.TextChoices):
        SENT = "SENT", _("Sent")
        ACCEPTED = "ACCEPTED", _("Accepted")
        DECLINED = "DECLINED", _("Declined")
        EXPIRED = "EXPIRED", _("Expired")
        CANCELLED = "CANCELLED", _("Cancelled")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    from_tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="invitations_sent"
    )
    #: Set when the sender already knows the counterparty's account.
    to_tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="invitations_received",
    )
    #: Set when a business invites a firm for a specific entity. Null when a firm
    #: invites a prospective client who has no entities yet.
    entity = models.ForeignKey(
        Entity, on_delete=models.CASCADE, null=True, blank=True, related_name="invitations"
    )

    to_email = models.EmailField()
    to_phone_e164 = models.CharField(max_length=20, blank=True)
    to_name = models.CharField(max_length=200, blank=True)

    initiated_by_side = models.CharField(max_length=16, choices=Engagement.Side.choices)
    categories = ArrayField(
        models.CharField(max_length=32, choices=ComplianceCategory.choices),
        default=list,
        blank=True,
    )
    permissions = models.JSONField(default=list, blank=True)
    message = models.TextField(blank=True)

    #: Only the hash is stored; the raw token exists solely in the emailed link.
    token_hash = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SENT)
    expires_at = models.DateTimeField()
    responded_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="engagement_invitations",
    )
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="engagement_invitations_accepted",
    )
    engagement = models.ForeignKey(
        Engagement, on_delete=models.SET_NULL, null=True, blank=True, related_name="invitations"
    )

    objects = models.Manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["to_email", "status"], name="invite_email_status_idx"),
            models.Index(fields=["from_tenant", "status"], name="invite_from_status_idx"),
        ]

    def __str__(self) -> str:
        return f"Invitation to {self.to_email} from {self.from_tenant}"

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.SENT and timezone.now() < self.expires_at
