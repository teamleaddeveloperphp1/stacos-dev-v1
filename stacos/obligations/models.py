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

import pathlib
from datetime import date, timedelta
from typing import Any, ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.ids import uuid7
from stacos.core.models import SoftDeleteModel, TenantScopedModel
from stacos.engine.lifecycle import OPEN_STATES, State, state_label
from stacos.engine.types import InstanceScope

__all__ = [
    "EntityEvent",
    "MaterialisationRun",
    "ObligationEvent",
    "ObligationInclusion",
    "ObligationInstance",
    "ObligationSuppression",
]


def acknowledgement_upload_to(instance: ObligationInstance, filename: str) -> str:
    """Where an uploaded acknowledgement lands on disk.

    Module scope, forever: a migration serialises the reference by path, the
    same reason ``stacos.core.ids.uuid7`` may never move.

    The stored name is a fresh UUID rather than the one the browser sent. Two
    reasons, and neither is tidiness — an uploaded name is attacker-controlled
    text that has no business becoming a filesystem path, and a guessable path
    is the whole attack against media storage. What the user called the file is
    kept in ``acknowledgement_name`` for display, where it is escaped like any
    other string.

    ``MEDIA_ROOT`` is only ever served directly by Django in development (see
    ``config/urls.py``); in production these bytes leave exclusively through
    ``compliance:acknowledgement``, which re-checks the caller's scope.
    """
    suffix = pathlib.Path(filename).suffix.lower()[:12]
    return f"acknowledgements/{instance.tenant_id}/{instance.pk}/{uuid7().hex}{suffix}"


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

    #: What the event is *about*: a DIN, a PAN, a charge id. Empty for events
    #: about the company itself. This is what makes two directors appointed on
    #: one day two events rather than a unique-constraint violation — which is
    #: exactly what the original constraint made it.
    subject_ref = models.CharField(max_length=100, blank=True)
    #: "Ramesh Mehta". Denormalised so a calendar row reads "DIR-12 — Ramesh
    #: Mehta" without a join, and still reads correctly after the person leaves.
    subject_label = models.CharField(max_length=200, blank=True)
    #: Structured payload that ``trigger.when`` evaluates against — the role an
    #: officer was appointed to, whether an allottee was non-resident. Validated
    #: against the event type's declared attributes for the same reason
    #: ``EntityProfile.facts`` is: an unvalidated JSONB blob becomes ``din``,
    #: ``DIN`` and ``director_din`` inside a year.
    attributes = models.JSONField(default=dict, blank=True)

    #: Recorded in error, or corrected. **Soft**, because the primary key is the
    #: engine's stable occurrence reference: destroying the row destroys the
    #: identity of every obligation derived from it, and undoing the mistake
    #: would then create a duplicate beside the one somebody had started work on.
    superseded_at = models.DateTimeField(null=True, blank=True)
    supersede_reason = models.CharField(max_length=250, blank=True)

    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["entity", "key", "-occurred_on"]
        constraints = [
            # `subject_ref` joins the key so that two appointments on one day are
            # two events. The partial condition lets a correction supersede and
            # re-record on the same day without colliding with what it replaced.
            models.UniqueConstraint(
                fields=["entity", "key", "scope_ref", "occurred_on", "subject_ref"],
                condition=Q(superseded_at__isnull=True),
                name="entityevent_identity_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "key", "-occurred_on"], name="entityevent_lookup_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.key} on {self.occurred_on}"

    @property
    def ref(self) -> str:
        """The stable occurrence reference the engine numbers instances from.

        The primary key itself, not a second identifier: a UUIDv7 generated in
        Python before insert and never reused. A parallel column that has to be
        kept in step with the primary key is a bug waiting to be written.
        """
        return str(self.pk)

    def display_label(self) -> str:
        """What the obligation row shows — "Ramesh Mehta", or the date."""
        return self.subject_label or self.occurred_on.isoformat()


#: Rendered into the partial index and into the SQL overdue annotation. Sorted so
#: the generated migration is stable across machines.
_OPEN_STATE_VALUES: list[str] = sorted(str(s) for s in OPEN_STATES)

#: The happy path, collapsed to four stages for the detail page's progress
#: indicator. DEFERRED, DISPUTED and NOT_APPLICABLE are deliberately absent —
#: see ``ObligationInstance.progress_steps``.
#:
#: ``submitted`` and ``completed`` are separate stages, not one — this used to
#: read "Filed" for both ``FILED`` and ``CLOSED``, which quietly answered a
#: question nobody asked ("has the paperwork been filed") while hiding the one
#: a user actually has: is the acknowledgement filed away with its evidence,
#: or does it still need attaching? Splitting them is what lets the stepper
#: and ``ObligationInstance.state``'s own label agree, instead of a fourth
#: circle that stays lit for two different true things.
_PROGRESS_STAGES: tuple[tuple[str, Any, tuple[State, ...]], ...] = (
    ("not_started", _("Not started"), (State.NOT_STARTED,)),
    (
        "in_progress",
        _("In progress"),
        (
            State.INFO_REQUESTED,
            State.IN_PREPARATION,
            State.PENDING_REVIEW,
            State.PENDING_CLIENT_APPROVAL,
            State.READY_TO_FILE,
        ),
    ),
    ("submitted", _("Submitted"), (State.FILED,)),
    ("completed", _("Completed"), (State.CLOSED,)),
)


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
    #: 120 rather than 60: an event-driven row reads "Director appointed —
    #: Ramesh Chandrashekhar Mehta", and truncating a person's name onto a
    #: statutory filing — or raising DataError on the nightly job — are both
    #: unacceptable ways to find out the column was too narrow.
    period_label = models.CharField(max_length=120, blank=True)
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
    #: The acknowledgement itself, as the portal handed it back. Optional: a
    #: filing recorded from memory the week after, with only its number, is
    #: still worth having, and refusing it would push people to record nothing.
    #:
    #: A ``FileField`` on this model rather than a row in the vault, because the
    #: vault does not exist yet. When it does, this becomes a foreign key and
    #: these bytes are migrated into it — the download already goes through a
    #: permission-checked view rather than ``MEDIA_URL``, so that move changes
    #: where the bytes live and nothing about who may read them.
    acknowledgement = models.FileField(
        upload_to=acknowledgement_upload_to, blank=True, max_length=300
    )
    #: What the uploader called it. Kept because the stored name is a UUID, and
    #: "Ack.pdf" is what a person recognises in a download prompt a year later.
    acknowledgement_name = models.CharField(max_length=255, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    # -- Not done yet, and why ----------------------------------------------
    #: The answer to "why is this still pending", asked on the detail page
    #: whenever someone says the filing is not done. Deliberately *not* a
    #: suppression: the obligation stays on the calendar, keeps its due date and
    #: keeps going overdue. Recording a reason is not permission to stop
    #: chasing it.
    pending_reason = models.CharField(max_length=300, blank=True)
    #: When the person answering expects to have it done. A working estimate,
    #: never a legal date — the statutory one is ``due_date`` and this never
    #: displaces it.
    expected_completion_date = models.DateField(null=True, blank=True)
    #: When the pending answer above was last given, so a stale "next week"
    #: from two months ago reads as stale rather than as current.
    pending_reported_at = models.DateTimeField(null=True, blank=True)

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
    #:
    #: True for an obligation somebody opted into by hand, through a pack or one
    #: at a time. "Unconfirmed" means nobody has decided yet, and a person choosing
    #: to add something has decided — more firmly than any rule could. Where the
    #: answer came from is not lost by folding the two together: ``reasons`` says
    #: "you added this to your calendar" for exactly these.
    confirmed = models.BooleanField(default=True)
    #: Which facts the rule needed and did not have. The engine computes this on
    #: every evaluation (``Verdict.missing_facts``) and it used to be thrown away
    #: with the verdict, so the register knew an obligation was unconfirmed but
    #: not what would confirm it — and the "Confirm" badge was an inert span
    #: offering an action nothing implemented. Empty for an obligation somebody
    #: opted into by hand, whatever the rule made of it: the person adding it has
    #: already answered the only question those facts would settle.
    missing_facts = ArrayField(models.CharField(max_length=64), default=list, blank=True)
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
    def progress_steps(self) -> list[dict[str, Any]] | None:
        """The four-stage happy path, for the detail page's stepper.

        Returns ``None`` off the happy path (deferred, disputed, not
        applicable): those are exceptions the status chip and its callout
        already explain, and forcing them onto four numbered circles would
        claim a false sense of where the filing stands.
        """
        try:
            current = next(
                index
                for index, (_key, _label, states) in enumerate(_PROGRESS_STAGES)
                if self.state in states
            )
        except StopIteration:
            return None
        return [
            {"key": key, "label": label, "done": index < current, "current": index == current}
            for index, (key, label, _states) in enumerate(_PROGRESS_STAGES)
        ]

    @property
    def progress_done_count(self) -> int | None:
        """How many of the four stages are behind this one, for a "2 of 4" tally.

        ``None`` off the happy path, exactly when :attr:`progress_steps` is —
        there is no stage count worth showing for a deferred, disputed or
        not-applicable filing.
        """
        steps = self.progress_steps
        if steps is None:
            return None
        return sum(1 for step in steps if step["done"])

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
        self.save(update_fields=["superseded_at", "supersede_reason", "state", "updated_at"])


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
        STEP_ASSIGNED = "STEP_ASSIGNED", _("Checklist step assigned")
        STEP_COMPLETED = "STEP_COMPLETED", _("Checklist step completed")
        STEP_REOPENED = "STEP_REOPENED", _("Checklist step reopened")
        STEP_BLOCKED = "STEP_BLOCKED", _("Checklist step blocked")
        STEP_UNBLOCKED = "STEP_UNBLOCKED", _("Checklist step unblocked")

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


class ObligationStep(TenantScopedModel):
    """One named item of work behind a filing — for the definitions that have
    written one down.

    A minority of catalog definitions carry a ``workflow_steps`` template
    (``DefinitionVersion.workflow_steps``); most do not, and an obligation with
    no steps falls back to the generic four-stage lifecycle stepper the detail
    page has always shown. This is opt-in detail, never a second source of
    truth for whether an obligation is done — ``ObligationInstance.state``
    still answers that; a step is a real work item, not itself a lifecycle
    state.

    Steps are created once, lazily, on first read of an obligation whose
    definition has a template — see
    ``stacos.obligations.transitions.ensure_steps``. There is deliberately no
    row for the ~140 definitions that have not had a checklist written for
    them yet.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Role(models.TextChoices):
        """Which of the four maker-checker permissions gates this step.

        Not a permission string itself — a step is catalog data and the
        catalog must never carry a permission code that might be renamed out
        from under it. ``ROLE_PERMISSION`` below is the one place that maps
        one to the other.
        """

        PREPARE = "PREPARE", _("Prepare")
        REVIEW = "REVIEW", _("Review")
        APPROVE = "APPROVE", _("Approve")
        SIGN = "SIGN", _("Sign")

    class State(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        BLOCKED = "BLOCKED", _("Blocked")
        DONE = "DONE", _("Done")

    obligation = models.ForeignKey(
        ObligationInstance, on_delete=models.CASCADE, related_name="steps"
    )
    entity = models.ForeignKey("tenancy.Entity", on_delete=models.CASCADE, related_name="+")

    #: From the catalog template's own ``key`` — stable across re-reads of the
    #: same obligation, which is what makes :func:`ensure_steps` idempotent.
    key = models.SlugField(max_length=64)
    order = models.PositiveSmallIntegerField()
    label = models.CharField(max_length=200)
    role = models.CharField(max_length=8, choices=Role.choices)
    state = models.CharField(max_length=8, choices=State.choices, default=State.PENDING)

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_obligation_steps",
    )
    #: Why the step can't move — required to enter ``State.BLOCKED``, cleared
    #: on completion or on being reopened.
    blocked_reason = models.CharField(max_length=300, blank=True)

    #: From the template's own ``days_before_due`` — how many days before the
    #: obligation's due date this step should be done by. Deliberately not a
    #: stored date: computed from ``obligation.due_date`` via :attr:`target_date`
    #: so a government extension shifts every step's target automatically
    #: instead of leaving them stale.
    days_before_due = models.PositiveSmallIntegerField(null=True, blank=True)
    #: Whether completing this step needs a note saying what evidence backs
    #: it — the "Generate FVU file" or "File with DSC" kind of step, not
    #: every step. Enforced in ``transitions.complete_step``.
    requires_evidence = models.BooleanField(default=False)
    evidence_note = models.CharField(max_length=300, blank=True)

    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    #: Not a field — annotated onto the instances the detail view hands to the
    #: template, marking the one step a reader should pick up next. Declared
    #: here rather than set out of thin air so the attribute has a documented
    #: default for every other caller: a step nobody annotated is simply not
    #: next, which is what a checklist rendered outside the detail page wants.
    is_next: bool = False

    class Meta:
        ordering = ["obligation", "order"]
        constraints = [
            models.UniqueConstraint(
                fields=["obligation", "key"], name="obligationstep_identity_uniq"
            )
        ]
        indexes = [
            models.Index(fields=["assigned_to", "state"], name="obligationstep_assignee_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.get_state_display()})"

    @property
    def target_date(self) -> date | None:
        """When this step should be done by, derived from the obligation's
        own due date — never stored, so it never goes stale."""
        if self.days_before_due is None or self.obligation.due_date is None:
            return None
        return self.obligation.due_date - timedelta(days=self.days_before_due)


#: Which existing lifecycle permission gates completing or blocking a step of
#: each role. Deliberately reuses the four permissions ``apply_transition``
#: already checks for the obligation as a whole, rather than a parallel set of
#: per-step permissions nobody would remember to grant.
ROLE_PERMISSION: dict[str, str] = {
    ObligationStep.Role.PREPARE: "compliance.obligation.prepare",
    ObligationStep.Role.REVIEW: "compliance.obligation.review",
    ObligationStep.Role.APPROVE: "compliance.obligation.approve",
    ObligationStep.Role.SIGN: "compliance.obligation.file",
}

#: The checklist every obligation gets when its own catalog definition has not
#: written a more specific one (``DefinitionVersion.workflow_steps``). Four
#: items, one per role — the same four stages the lifecycle stepper has always
#: shown, just as real per-item work instead of a status-only bar. A generic
#: product default, not a jurisdiction fact, so it lives here in code rather
#: than being duplicated into ~140 catalog YAML files; a definition overrides
#: it by writing its own ``steps:`` list, the way Form 24Q already does.
#:
#: ``default_owner_role`` is empty for all four — a generic step has no
#: natural department to default to; ``requires_evidence`` is false for the
#: same reason. Only a definition that opts in (Form 24Q) asks for either.
DEFAULT_WORKFLOW_STEPS: tuple[dict[str, Any], ...] = (
    {
        "key": "prepare",
        "label": "Prepare the filing",
        "role": ObligationStep.Role.PREPARE,
        "default_owner_role": "",
        "days_before_due": 10,
        "requires_evidence": False,
    },
    {
        "key": "review",
        "label": "Review before approval",
        "role": ObligationStep.Role.REVIEW,
        "default_owner_role": "",
        "days_before_due": 5,
        "requires_evidence": False,
    },
    {
        "key": "approve",
        "label": "Approve for filing",
        "role": ObligationStep.Role.APPROVE,
        "default_owner_role": "",
        "days_before_due": 2,
        "requires_evidence": False,
    },
    {
        "key": "file",
        "label": "File and record the acknowledgement",
        "role": ObligationStep.Role.SIGN,
        "default_owner_role": "",
        "days_before_due": 0,
        "requires_evidence": False,
    },
)


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
    #: Which repeat within the period. Zero for everything periodic, and the
    #: occurrence number for an event-driven instance.
    #:
    #: Without this, dismissing one of two DIR-12s triggered on the same day
    #: would build an ``Identity`` with ``occurrence=0`` and match whichever of
    #: them happened to hash to zero — probably neither. The user dismisses a
    #: row, the nightly job brings it back, and the calendar stops being
    #: believed. That is the failure this whole model exists to prevent.
    occurrence = models.PositiveSmallIntegerField(default=0)

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.NOT_APPLICABLE)
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
                fields=["entity", "definition_code", "scope_ref", "period_key", "occurrence"],
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


class ObligationInclusion(TenantScopedModel):
    """A user's decision that an obligation *does* apply to them after all.

    The exact mirror of :class:`ObligationSuppression`, and the two are needed
    for the same reason: the engine is a very good default and a poor final
    authority. A client who knows they file something the rules cannot yet
    infer — an unusual sectoral return, an obligation that follows from a fact
    the profile does not model — needs to be able to say so, and needs it to
    survive the nightly rebuild.

    Reaching the planner as ``opted_in`` rather than as a written fact is a
    deliberate choice. Subscribing to the "DPIIT startup" pack is not the same
    claim as ``is_startup_dpiit = true``; writing the fact would make the
    obligation's *explanation* wrong — "applicable because you are a
    DPIIT-recognised startup" when the truth is "because you asked for it". The
    reasons list is what the product is actually selling, and corrupting it to
    save a parameter is a bad trade.
    """

    class Source(models.TextChoices):
        USER = "USER", _("Chosen one at a time")
        PACK = "PACK", _("Came with a compliance pack")

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    entity = models.ForeignKey(
        "tenancy.Entity", on_delete=models.CASCADE, related_name="obligation_inclusions"
    )
    definition_code = models.SlugField(max_length=64)
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.USER)
    #: Which pack put it here, when it came from one. Removing a pack revokes its
    #: rows, and needs no separate subscription model to do it.
    pack_code = models.SlugField(max_length=64, blank=True)
    reason = models.TextField(blank=True)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "definition_code"],
                condition=Q(revoked_at__isnull=True),
                name="inclusion_identity_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "definition_code"], name="inclusion_lookup_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.definition_code} added for {self.entity_id}"

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None


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
        #: An event was recorded, corrected or withdrawn. Distinct from MANUAL so
        #: that "why did a DIR-12 appear on Tuesday" is answerable from the run log.
        EVENT_RECORDED = "EVENT_RECORDED", _("Entity event recorded")

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


class CalendarFeedToken(models.Model):
    """The secret behind one user's read-only ``.ics`` calendar subscription.

    Deliberately not a :class:`TenantScopedModel`: an external calendar app
    polls the feed URL with no Django session at all, so there is no request
    scope to filter this row by. It is bound to a *user*, not a tenant —
    ``stacos.obligations.feed`` re-resolves that user's own membership and
    access scope on every fetch, the same way a real request would, so a later
    change of role or engagement is honoured automatically rather than baked
    into the link the day it was generated.

    Only the secret's hash is stored, the same shape as
    :class:`stacos.accounts.models.TrustedDevice`: a database disclosure must
    not yield a working subscription link. The raw secret is handed to the
    user once, at creation, and never persisted — losing the link means
    generating a new one, not recovering the old.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="calendar_feed_tokens"
    )
    secret_hash = models.CharField(max_length=64, db_index=True)
    label = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"calendar feed token for {self.user_id}"

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None
