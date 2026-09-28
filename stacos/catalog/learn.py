"""
Learn: the compliance catalog read by legal form, before any entity exists.

Every other screen starts from a business. The calendar and the compliance
library take one entity's profile — its registrations, its turnover, the answers
it has given — and ask the engine what *that business* owes. This module asks
the question the other way round: given only a legal form, what does the catalog
carry that could apply to it? It is what the Learn page shows, and nothing else
reads it.

Three properties make that safe to put in front of a user, and each is a
property of what this module *cannot do* rather than of care taken by a caller:

* **It reads no customer data.** The catalog, the jurisdiction pack, holiday
  calendars and published government extensions — platform-owned reference
  data, identical for every tenant — and nothing else. It imports no
  tenant-owned app, so an entity, a profile or a register is not something it
  could reach for by mistake. ``tests/obligations/test_learn.py`` asserts both
  the import list and the tables its queries touch.
* **Nothing depends on it.** The planner decides applicability with
  ``evaluate()`` against a real profile, and dates with its own calls into
  ``stacos.engine.dates``. A test asserts that nothing on the materialisation
  path imports this module, so a wrong answer here is a moment's confusion on a
  navigation screen, never a wrong calendar.
* **It does no date arithmetic.** "Next due" is ``generate_periods`` and
  ``resolve_due_date`` — the two functions the planner calls, fed the same fiscal
  year, holiday calendar and extensions — with no profile attached: no events, no
  licence expiry, no predecessor. A rule that dates itself from one of those
  resolves to nothing, and nothing is what the page shows.

"Could apply" is ``DefinitionVersion.possible_entity_types``, the probe index: a
rule that cannot be *refuted* for this legal form knowing only the form. It is
advisory by design, which is exactly the claim the page makes, and the page says
so in words.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Q, QuerySet

from stacos.catalog.loader import PROBE_SIGNATURE, probe_entity_types
from stacos.catalog.models import DefinitionVersion, PublicationStatus
from stacos.catalog.snapshots import (
    build_calendar_snapshot,
    build_extension_set,
    calendar_keys_in,
    fiscal_year_for,
    version_to_snapshot,
)
from stacos.engine.dates import generate_periods, resolve_due_date, static_max_lag_days
from stacos.engine.types import (
    CalendarSnapshot,
    DefinitionSnapshot,
    DueDateResolution,
    ExtensionSet,
    FiscalYearConvention,
    Periodicity,
)
from stacos.jurisdictions.models import JurisdictionPack

__all__ = [
    "EntityType",
    "EntityTypeCard",
    "LearnRow",
    "entity_type_cards",
    "find_entity_type",
    "next_due",
    "next_due_dates",
    "obligations_for",
    "pack_for",
]

#: Periodicities with no schedule of their own. An event-based rule has no
#: periods until something is recorded — ``generate_periods`` returns none for
#: it. A one-time rule's single "period" is the generation window itself, so a
#: date resolved from it would be an artefact of the window chosen here rather
#: than a date in the statute.
_UNSCHEDULED = frozenset({Periodicity.EVENT_BASED, Periodicity.ONE_TIME})

#: How far past the widest rule's lag the window reaches: a year, so that an
#: annual rule whose date this year has already passed still finds next year's.
_LOOKAHEAD_DAYS = 366


@dataclass(frozen=True, slots=True)
class EntityType:
    """One legal form, as the jurisdiction pack names and describes it."""

    code: str
    name: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class EntityTypeCard:
    entity_type: EntityType
    #: Definitions in force today that this legal form cannot be ruled out for.
    obligation_count: int


@dataclass(frozen=True, slots=True)
class LearnRow:
    """One definition that could apply to a legal form, and its next standard
    statutory date."""

    #: The version in force on the day the page was built. Carries everything
    #: the row shows — title, summary, frequency, review date, confidence.
    version: DefinitionVersion
    #: ``None`` when the rule has no generic date: it waits on an event, or has
    #: no recurring schedule. Shown as a blank, never as a guess.
    next_due: DueDateResolution | None = None

    @property
    def code(self) -> str:
        return self.version.definition.code

    @property
    def category(self) -> str:
        return self.version.definition.category

    @property
    def title(self) -> str:
        return self.version.title

    @property
    def notification(self) -> str:
        """The notification that moved ``next_due``, for the struck-through
        original the due badge shows beside it."""
        extension = self.next_due.applied_extension if self.next_due else None
        return extension.notification_reference if extension else ""


# ---------------------------------------------------------------------------
# Legal forms
# ---------------------------------------------------------------------------


def pack_for(country: str) -> JurisdictionPack | None:
    """The pack the real calendar computes against, looked up the way
    ``stacos.obligations.services`` looks it up — by country alone — so this
    page and a client's calendar cannot be reading two fiscal-year conventions.
    """
    return JurisdictionPack.objects.filter(country=country).first()


def _entity_types(pack: JurisdictionPack) -> tuple[EntityType, ...]:
    """The pack's legal forms, in the pack's own order."""
    found: list[EntityType] = []
    for item in pack.entity_types or ():
        if not isinstance(item, Mapping) or not item.get("code"):
            continue
        code = str(item["code"])
        found.append(
            EntityType(
                code=code,
                name=str(item.get("name") or code),
                description=str(item.get("description") or ""),
            )
        )
    return tuple(found)


def find_entity_type(pack: JurisdictionPack, code: str) -> EntityType | None:
    return next((item for item in _entity_types(pack) if item.code == code), None)


def entity_type_cards(pack: JurisdictionPack, *, as_of: date) -> tuple[EntityTypeCard, ...]:
    """Every legal form in the pack, each with how many definitions in force on
    ``as_of`` could apply to it. One query for the whole catalog."""
    counts: Counter[str] = Counter()
    for version in _in_force(pack.country, as_of=as_of).only(
        "probe_signature", "possible_entity_types", "applicability_rule"
    ):
        counts.update(_possible_types(version, country=pack.country))

    return tuple(
        EntityTypeCard(entity_type=item, obligation_count=counts[item.code])
        for item in _entity_types(pack)
    )


# ---------------------------------------------------------------------------
# One legal form's obligations
# ---------------------------------------------------------------------------


def obligations_for(
    entity_type: str, *, pack: JurisdictionPack, as_of: date
) -> tuple[LearnRow, ...]:
    """Every definition in force on ``as_of`` that ``entity_type`` cannot be
    ruled out for, each with its next standard statutory date.

    A fixed number of queries however long the list: the definitions in force,
    every published version of those, and the shared inputs ``next_due_dates``
    builds once. Nothing runs per row.
    """
    current = [
        version
        for version in _in_force(pack.country, as_of=as_of).select_related("definition")
        if entity_type in _possible_types(version, country=pack.country)
    ]
    if not current:
        return ()

    codes = [version.definition.code for version in current]
    history: dict[str, list[DefinitionSnapshot]] = {code: [] for code in codes}
    for version in (
        _published(pack.country)
        .filter(definition__code__in=codes)
        .select_related("definition")
        .order_by("definition__code", "effective_from")
    ):
        history[version.definition.code].append(version_to_snapshot(version))

    due = next_due_dates(history, pack=pack, as_of=as_of)
    return tuple(
        LearnRow(version=version, next_due=due.get(version.definition.code)) for version in current
    )


def _published(country: str) -> QuerySet[DefinitionVersion]:
    """The rows ``build_catalog`` snapshots for the real calendar: published
    versions of the active definitions in ``country``."""
    return DefinitionVersion.objects.filter(
        status=PublicationStatus.PUBLISHED,
        definition__country=country,
        definition__is_active=True,
    )


def _in_force(country: str, *, as_of: date) -> QuerySet[DefinitionVersion]:
    """The version governing ``as_of``, for each definition that has one — the
    same window ``build_catalog`` applies when it is given a date. The exclusion
    constraint on published versions makes that at most one per definition."""
    return (
        _published(country)
        .filter(effective_from__lte=as_of)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=as_of))
    )


def _possible_types(version: DefinitionVersion, *, country: str) -> frozenset[str]:
    """The legal forms this version could apply to, from the probe index.

    A row indexed against an older entity-type vocabulary is re-probed rather
    than trusted: its stored list is missing every type added since, and a
    missing type reads on this page as "nothing applies". ``probe_entity_types``
    is the loader's own computation, so the answer is the one the next
    ``loadcatalog`` stores.
    """
    if version.probe_signature == PROBE_SIGNATURE:
        return frozenset(version.possible_entity_types)
    return frozenset(probe_entity_types(version.applicability_rule, country=country))


# ---------------------------------------------------------------------------
# Next due
# ---------------------------------------------------------------------------


def next_due_dates(
    catalog: Mapping[str, Sequence[DefinitionSnapshot]],
    *,
    pack: JurisdictionPack,
    as_of: date,
) -> dict[str, DueDateResolution]:
    """The next standard statutory date of each definition, keyed by code.
    Definitions with no generic date are absent.

    ``catalog`` maps each code to *every* published version of it, because the
    version in force today need not be the one governing the next period — the
    planner is handed every version for the same reason.

    The shared inputs are built once for the whole list, by the builders the real
    calendar uses: the fiscal year from the pack, the weekend and the holiday
    calendars these rules name, and every extension published by ``as_of``.
    """
    snapshots = [snapshot for versions in catalog.values() for snapshot in versions]
    if not snapshots:
        return {}

    window_start, window_end = _window(snapshots, as_of=as_of)
    fy = fiscal_year_for(pack)
    calendars = build_calendar_snapshot(
        country=pack.country,
        calendar_keys=calendar_keys_in(snapshots),
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
    )
    extensions = build_extension_set(definition_codes=catalog.keys(), as_of=as_of)

    found: dict[str, DueDateResolution] = {}
    for code, versions in catalog.items():
        resolution = next_due(
            versions,
            as_of=as_of,
            window_start=window_start,
            window_end=window_end,
            fy=fy,
            calendars=calendars,
            extensions=extensions,
        )
        if resolution is not None:
            found[code] = resolution
    return found


def _window(snapshots: Sequence[DefinitionSnapshot], *, as_of: date) -> tuple[date, date]:
    """From a little before today to comfortably past the worst-case lag.

    Opened the widest rule's lag behind ``as_of`` for the reason the planner
    widens its own window: a period that closed before today can still fall due
    after it — the return for the year that ended in March is due in October.
    Closed that lag plus a year ahead, so the next occurrence of even the
    slowest annual rule is generated. One window for every rule, because the
    holiday calendar is loaded once for all of them.
    """
    lag = max(static_max_lag_days(snapshot.due_rule) for snapshot in snapshots)
    return as_of - timedelta(days=lag), as_of + timedelta(days=lag + _LOOKAHEAD_DAYS)


def next_due(
    versions: Sequence[DefinitionSnapshot],
    *,
    as_of: date,
    window_start: date,
    window_end: date,
    fy: FiscalYearConvention,
    calendars: CalendarSnapshot,
    extensions: ExtensionSet,
) -> DueDateResolution | None:
    """The soonest due date on or after ``as_of`` across every version of one
    definition, or ``None`` when it has no standard date.

    Pure — no database, no clock. It is the planner's loop with the profile
    taken out: the same ``generate_periods``, the same ``is_effective_for`` choice
    of the version governing each period, the same ``resolve_due_date``, and then
    nothing but picking the earliest date that has not yet passed.

    ``None`` is an answer, not a failure. Resolution is given no events, no
    trigger date, no licence expiry and no predecessor, so a rule that dates
    itself from one of those — an AGM-anchored filing, a licence renewal —
    resolves to nothing. No scope jurisdictions either, so only a nationwide
    extension moves a date: relief granted to one state is not the standard
    date, and ``ExtensionRecord.covers`` does not apply it here.
    """
    candidates: list[DueDateResolution] = []
    for version in versions:
        if version.periodicity in _UNSCHEDULED:
            continue
        for period in generate_periods(
            periodicity=version.periodicity,
            fy=fy,
            window_start=window_start,
            window_end=window_end,
            period_anchor=version.period_anchor,
        ):
            if not version.is_effective_for(period):
                continue
            resolution = resolve_due_date(
                version, period, calendars=calendars, fy=fy, extensions=extensions
            )
            if resolution.effective_date is not None and resolution.effective_date >= as_of:
                candidates.append(resolution)

    return min(
        candidates, key=lambda resolution: resolution.effective_date or date.max, default=None
    )
