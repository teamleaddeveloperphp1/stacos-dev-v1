"""
Obligations an entity writes for itself: a licence condition, a lender's
covenant, an internal control the firm runs monthly.

The design rests on one decision: **a custom obligation is a definition, not a
reminder.** It is handed to the planner as a ``DefinitionSnapshot`` beside the
catalog (see :func:`custom_snapshots` and ``services.preview``), so every
instance it produces is an ordinary ``ObligationInstance`` — the same lifecycle,
evidence guard, checklist, assignment, timeline and audit trail, and the same
idempotent nightly rebuild. Nothing below the planner needs to know who wrote
the rule.

Three operations change one, and each ends in a synchronous rebuild so the
calendar a person is looking at already reflects what they just did:

:func:`create_custom_obligation`
    A definition and its first schedule version.
:func:`update_custom_obligation`
    Details (name, description, category) are edited in place. A
    schedule change is a **new version** with its own window — see
    :func:`_split_windows` — so periods that already ended keep the rule that
    dated them.
:func:`withdraw_custom_obligation`
    Stops it producing anything. The planner then treats its instances exactly
    as it treats a catalog definition that stopped applying: filed ones stay as
    they are, ones somebody worked on are superseded and kept, untouched future
    ones are archived. Nothing is deleted.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from stacos.catalog.snapshots import fiscal_year_for
from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.core.scope import across_all_categories
from stacos.engine.dates import generate_periods
from stacos.engine.lifecycle import OPEN_STATES
from stacos.engine.types import (
    DefinitionSnapshot,
    FiscalYearConvention,
    InstanceScope,
    Periodicity,
)
from stacos.jurisdictions.models import JurisdictionPack
from stacos.obligations.models import (
    CustomObligation,
    CustomObligationVersion,
    MaterialisationRun,
    ObligationInstance,
    is_custom_code,
)
from stacos.tenancy.models import Entity

__all__ = [
    "DETAIL_FIELDS",
    "SCHEDULE_FIELDS",
    "SCHEDULE_PERIODICITIES",
    "CustomObligationError",
    "create_custom_obligation",
    "current_version",
    "custom_snapshots",
    "definition_for",
    "evidence_requirements_for",
    "removal_reasons",
    "resolve_schedule",
    "schedule_summary",
    "update_custom_obligation",
    "withdraw_custom_obligation",
]

#: What a person can schedule their own obligation as. ``EVENT_BASED`` needs a
#: registered event type and ``ONE_TIME`` has no due date the engine can compute
#: from a period, so neither is offered.
SCHEDULE_PERIODICITIES: tuple[Periodicity, ...] = (
    Periodicity.MONTHLY,
    Periodicity.QUARTERLY,
    Periodicity.HALF_YEARLY,
    Periodicity.ANNUAL,
)

#: Edited in place on :class:`CustomObligation`.
DETAIL_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "category",
)

#: Versioned on :class:`CustomObligationVersion`. Changing any one of them is a
#: schedule change and opens a new version.
SCHEDULE_FIELDS: tuple[str, ...] = (
    "periodicity",
    "period_anchor",
    "due_offset_months",
    "due_day_of_month",
)


class CustomObligationError(Exception):
    """A change that cannot be made, with a message fit to show a user."""


# ---------------------------------------------------------------------------
# Reading: what the planner and the pages see
# ---------------------------------------------------------------------------


def custom_snapshots(entity: Entity) -> tuple[DefinitionSnapshot, ...]:
    """Every live custom obligation's schedule versions, as the engine reads them.

    Oldest version first within each obligation. The version windows never
    claim the same period (see :func:`_split_windows`), so the order matters
    only as a tiebreak nobody should ever need.
    """
    obligations = (
        CustomObligation.objects.filter(entity=entity, withdrawn_at__isnull=True)
        .prefetch_related(
            Prefetch(
                "versions",
                queryset=CustomObligationVersion.objects.order_by("version"),
            )
        )
        .order_by("code")
    )
    return tuple(
        _to_snapshot(obligation, version, country=entity.country)
        for obligation in obligations
        for version in obligation.versions.all()
        if version.governs_anything
    )


def _to_snapshot(
    obligation: CustomObligation, version: CustomObligationVersion, *, country: str
) -> DefinitionSnapshot:
    return DefinitionSnapshot(
        code=obligation.code,
        version=version.version,
        title=obligation.title,
        country=country,
        periodicity=Periodicity(version.periodicity),
        due_rule=version.due_rule,
        # Empty applies to everyone — and "everyone" is the one entity that
        # wrote it. There is no fact to be missing, so it is always confirmed.
        applicability_rule={},
        category=obligation.category,
        instance_scope=InstanceScope.ENTITY,
        period_anchor=version.period_anchor,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
        evidence_requirements=(),
    )


def removal_reasons(entity: Entity) -> dict[str, str]:
    """What a supersession of a custom obligation's instance should say.

    Derived from stored state rather than passed in by whoever made the change,
    so the interactive rebuild and the nightly one write the same words.
    """
    reasons: dict[str, str] = {}
    for code, withdrawn_at, reason in CustomObligation.objects.filter(entity=entity).values_list(
        "code", "withdrawn_at", "withdraw_reason"
    ):
        if withdrawn_at is not None:
            reasons[code] = f"Withdrawn: {reason}" if reason else "Withdrawn."
        else:
            reasons[code] = "No longer on this obligation's schedule after it was changed."
    return reasons


def definition_for(code: str, version: int) -> Any:
    """The rule behind an instance: a catalog ``DefinitionVersion`` or a
    :class:`CustomObligationVersion`.

    One lookup for every page that shows "what this requires" — the detail
    page, the evidence card, the completion modal. The two types expose the
    attributes those templates read under the same names; ``is_custom`` tells
    them which heading to put above it.
    """
    if is_custom_code(code):
        return (
            CustomObligationVersion.objects.select_related("obligation")
            .filter(obligation__code=code, version=version)
            .first()
        )

    from stacos.catalog.models import DefinitionVersion

    return (
        DefinitionVersion.objects.select_related("definition", "definition__authority")
        .filter(definition__code=code, version=version)
        .first()
    )


def evidence_requirements_for(code: str, version: int) -> list[dict[str, Any]]:
    """The evidence list the completion guard enforces, whoever wrote the rule."""
    if is_custom_code(code):
        return []

    from stacos.catalog.models import DefinitionVersion

    requirements = (
        DefinitionVersion.objects.filter(definition__code=code, version=version)
        .values_list("evidence_requirements", flat=True)
        .first()
    )
    return list(requirements or [])


def current_version(obligation: CustomObligation) -> CustomObligationVersion:
    """The version governing periods from now on — always the highest number."""
    version = obligation.versions.order_by("-version").first()
    if version is None:  # pragma: no cover - every create writes version 1
        raise CustomObligationError("This obligation has no schedule.")
    return version


def schedule_summary(version: CustomObligationVersion) -> str:
    """ "Quarterly (financial year) — due on the 20th, 1 month after each period ends"."""
    from stacos.core.templatetags.stacos import periodicity_adjective

    frequency = str(periodicity_adjective(version.periodicity))
    if version.periodicity != Periodicity.MONTHLY:
        frequency = f"{frequency} ({version.get_period_anchor_display().lower()})"

    day = version.due_day_of_month
    months = version.due_offset_months
    if day is None or (day == -1 and months == 0):
        return f"{frequency} — due on the last day of each period"
    day_label = "the last day" if day == -1 else f"the {_ordinal(day)}"
    if months == 0:
        return f"{frequency} — due on {day_label} of the month each period ends"
    unit = "month" if months == 1 else "months"
    return f"{frequency} — due on {day_label}, {months} {unit} after each period ends"


def _ordinal(day: int) -> str:
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _fiscal_year(entity: Entity) -> FiscalYearConvention:
    pack = JurisdictionPack.objects.filter(country=entity.country).first()
    if pack is None:
        raise CustomObligationError(
            "This entity's country has no jurisdiction pack, so there is no fiscal "
            "year to schedule against."
        )
    return fiscal_year_for(pack)


def _period_due_on(
    due_date: date, *, periodicity: str, period_anchor: str, fy: FiscalYearConvention
) -> Any:
    """The period an occurrence due on ``due_date`` belongs to: the latest one
    ending on or before it. A due date that falls inside a period belongs to the
    one before — "due 15 December, quarterly" is the July–September quarter."""
    periods = generate_periods(
        periodicity=Periodicity(periodicity),
        fy=fy,
        window_start=due_date - timedelta(days=400),
        window_end=due_date,
        period_anchor=period_anchor,
    )
    ended = [period for period in periods if period.end <= due_date]
    if not ended:
        raise CustomObligationError("No period ends on or before that due date.")
    return max(ended, key=lambda period: period.end)


def resolve_schedule(
    entity: Entity, schedule: Mapping[str, Any]
) -> tuple[dict[str, Any], date | None]:
    """The form's schedule — frequency plus one due date — as stored fields.

    Returns the :data:`SCHEDULE_FIELDS` values, and the end of the period the
    given due date belongs to (``None`` when no date was given), so the caller
    can make sure that very occurrence is tracked. The date becomes "N months
    after each period ends, on day D", so one date repeats for every period: a
    quarterly obligation due 20 October is next due 20 January. A date on the
    last day of its month is stored as "the last day", so 31 December is next
    due 31 March, not 31 March's non-existent neighbour.
    """
    resolved = {name: schedule[name] for name in ("periodicity", "period_anchor")}
    due_date: date | None = schedule.get("due_date")
    if due_date is None:
        return {**resolved, "due_offset_months": 0, "due_day_of_month": None}, None

    period = _period_due_on(
        due_date,
        periodicity=resolved["periodicity"],
        period_anchor=resolved["period_anchor"],
        fy=_fiscal_year(entity),
    )
    months = (due_date.year - period.end.year) * 12 + due_date.month - period.end.month
    last_day = (due_date + timedelta(days=1)).day == 1
    return {
        **resolved,
        "due_offset_months": months,
        "due_day_of_month": -1 if last_day else due_date.day,
    }, period.end


def _rebuild(entity: Entity, *, actor: Any, as_of: date) -> MaterialisationRun:
    from stacos.obligations.services import materialise

    return materialise(
        entity,
        as_of=as_of,
        trigger=MaterialisationRun.Trigger.CUSTOM_OBLIGATION,
        actor=actor,
    )


def _schedule_payload(version: CustomObligationVersion) -> dict[str, Any]:
    payload = {name: getattr(version, name) for name in SCHEDULE_FIELDS}
    payload["effective_from"] = version.effective_from.isoformat()
    payload["version"] = version.version
    return payload


def _details_payload(obligation: CustomObligation) -> dict[str, Any]:
    return {name: getattr(obligation, name) for name in DETAIL_FIELDS}


@across_all_categories
@transaction.atomic
def create_custom_obligation(
    entity: Entity,
    *,
    actor: Any,
    details: Mapping[str, Any],
    schedule: Mapping[str, Any],
    as_of: date,
    starts_on: date | None = None,
) -> CustomObligation:
    """Write a new obligation and put its instances on the calendar now.

    Tracking starts ``as_of``: the first occurrence is the period that ends on
    or after it — or, when the due date given belongs to a period that already
    ended, that period, so the date the person typed is on the calendar.
    ``starts_on`` backdates that, for seed data only — the form does not ask.
    """
    stored, due_period_end = resolve_schedule(entity, schedule)
    effective_from = starts_on or as_of
    if due_period_end is not None:
        effective_from = min(effective_from, due_period_end)

    obligation = CustomObligation(
        tenant=entity.tenant,
        entity=entity,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        **{name: details[name] for name in DETAIL_FIELDS},
    )
    obligation.save()

    version = CustomObligationVersion.objects.create(
        tenant=entity.tenant,
        entity=entity,
        obligation=obligation,
        version=1,
        effective_from=effective_from,
        created_by=obligation.created_by,
        **stored,
    )

    run = _rebuild(entity, actor=actor, as_of=as_of)

    record_event(
        action=AuditAction.CREATE,
        actor=actor,
        obj=obligation,
        after={**_details_payload(obligation), "schedule": _schedule_payload(version)},
        context={"calendar": run.summary()},
    )
    return obligation


def _split_windows(
    current: CustomObligationVersion, *, applies_from: date, fy: FiscalYearConvention
) -> date:
    """Close ``current`` so it governs exactly the periods ending before ``applies_from``.

    The rule is the engine's own: a period is governed by the rule in force
    when it *closes*. The successor opens at ``applies_from``, and
    ``DefinitionSnapshot.is_effective_for`` admits a period when it ends on or
    after ``effective_from`` — so it takes every period ending on or after that
    day. This version must then stop short of its own period in progress on
    ``applies_from``: the same check admits a period while it *starts* on or
    before ``effective_to``, so the window closes the day before that period
    began, computed with the engine's own period generator in this version's
    frequency.

    With the frequency unchanged, the two windows tile exactly: a schedule
    changed mid-month produces one instance for that month, under the new rule.
    With the frequency changed, the new frequency's period in progress follows
    the new rule in full — monthly to quarterly from 15 November keeps
    October's monthly instance and adds the October–December quarter, rather
    than silently leaving a gap nobody is chased for.
    """

    in_progress = generate_periods(
        periodicity=Periodicity(current.periodicity),
        fy=fy,
        window_start=applies_from,
        window_end=applies_from,
        period_anchor=current.period_anchor,
    )
    start = in_progress[0].start if in_progress else applies_from
    return start - timedelta(days=1)


@across_all_categories
@transaction.atomic
def update_custom_obligation(
    obligation: CustomObligation,
    *,
    actor: Any,
    details: Mapping[str, Any],
    schedule: Mapping[str, Any],
    applies_from: date | None,
    as_of: date,
) -> CustomObligationVersion | None:
    """Apply an edit. Returns the new schedule version, when there is one.

    Details are corrected in place: the name on open instances follows the
    edit, and so does the category on every instance — category is an access
    boundary, and a row visible under one category while its siblings sit
    under another is a leak or a hole. A completed instance keeps the name it
    was completed under, as the record of what was done.
    """
    if obligation.is_withdrawn:
        raise CustomObligationError("This obligation has been withdrawn and can no longer change.")

    entity = obligation.entity
    before = _details_payload(obligation)
    current = current_version(obligation)
    before_schedule = _schedule_payload(current)

    changed_details = {
        name: details[name] for name in DETAIL_FIELDS if details[name] != getattr(obligation, name)
    }
    stored, _ = resolve_schedule(entity, schedule)
    schedule_changed = any(stored[name] != getattr(current, name) for name in SCHEDULE_FIELDS)

    if not changed_details and not schedule_changed:
        raise CustomObligationError("Nothing has changed.")

    new_version: CustomObligationVersion | None = None
    if schedule_changed:
        if applies_from is None:
            raise CustomObligationError("Say from when the new schedule applies.")
        if applies_from < current.effective_from:
            raise CustomObligationError(
                f"The current schedule only started on {current.effective_from:%d %b %Y}; "
                f"a change cannot apply before that."
            )
        current.effective_to = _split_windows(
            current, applies_from=applies_from, fy=_fiscal_year(entity)
        )
        current.save(update_fields=["effective_to", "updated_at"])
        new_version = CustomObligationVersion.objects.create(
            tenant=obligation.tenant,
            entity=entity,
            obligation=obligation,
            version=current.version + 1,
            effective_from=applies_from,
            created_by=actor if getattr(actor, "is_authenticated", False) else None,
            **stored,
        )

    if changed_details:
        for name, value in changed_details.items():
            setattr(obligation, name, value)
        obligation.save(update_fields=[*changed_details, "updated_at"])

        instances = ObligationInstance.objects.filter(
            entity=entity, definition_code=obligation.code
        )
        if "category" in changed_details:
            instances.update(category=obligation.category)
        if "title" in changed_details:
            instances.filter(state__in=[str(state) for state in OPEN_STATES]).update(
                title=obligation.title
            )

    run = _rebuild(entity, actor=actor, as_of=as_of)

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=obligation,
        before={
            **{name: before[name] for name in changed_details},
            **({"schedule": before_schedule} if new_version else {}),
        },
        after={
            **changed_details,
            **({"schedule": _schedule_payload(new_version)} if new_version else {}),
        },
        context={"calendar": run.summary()},
    )
    return new_version


@across_all_categories
@transaction.atomic
def withdraw_custom_obligation(
    obligation: CustomObligation, *, actor: Any, reason: str, as_of: date
) -> MaterialisationRun:
    """Stop an obligation producing anything, keeping everything it produced."""
    if obligation.is_withdrawn:
        raise CustomObligationError("This obligation is already withdrawn.")

    obligation.withdrawn_at = timezone.now()
    obligation.withdraw_reason = reason
    obligation.withdrawn_by = actor if getattr(actor, "is_authenticated", False) else None
    obligation.save(update_fields=["withdrawn_at", "withdraw_reason", "withdrawn_by", "updated_at"])

    run = _rebuild(obligation.entity, actor=actor, as_of=as_of)

    record_event(
        action=AuditAction.ARCHIVE,
        actor=actor,
        obj=obligation,
        before={"state": "active"},
        after={"state": "withdrawn", **run.as_dict()},
        context={"reason": reason},
    )
    return run
