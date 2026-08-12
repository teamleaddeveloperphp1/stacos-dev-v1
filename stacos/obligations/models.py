"""
The entity obligation register: what this client actually owes, and where each
one has got to.

The catalog says what the law requires. This says what *you* have to file, by
when, and who is holding it up. Everything downstream in the product — the
information requests, the vault, return preparation, the practice work board —
reads from this table, which is why it lands before any of them.

Three design points carry more weight than the rest:

**The natural key excludes the definition version.** An instance is identified by
``(entity, definition_code, scope_ref, period_key, occurrence)``. Including the
version would make every catalog bump duplicate every row; the version is an
attribute the planner updates in place.

**``scope_ref`` is not decoration.** GSTR-3B is filed per GSTIN. An entity with
registrations in six states files six of them every month, and each has its own
state, its own deadline pressure and its own evidence. Modelling one row per
entity per period would lose five filings a month.

**Nothing with history is ever destroyed.** An instance carrying evidence or
events is superseded and retained, never deleted, because the nightly rebuild
must not be able to erase a client's audit trail.
"""

from __future__ import annotations

from typing import Any, ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.models import SoftDeleteModel, TenantScopedModel
from stacos.engine.lifecycle import OPEN_STATES, State, state_label
from stacos.engine.types import InstanceScope

__all__ = [
    "EntityEvent",
    "MaterialisationRun",
    "ObligationEvent",
    "ObligationInstance",
    "ObligationSuppression",
]


