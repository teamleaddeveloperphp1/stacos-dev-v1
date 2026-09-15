"""
The global compliance catalog: what the law requires, of whom, and by when.

Platform-owned reference data, deliberately **not** tenant-scoped. Every tenant
reads the same catalog; what differs between them is which definitions their
profile makes applicable, and that is computed rather than stored here.

Two independent time axes run through this app, and conflating them is the
mistake that makes a typo correction look like a change in the law:

``effective_from`` / ``effective_to``
    **Statutory validity.** When this rule governed filings. GSTR-3B's due date
    moved from the 20th to a staggered 20th/22nd/24th in January 2021 — that is a
    new version with a new effective window, and the old one stays exactly as it
    was because it is still the correct rule for the periods it covered.

``status`` / ``published_at``
    **Editorial lifecycle.** Whether *we* have reviewed and released it. A draft
    correcting last year's typo has an old effective window and a new publication
    date, and nobody should have to reason about which is which.

Source of truth is YAML in git, loaded by ``manage.py loadcatalog``. The database
is a cache of what the repository says. That is why versions are append-only and
immutable once published: rebuilding any historical catalog state has to be
deterministic, and rolling back has to be one command rather than an archaeology
exercise.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import ArrayField, DateRangeField
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Func, Q, Value
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.ids import uuid7
from stacos.core.models import TimeStampedModel
from stacos.engine.types import InstanceScope, Periodicity, ShiftRule
from stacos.jurisdictions.events import EVENT_TYPES

__all__ = [
    "ComplianceDefinition",
    "DefinitionVersion",
    "GovernmentExtension",
    "PublicationStatus",
]


class PublicationStatus(models.TextChoices):
    """Editorial lifecycle. Nothing to do with statutory validity."""

    DRAFT = "DRAFT", _("Draft")
    #: In force. Exactly one published version may claim any statutory instant.
    PUBLISHED = "PUBLISHED", _("Published")
    #: Withdrawn without a successor — a definition we should never have shipped.
    #: Distinct from a version whose effective window simply ended.
    RETIRED = "RETIRED", _("Retired")


class DateRange(Func):
    """``daterange(lower, upper, '[)')`` for the exclusion constraint.

    Half-open on purpose: a version effective to 31 March and its successor
    effective from 1 April must not be read as overlapping on the boundary.
    """

    function = "daterange"
    output_field = DateRangeField()


class ComplianceDefinition(TimeStampedModel):
    """The stable identity of one obligation, across every version of its rules.

    ``code`` is the permanent handle — it appears in YAML filenames, in
    government-extension records, and denormalised onto every materialised
    instance so that publishing an extension is one indexed UPDATE rather than a
    join. It is never renamed once shipped.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    code = models.SlugField(
        max_length=64,
        unique=True,
        help_text=_("Permanent handle, e.g. IN-GST-GSTR3B-MONTHLY. Never renamed once shipped."),
    )
    country = models.CharField(max_length=2, db_index=True)
    #: Which regulator. Referenced rather than named, so a portal URL change is
    #: one row rather than a hundred.
    authority = models.ForeignKey(
        "jurisdictions.Authority",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="definitions",
    )

    #: Grouping for navigation and for engagement scoping. Values come from
    #: ``tenancy.ComplianceCategory``; not a FK because the catalog must load
    #: without the tenancy app's migrations having run.
    category = models.CharField(max_length=32, db_index=True)
    #: Finer grouping inside a category — "GST", "TDS", "ROC". Display only.
    family = models.CharField(max_length=40, blank=True, db_index=True)

    class TriggerKind(models.TextChoices):
        """Why this obligation appears at all.

        Not derivable from ``periodicity`` alone, which is why it is stored.
        ``IN-MCA-STATUTORY-REGISTERS`` is ANNUAL and PERIOD_END-anchored and is
        nonetheless a register you keep, not a return you file; a user filtering
        for "things I have to submit" does not want it.
        """

        STATUTORY_PERIODIC = "STATUTORY_PERIODIC", _("Recurring statutory filing")
        EVENT_DRIVEN = "EVENT_DRIVEN", _("Triggered by something happening")
        RENEWAL = "RENEWAL", _("Licence or registration renewal")
        GOVERNANCE = "GOVERNANCE", _("Meeting, register or resolution")
        INTERNAL = "INTERNAL", _("Internal control, no external filing")

    trigger_kind = models.CharField(
        max_length=20,
        choices=TriggerKind.choices,
        default=TriggerKind.STATUTORY_PERIODIC,
        db_index=True,
    )

    #: Free-form navigation labels — "roc", "annual-filing", "director". Validated
    #: against a controlled vocabulary at load time, because free text rots into
    #: three spellings of one word inside a year; that is the whole reason the
    #: fact registry exists. Unversioned, like ``category`` and ``family``: a tag
    #: is how *we* file the thing, and re-tagging must not read as the law
    #: changing or force a republish of every version.
    tags = ArrayField(models.SlugField(max_length=40), default=list, blank=True)

    #: Which kinds of business this is *about* — "nbfc", "listed", "food". What
    #: finally makes the SECTORAL category usable and gives onboarding a "what
    #: line of business are you in" question worth asking.
    sector_tags = ArrayField(models.SlugField(max_length=40), default=list, blank=True)

    #: False retires the whole definition regardless of its versions. Used when a
    #: statute is repealed outright.
    is_active = models.BooleanField(default=True)

    objects = models.Manager()

    class Meta:
        ordering = ["country", "family", "code"]
        indexes = [
            models.Index(fields=["country", "category"], name="definition_country_cat_idx"),
            models.Index(fields=["country", "is_active"], name="definition_country_active_idx"),
            GinIndex(fields=["tags"], name="definition_tags_gin"),
            GinIndex(fields=["sector_tags"], name="definition_sector_gin"),
        ]

    def __str__(self) -> str:
        return self.code

    def version_for(self, day: date) -> DefinitionVersion | None:
        """The published version governing ``day``, or ``None``."""
        return (
            self.versions.filter(status=PublicationStatus.PUBLISHED, effective_from__lte=day)
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=day))
            .order_by("-effective_from")
            .first()
        )