class EntityEvent(TenantScopedModel):
    """A recorded date that a due rule anchors on.

    Lives with the obligations rather than with the entity because that is the
    only thing it is for: an AGM date, a licence issue date or the date of the
    last board meeting has no meaning in this product except as the anchor a
    filing deadline is computed from.

    Its absence is a first-class outcome. A company that has not recorded its AGM
    date gets AOC-4 materialised with a null due date and a prompt — "tell us your
    AGM date to schedule this" — rather than a guessed date, because a calendar
    that says what it does not know is more useful than one that is confidently
    wrong.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="schedule_events"
    )
    #: Matches ``due_rule.event_key`` in the catalog — AGM_DATE, BOARD_MEETING.
    key = models.CharField(max_length=64, db_index=True)
    occurred_on = models.DateField()
    #: Which registration or premises this event belongs to, when it is not
    #: entity-wide — a factory licence issued for one plant.
    scope_ref = models.CharField(max_length=100, blank=True)
    note = models.CharField(max_length=250, blank=True)

    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["entity", "key", "-occurred_on"]
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "key", "scope_ref", "occurred_on"],
                name="entityevent_identity_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "key", "-occurred_on"], name="entityevent_lookup_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.key} on {self.occurred_on}"

#: Rendered into the partial index and into the SQL overdue annotation. Sorted so
#: the generated migration is stable across machines.
_OPEN_STATE_VALUES: list[str] = sorted(str(s) for s in OPEN_STATES)


class ObligationInstance(TenantScopedModel, SoftDeleteModel):
    """One filing, for one entity, for one period, at one registration or site."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="obligations"
    )

    # -- Identity -----------------------------------------------------------
    #: Denormalised from the catalog rather than a foreign key. Publishing a
    #: government extension is then a single indexed UPDATE on
    #: (definition_code, period_key) across every tenant, which is the operation
    #: that has to be fast when a notification lands the evening before a deadline.
    definition_code = models.SlugField(max_length=64, db_index=True)
    #: Updated in place by the planner when the catalog moves on. Deliberately
    #: absent from the uniqueness constraint.
    definition_version = models.PositiveIntegerField(default=1)

    scope_kind = models.CharField(
        max_length=16, choices=[(s, s) for s in InstanceScope], default=InstanceScope.ENTITY
    )
    #: The registration or premises this filing is *for*. Empty for entity-level
    #: obligations. Stored as text rather than a polymorphic FK because the
    #: planner works in opaque references and this column is on the hot path of
    #: every list query.
    scope_ref = models.CharField(max_length=100, blank=True)
    #: Human-readable form of the above — "GSTIN 24AABCS1429B1ZQ (Gujarat)". Kept
    #: denormalised so a list of two hundred rows costs no extra queries, and so
    #: the row still reads correctly after a registration is surrendered.
    scope_label = models.CharField(max_length=150, blank=True)
    scope_jurisdiction = models.CharField(max_length=12, blank=True, db_index=True)

    period_key = models.CharField(max_length=32, db_index=True)
    period_label = models.CharField(max_length=60, blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    #: Distinguishes repeats within one period — four board meetings in a quarter,
    #: two instalments in a month.
    occurrence = models.PositiveSmallIntegerField(default=0)

    # -- Display ------------------------------------------------------------
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=32, db_index=True)

    # -- Dates --------------------------------------------------------------
    #: A **date**, not a datetime. "Due 20 September" is a legal date in a
    #: jurisdiction's timezone; storing an instant guarantees off-by-one-day bugs
    #: at month boundaries in a product whose entire value is not missing
    #: deadlines. Null means the date could not be resolved yet — see
    #: ``needs_input``.
    due_date = models.DateField(null=True, blank=True, db_index=True)
    #: What the statute said before a notification moved it. Shown struck through
    #: beside the effective date, with the notification behind it.
    original_due_date = models.DateField(null=True, blank=True)
    applied_extension_reference = models.CharField(max_length=120, blank=True)
    #: What was relieved without the date moving: {"late_fee": true}.
    relief = models.JSONField(default=dict, blank=True)

    # -- Lifecycle ----------------------------------------------------------
    state = models.CharField(
        max_length=28, choices=[(s, state_label(s)) for s in State], default=State.NOT_STARTED
    )
    #: Set when the state last changed, for "untouched for three weeks" reporting.
    state_changed_at = models.DateTimeField(default=timezone.now)

    filed_on = models.DateField(null=True, blank=True)
    filing_reference = models.CharField(
        max_length=120, blank=True, help_text=_("Acknowledgement or challan number.")
    )
    closed_at = models.DateTimeField(null=True, blank=True)

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_obligations",
    )
    owner_role = models.CharField(max_length=60, blank=True)

    # -- Why it is here -----------------------------------------------------
    #: False when the applicability rule evaluated to UNKNOWN because a fact is
    #: missing. The obligation is materialised anyway and shown as "we think this
    #: may apply — confirm". Silently dropping it is the failure mode that gets a
    #: client penalised.
    confirmed = models.BooleanField(default=True)
    #: The pruned, minimal reason the rule fired. Shown verbatim on the detail
    #: page, so "why is this on my calendar" always has an answer.
    reasons = ArrayField(models.CharField(max_length=250), default=list, blank=True)
    #: Names the fact or event blocking date resolution — "AGM_DATE". Surfaced as
    #: "tell us your AGM date to schedule this". A calendar that says what it does
    #: not know beats one that guesses.
    needs_input = models.CharField(max_length=64, blank=True)
    #: Set when a retroactive fact restatement produced this row. Routed to a
    #: human rather than auto-applied: a back-dated obligation appearing silently
    #: in a closed period is not something to spring on a client.
    retroactive = models.BooleanField(default=False)

    #: Set when the planner would have removed this but it carried history. The
    #: row stays visible in the audit trail and disappears from the working
    #: calendar.
    superseded_at = models.DateTimeField(null=True, blank=True)
    supersede_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["due_date", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "entity",
                    "definition_code",
                    "scope_ref",
                    "period_key",
                    "occurrence",
                ],
                name="obligation_identity_uniq",
            ),
            # A filed instance without a date it was filed on cannot be checked
            # for lateness, which is half the point of the register.
            models.CheckConstraint(
                condition=~Q(state__in=["FILED", "CLOSED"]) | Q(filed_on__isnull=False),
                name="obligation_filed_has_date",
            ),
        ]
        indexes = [
            # The overdue query, which runs on every dashboard load. Partial,
            # because finished obligations are the majority of the table within a
            # year and none of them can be overdue.
            models.Index(
                fields=["tenant", "due_date"],
                name="obligation_open_due_idx",
                condition=Q(state__in=_OPEN_STATE_VALUES) & Q(archived_at__isnull=True),
            ),
            # The calendar view: one entity, a date window.
            models.Index(fields=["entity", "due_date"], name="obligation_entity_due_idx"),
            # Publishing a government extension.
            models.Index(
                fields=["definition_code", "period_key"], name="obligation_code_period_idx"
            ),
            # The work board: what is assigned to me, soonest first.
            models.Index(fields=["assigned_to", "due_date"], name="obligation_assignee_due_idx"),
            models.Index(fields=["tenant", "category"], name="obligation_tenant_cat_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.title} — {self.period_label or self.period_key}"

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_superseded(self) -> bool:
        return self.superseded_at is not None

    def supersede(self, *, reason: str) -> None:
        """Retain an instance the planner no longer wants.

        Used when the row carries evidence or events. It stops appearing in the
        working calendar but remains in the record, because "this used to be
        required and then the client's profile changed" is exactly the kind of
        thing a regulator asks about.
        """
        self.superseded_at = timezone.now()
        self.supersede_reason = reason
        self.state = State.NOT_APPLICABLE
        self.save(
            update_fields=["superseded_at", "supersede_reason", "state", "updated_at"]
        )


class ObligationEvent(TenantScopedModel):
    """Append-only history of one obligation.

    Distinct from :class:`~stacos.core.models.AuditLog`, which is the tenant-wide
    security record. This is the *timeline the user reads* on the detail page:
    who moved it, when, and what they said about it. Both are written on every
    transition — the audit log because a regulator asks, this because a colleague
    picking the work up tomorrow needs the story.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"
    #: Written by the system on behalf of whoever acted, including from a
    #: read-only engagement where the actor could not otherwise write.
    ENFORCE_WRITE_SCOPE: ClassVar[bool] = False

    class Kind(models.TextChoices):
        TRANSITION = "TRANSITION", _("State changed")
        MATERIALISED = "MATERIALISED", _("Added to the calendar")
        DATE_CHANGED = "DATE_CHANGED", _("Due date changed")
        EXTENSION_APPLIED = "EXTENSION_APPLIED", _("Government extension applied")
        SUPERSEDED = "SUPERSEDED", _("No longer applicable")
        ASSIGNED = "ASSIGNED", _("Assigned")
        NOTE = "NOTE", _("Note added")
        EVIDENCE_ADDED = "EVIDENCE_ADDED", _("Evidence attached")

    obligation = models.ForeignKey(
        ObligationInstance, on_delete=models.CASCADE, related_name="events"
    )
    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="obligation_events"
    )

    kind = models.CharField(max_length=20, choices=Kind.choices)
    from_state = models.CharField(max_length=28, blank=True)
    to_state = models.CharField(max_length=28, blank=True)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    #: Denormalised so the timeline stays readable after a user is removed.
    actor_label = models.CharField(max_length=200, blank=True)
    note = models.TextField(blank=True)
    context = models.JSONField(default=dict, blank=True)

    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["obligation", "-occurred_at"], name="oblevent_timeline_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.occurred_at:%Y-%m-%d} {self.kind}"


class ObligationSuppression(TenantScopedModel):
    """A user's decision that an obligation does not apply to them.

    An **input** to the planner, not an output. A nightly job that resurrects
    obligations somebody deliberately dismissed destroys trust faster than any
    bug — the client stops believing the calendar, and a calendar nobody believes
    is worth nothing.

    Scoped by identity rather than by row, so the suppression survives the
    instance being archived and reinstated.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Kind(models.TextChoices):
        NOT_APPLICABLE = "NOT_APPLICABLE", _("Does not apply to us")
        DEFERRED = "DEFERRED", _("Postponed")

    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="obligation_suppressions"
    )
    definition_code = models.SlugField(max_length=64)
    scope_ref = models.CharField(max_length=100, blank=True)
    #: Empty suppresses every period of this definition for this scope — "we are
    #: not a factory, stop asking". A specific key suppresses one occurrence.
    period_key = models.CharField(max_length=32, blank=True)

    kind = models.CharField(
        max_length=20, choices=Kind.choices, default=Kind.NOT_APPLICABLE
    )
    reason = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    #: A deferral that expires. Null means indefinite.
    expires_on = models.DateField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "definition_code", "scope_ref", "period_key"],
                condition=Q(revoked_at__isnull=True),
                name="suppression_identity_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "definition_code"], name="suppression_lookup_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.definition_code} suppressed for {self.entity_id}"

    @property
    def is_live(self) -> bool:
        if self.revoked_at is not None:
            return False
        return not (self.expires_on and self.expires_on < timezone.localdate())


class MaterialisationRun(TenantScopedModel):
    """One application of a materialisation plan.

    Kept because "why did forty-three obligations appear on my calendar last
    Tuesday" is a question that gets asked, and because a run that produced
    supersessions is one a human should look at. The stored fingerprint is what
    makes an apply refuse to proceed against a catalog that moved after the
    preview was generated.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Trigger(models.TextChoices):
        NIGHTLY = "NIGHTLY", _("Nightly horizon roll")
        PROFILE_CHANGE = "PROFILE_CHANGE", _("Profile changed")
        CATALOG_PUBLISH = "CATALOG_PUBLISH", _("Catalog published")
        MANUAL = "MANUAL", _("Requested by a user")
        ONBOARDING = "ONBOARDING", _("Entity onboarded")

    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="materialisation_runs"
    )
    trigger = models.CharField(max_length=20, choices=Trigger.choices)
    catalog_fingerprint = models.CharField(max_length=32, blank=True)

    horizon_start = models.DateField()
    horizon_end = models.DateField()
    as_of = models.DateField()

    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    superseded_count = models.PositiveIntegerField(default=0)
    archived_count = models.PositiveIntegerField(default=0)
    revived_count = models.PositiveIntegerField(default=0)
    unchanged_count = models.PositiveIntegerField(default=0)

    diagnostics = models.JSONField(default=list, blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    duration_ms = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["entity", "-created_at"], name="matrun_entity_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.trigger} run for {self.entity_id}: {self.summary()}"

    def summary(self) -> str:
        parts: list[str] = []
        for count, word in (
            (self.created_count, "added"),
            (self.updated_count, "changed"),
            (self.superseded_count, "no longer applicable"),
            (self.archived_count, "removed"),
            (self.revived_count, "restored"),
        ):
            if count:
                parts.append(f"{count} {word}")
        return ", ".join(parts) if parts else "no changes"

    @property
    def needs_review(self) -> bool:
        """Supersessions touched instances someone had worked on."""
        return self.superseded_count > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger,
            "created": self.created_count,
            "updated": self.updated_count,
            "superseded": self.superseded_count,
            "archived": self.archived_count,
            "revived": self.revived_count,
        }