class DefinitionVersion(TimeStampedModel):
    """One published statement of what a definition requires.

    **Append-only.** A published version is immutable: correcting it means
    publishing a successor. That is what makes "rebuild the catalog as it stood
    on 12 March" a deterministic operation rather than a guess, and it is why
    ``save()`` refuses to alter the rule payload of a published row.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    definition = models.ForeignKey(
        ComplianceDefinition, on_delete=models.CASCADE, related_name="versions"
    )
    version = models.PositiveIntegerField()
    status = models.CharField(
        max_length=12, choices=PublicationStatus.choices, default=PublicationStatus.DRAFT
    )

    # -- What it is ---------------------------------------------------------
    title = models.CharField(max_length=200)
    #: One or two sentences a business owner understands. Legally exposed — see
    #: the review fields below.
    plain_language_summary = models.TextField(blank=True)
    statutory_reference = models.CharField(
        max_length=250,
        blank=True,
        help_text=_("Section and rule, e.g. 'Section 39 CGST Act r/w Rule 61'."),
    )
    filing_portal_url = models.URLField(blank=True)

    # -- When it applies ----------------------------------------------------
    periodicity = models.CharField(max_length=16, choices=[(p, p) for p in Periodicity])
    #: FY-anchored or calendar-anchored periods. TDS quarters are FY-anchored
    #: (Q1 is Apr–Jun); some state professional tax is calendar-quarter. Per
    #: definition because assuming either breaks the other.
    period_anchor = models.CharField(max_length=10, default="FY")

    #: Sub-jurisdiction codes this version is limited to. Empty means nationwide,
    #: which is why professional tax — fifteen state variants — needs it and
    #: GSTR-3B does not.
    jurisdictions = ArrayField(models.CharField(max_length=12), default=list, blank=True)

    # -- Who it applies to --------------------------------------------------
    applicability_rule = models.JSONField(
        default=dict,
        blank=True,
        help_text=_(
            "Declarative rule evaluated by stacos.engine.rules. Empty applies to everyone."
        ),
    )
    #: Every fact the rule reads, extracted at load time. The inverted index that
    #: lets a profile edit re-evaluate only the definitions it could have
    #: affected, rather than the whole catalog.
    facts_used = ArrayField(models.CharField(max_length=64), default=list, blank=True)

    #: Entity types this rule cannot be *refuted* for, knowing only country and
    #: legal form. Derived at load time by ``stacos.engine.probe``, exactly as
    #: ``facts_used`` is derived by ``facts_used()``.
    #:
    #: **Advisory. Navigation and ranking only.** It answers "what might apply to
    #: a private limited company in Maharashtra" before any profile exists, in one
    #: indexed scan. Nothing in the planner reads it, and nothing in the planner
    #: may ever read it: the authority on applicability is ``evaluate()`` against
    #: the real profile, and a wrong value here must never be able to make a
    #: calendar wrong. A test asserts this name appears nowhere in the engine's
    #: materialisation path.
    possible_entity_types = ArrayField(models.CharField(max_length=24), default=list, blank=True)

    #: Fingerprint of the entity-type vocabulary the index above was computed
    #: against. Adding a type without reloading the catalog would otherwise leave
    #: every row missing it, and the new type would appear to have no obligations
    #: at all — the worst silent wrongness available here. A system check warns.
    probe_signature = models.CharField(max_length=16, blank=True)

    # -- What it is filed per -----------------------------------------------
    instance_scope = models.CharField(
        max_length=16,
        choices=[(s, s) for s in InstanceScope],
        default=InstanceScope.ENTITY,
        help_text=_("ENTITY, REGISTRATION or PREMISES. GSTR-3B is per GSTIN, not per company."),
    )
    scope_selector = models.JSONField(default=dict, blank=True)

    # -- What makes an instance exist at all ---------------------------------
    #: ``{event_key, when?}`` for an EVENT_BASED definition; empty otherwise.
    #:
    #: Deliberately separate from ``due_rule.event_key``, because the two answer
    #: opposite questions about the same date. AOC-4 exists for FY 2025-26
    #: whether or not an AGM has been recorded — null due date, and a prompt.
    #: DIR-12 does not exist until a director is actually appointed. Spell both
    #: as ``due.event_key`` and the only thing separating them is ``periodicity``,
    #: so a mis-set periodicity silently flips a definition between "one a year,
    #: prompting forever" and "nothing, ever", and neither raises.
    trigger_rule = models.JSONField(default=dict, blank=True)

    # -- When it is due -----------------------------------------------------
    due_rule = models.JSONField(default=dict)

    # -- What closing it requires -------------------------------------------
    evidence_requirements = models.JSONField(default=list, blank=True)
    #: ``[{key, label, role, default_owner_role?, days_before_due?,
    #: requires_evidence?}, ...]`` — the named checklist a firm actually works
    #: through, in order. ``role`` is one of PREPARE/REVIEW/APPROVE/SIGN, mapped
    #: to the matching ``compliance.obligation.*`` permission rather than
    #: carrying its own. Empty for most definitions, which fall back to the
    #: generic four-stage lifecycle stepper — this is opt-in detail, not a
    #: replacement for the lifecycle itself.
    workflow_steps = models.JSONField(default=list, blank=True)
    default_owner_role = models.CharField(max_length=60, blank=True)
    #: Free-text description of what happens if it is missed. Shown on the detail
    #: page, because "₹200 per day, capped at ₹5,000" is what actually motivates
    #: a client to answer an information request.
    penalty_summary = models.CharField(max_length=250, blank=True)
    #: ``[{kind: "PER_DAY_CAPPED"|"FIXED_RANGE", statutory_reference, ...}]`` —
    #: a structured, computable form of the same fact ``penalty_summary``
    #: describes in prose. Deliberately partial: a rate capped at an amount
    #: STACOS does not track anywhere (e.g. "the TDS amount") is still valid
    #: data, just not one this can total up — see
    #: ``stacos.engine.penalty.compute_penalties``, which renders the rate and
    #: reference alone in that case rather than inventing a total.
    penalty_rules = models.JSONField(default=list, blank=True)

    # -- Statutory validity -------------------------------------------------
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)

    # -- Editorial lifecycle ------------------------------------------------
    published_at = models.DateTimeField(null=True, blank=True)
    #: Who checked this against the statute, and when. A published definition with
    #: a review older than twelve months fails CI: the summary is legally exposed
    #: and stale compliance guidance is worse than none.
    reviewed_by = models.CharField(max_length=120, blank=True)
    reviewed_at = models.DateField(null=True, blank=True)

    class Confidence(models.TextChoices):
        HIGH = "HIGH", _("Verified against the statute")
        MEDIUM = "MEDIUM", _("Believed correct, not formally verified")
        LOW = "LOW", _("Provisional — shown with a caveat")

    confidence = models.CharField(
        max_length=8, choices=Confidence.choices, default=Confidence.MEDIUM
    )

    #: Path of the YAML file this row was loaded from, so a wrong date in the
    #: product leads straight to the file that has to change.
    source_path = models.CharField(max_length=250, blank=True)
    #: Hash of the source document, so the loader can skip unchanged files and
    #: detect a database edited out from under the repository.
    source_checksum = models.CharField(max_length=64, blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["definition", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["definition", "version"], name="defversion_code_version_uniq"
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_to__gte=models.F("effective_from")),
                name="defversion_window_sane",
            ),
            # At most one published version may claim any statutory instant. The
            # alternative — two versions silently overlapping — produces a
            # calendar that depends on row ordering, which is the kind of bug
            # that is found by a client rather than by a test.
            ExclusionConstraint(
                name="defversion_no_overlapping_published",
                expressions=[
                    ("definition", "="),
                    (DateRange("effective_from", "effective_to", Value("[)")), "&&"),
                ],
                condition=Q(status=PublicationStatus.PUBLISHED),
            ),
        ]
        indexes = [
            models.Index(fields=["status", "effective_from"], name="defversion_status_eff_idx"),
            GinIndex(fields=["facts_used"], name="defversion_facts_gin"),
            GinIndex(fields=["possible_entity_types"], name="defversion_possible_gin"),
        ]

    def __str__(self) -> str:
        code = self.definition.code if self.definition_id else "?"
        return f"{code} v{self.version}"

    @property
    def is_published(self) -> bool:
        return self.status == PublicationStatus.PUBLISHED

    @property
    def review_is_stale(self) -> bool:
        """Whether the statutory review is older than a year, or never happened.

        Surfaced in the product as a caveat and enforced in CI as a merge gate.
        The summary is legal guidance a client acts on; there has to be a date
        attached to it.
        """
        if self.reviewed_at is None:
            return True
        return (timezone.localdate() - self.reviewed_at).days > 365

    def clean(self) -> None:
        super().clean()
        errors: dict[str, list[str]] = {}

        try:
            Periodicity(self.periodicity)
        except ValueError:
            errors.setdefault("periodicity", []).append(
                f"{self.periodicity!r} is not a known periodicity."
            )

        try:
            InstanceScope(self.instance_scope)
        except ValueError:
            errors.setdefault("instance_scope", []).append(
                f"{self.instance_scope!r} is not a known instance scope."
            )

        # `shift_if_holiday` is mandatory rather than defaulted, so an author has
        # to decide consciously. Indian statutory tax dates do NOT shift for
        # weekends — the portals accept filings on a Sunday, and relief comes
        # through explicit government extensions. Silently defaulting to
        # NEXT_WORKING_DAY produces dates that are wrong in law.
        if "shift_if_holiday" not in (self.due_rule or {}):
            errors.setdefault("due_rule", []).append(
                "shift_if_holiday must be stated explicitly. Use NONE for statutory "
                "filing dates, and a shifting rule only for physically-dependent "
                "obligations such as counter filings or inspections."
            )
        else:
            try:
                ShiftRule(str(self.due_rule["shift_if_holiday"]))
            except ValueError:
                errors.setdefault("due_rule", []).append(
                    f"{self.due_rule['shift_if_holiday']!r} is not a known shift rule."
                )

        # An event-triggered definition and a periodic one are different shapes,
        # and the failure modes of getting it wrong are silent in both
        # directions: a trigger with a periodic periodicity generates a row per
        # quarter that nothing ever happened for, and EVENT_BASED with no trigger
        # generates nothing at all, forever, with every test still green. The
        # loader checks this too; this is the guard for an admin write.
        trigger = self.trigger_rule or {}
        is_event_based = self.periodicity == Periodicity.EVENT_BASED
        anchor = str((self.due_rule or {}).get("anchor", ""))

        if is_event_based and not trigger:
            errors.setdefault("trigger_rule", []).append(
                "periodicity EVENT_BASED needs a trigger. Without one the definition "
                "loads cleanly and materialises nothing, ever."
            )
        if trigger and not is_event_based:
            errors.setdefault("trigger_rule", []).append(
                "a trigger requires periodicity EVENT_BASED."
            )
        if trigger and str(trigger.get("event_key", "")) not in EVENT_TYPES:
            errors.setdefault("trigger_rule", []).append(
                f"unknown trigger event_key {trigger.get('event_key')!r}."
            )
        if is_event_based and anchor != "TRIGGER_DATE":
            errors.setdefault("due_rule", []).append(
                "an event-triggered definition must use anchor TRIGGER_DATE, which "
                "dates each instance from its own occurrence. EVENT_DATE reads the "
                "latest date for the key, so every filing of the year would be "
                "dated from the most recent event."
            )
        if anchor == "TRIGGER_DATE" and not trigger:
            errors.setdefault("due_rule", []).append(
                "anchor TRIGGER_DATE has no trigger to anchor on."
            )

        if errors:
            raise ValidationError(errors)

    def to_snapshot(self) -> Any:
        """Convert to the frozen value type the engine reads.

        Deferred import so that ``models.py`` does not drag the engine into every
        Django startup path, and typed loosely here because the engine's types are
        the authority on their own shape.
        """
        from stacos.catalog.snapshots import version_to_snapshot

        return version_to_snapshot(self)


class CompliancePack(TimeStampedModel):
    """A curated set of definitions somebody can adopt in one decision.

    The applicability engine answers "what does the law require of you". A pack
    answers a different question — "what does a business like mine usually
    track" — and the two are not the same. A newly incorporated private company
    does not yet hold a GSTIN or a PF code, so the rules correctly infer almost
    nothing; what it actually wants on day one is the ROC annual kit, chosen in
    one click rather than assembled from a list of three hundred.

    Platform-owned reference data like the rest of the catalog, authored as YAML
    in ``catalog/bundles/`` and loaded by ``manage.py loadcatalog``.

    ``definition_codes`` is an array of slugs rather than a many-to-many, for
    three reasons. ``validatecatalog`` must be able to check a pack against parsed
    YAML documents **with no database**, which a join table cannot support and
    which is the property the whole merge gate rests on. ``code`` is already the
    handle used by ``ObligationInstance``, ``ObligationSuppression`` and
    ``GovernmentExtension``, so a fourth spelling would be the odd one out. And a
    join table would make a pack that references a not-yet-loaded definition a
    load-order dependency inside a single command.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    code = models.SlugField(max_length=64, unique=True)
    country = models.CharField(max_length=2, db_index=True)
    name = models.CharField(max_length=120)
    summary = models.TextField(blank=True)
    #: Why somebody would choose this, in their words rather than the statute's.
    #: Rendered on the card, and the thing that makes a pack pickable at all.
    rationale = models.TextField(blank=True)

    definition_codes = ArrayField(models.SlugField(max_length=64), default=list)

    #: The same applicability DSL, deciding whether to *offer* the pack. Only ever
    #: a suggestion: a pack is never applied without somebody pressing a button,
    #: which is exactly what distinguishes it from an applicability rule.
    suggestion_rule = models.JSONField(default=dict, blank=True)
    facts_used = ArrayField(models.CharField(max_length=64), default=list, blank=True)

    tags = ArrayField(models.SlugField(max_length=40), default=list, blank=True)
    jurisdictions = ArrayField(models.CharField(max_length=12), default=list, blank=True)

    is_active = models.BooleanField(default=True)
    source_path = models.CharField(max_length=250, blank=True)
    source_checksum = models.CharField(max_length=64, blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["country", "name"]
        indexes = [
            models.Index(fields=["country", "is_active"], name="pack_country_active_idx"),
            GinIndex(fields=["definition_codes"], name="pack_definitions_gin"),
            GinIndex(fields=["facts_used"], name="pack_facts_gin"),
        ]

    def __str__(self) -> str:
        return self.code

    @property
    def size(self) -> int:
        return len(self.definition_codes)


class GovernmentExtension(TimeStampedModel):
    """A notification that moved a date, waived a penalty, or opened an amnesty.

    First-class from day one rather than retrofitted, because Indian due dates are
    extended by notification several times a year and bolting this on later means
    rewriting date resolution and every reminder that depends on it.

    The four kinds are genuinely different and conflating them corrupts data.
    ``WAIVER`` relieves a late fee **without moving the date** — treating it as an
    ``EXTENSION`` is the commonest bug in this space, and it produces a calendar
    that tells clients they have longer than they do.
    """

    class Kind(models.TextChoices):
        EXTENSION = "EXTENSION", _("Due date moved later")
        ADVANCEMENT = "ADVANCEMENT", _("Due date moved earlier")
        WAIVER = "WAIVER", _("Late fee or interest waived — date unchanged")
        AMNESTY = "AMNESTY", _("Window to file old periods without penalty")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    #: Denormalised rather than a FK. Publishing an extension is then a single
    #: indexed UPDATE against the register on (definition_code, period_key),
    #: which is the common case and the one that has to be fast.
    definition_code = models.SlugField(max_length=64, db_index=True)
    kind = models.CharField(max_length=12, choices=Kind.choices)

    notification_reference = models.CharField(
        max_length=120, help_text=_("e.g. 'Notification 07/2026 – Central Tax dated 31.03.2026'.")
    )
    notification_url = models.URLField(blank=True)

    #: Empty covers every period of the definition — used for a blanket
    #: procedural relaxation. Normally one specific period.
    period_key = models.CharField(max_length=32, blank=True, db_index=True)
    new_due_date = models.DateField(
        null=True,
        blank=True,
        help_text=_("Required for EXTENSION and ADVANCEMENT; meaningless for WAIVER."),
    )

    #: State-specific flood relief, turnover-banded relief. Empty is nationwide.
    jurisdictions = ArrayField(models.CharField(max_length=12), default=list, blank=True)
    #: Reuses the applicability DSL for turnover-banded or sector-limited relief.
    #: Zero new machinery, which is why extensions were affordable on day one.
    scope_rule = models.JSONField(default=dict, blank=True)
    #: What was relieved: {"late_fee": true, "interest": false}.
    relief = models.JSONField(default=dict, blank=True)

    amnesty_window_start = models.DateField(null=True, blank=True)
    amnesty_window_end = models.DateField(null=True, blank=True)

    published_at = models.DateField(help_text=_("Date of the notification, not of data entry."))
    #: Set when a later notification supersedes this one. Extensions get extended
    #: a second time more often than anyone expects.
    superseded_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ["-published_at", "definition_code"]
        constraints = [
            # An EXTENSION with no new date is a data-entry accident that would
            # silently resolve to the original date and look like it worked.
            models.CheckConstraint(
                condition=~Q(kind__in=["EXTENSION", "ADVANCEMENT"]) | Q(new_due_date__isnull=False),
                name="extension_has_new_date",
            ),
        ]
        indexes = [
            models.Index(
                fields=["definition_code", "period_key"], name="extension_code_period_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.definition_code} ({self.notification_reference})"

    @property
    def moves_the_date(self) -> bool:
        return self.kind in {self.Kind.EXTENSION, self.Kind.ADVANCEMENT}
