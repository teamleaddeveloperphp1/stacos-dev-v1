"""
The frozen value types that cross the boundary between Django and the engine.

Django builds these; the engine only reads them. Keeping the boundary explicit is
what lets the engine stay pure — and what makes it possible to run a whole
calendar generation in a unit test with no database at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

__all__ = [
    "OCCURRENCE_SPACE",
    "CalendarSnapshot",
    "DefinitionSnapshot",
    "Diagnostic",
    "DueDateResolution",
    "EventOccurrence",
    "EvidenceRequirement",
    "ExtensionKind",
    "ExtensionRecord",
    "ExtensionSet",
    "FiscalYearConvention",
    "Identity",
    "InstanceScope",
    "Period",
    "Periodicity",
    "ScopeRef",
    "Severity",
    "ShiftRule",
    "occurrence_number",
]


class Periodicity(StrEnum):
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    HALF_YEARLY = "HALF_YEARLY"
    ANNUAL = "ANNUAL"
    EVENT_BASED = "EVENT_BASED"
    ONE_TIME = "ONE_TIME"


class InstanceScope(StrEnum):
    """What an obligation instance is *per*.

    The single most consequential enum in the engine. GSTR-3B is filed per GSTIN,
    not per company: an entity registered in six states files six of them every
    month. Professional tax is per state registration; factory returns are per
    premises. Getting this wrong is not a rendering bug, it is five missing
    filings a month.
    """

    ENTITY = "ENTITY"
    REGISTRATION = "REGISTRATION"
    PREMISES = "PREMISES"


class ShiftRule(StrEnum):
    """What to do when a computed date is not a working day.

    ``NONE`` is the correct default for Indian statutory tax dates: the portals
    accept filings on a Sunday, and relief comes through explicit government
    extensions rather than through weekend shifting. Defaulting to
    ``NEXT_WORKING_DAY`` produces dates that are wrong in law.
    """

    NONE = "NONE"
    NEXT_WORKING_DAY = "NEXT_WORKING_DAY"
    PREVIOUS_WORKING_DAY = "PREVIOUS_WORKING_DAY"


class ExtensionKind(StrEnum):
    EXTENSION = "EXTENSION"
    ADVANCEMENT = "ADVANCEMENT"
    #: Late fee or interest relieved — **the due date does not move**. Conflating
    #: this with EXTENSION corrupts dates, and is the commonest bug in this space.
    WAIVER = "WAIVER"
    AMNESTY = "AMNESTY"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ---------------------------------------------------------------------------
# Calendar primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FiscalYearConvention:
    """When a financial year starts, and how it is written.

    India starts 1 April, the UK 6 April, most others 1 January. Nothing in the
    engine assumes any of them; this comes from a jurisdiction pack.
    """

    start_month: int = 1
    start_day: int = 1
    label_template: str = "FY {start_year}"

    def year_of(self, day: date) -> tuple[date, date, str]:
        """Return ``(start, end, label)`` of the fiscal year containing ``day``."""
        start_year = (
            day.year if (day.month, day.day) >= (self.start_month, self.start_day) else day.year - 1
        )
        start = date(start_year, self.start_month, self.start_day)
        end = _add_months(start, 12) - _ONE_DAY
        return start, end, self.label(start_year)

    def label(self, start_year: int) -> str:
        return self.label_template.format(
            start_year=start_year,
            end_year=start_year + 1,
            end_year_short=f"{(start_year + 1) % 100:02d}",
        )

    @property
    def is_calendar_year(self) -> bool:
        return (self.start_month, self.start_day) == (1, 1)


@dataclass(frozen=True, slots=True)
class Period:
    """One occurrence of a recurring obligation.

    ``key`` is canonical, unique within a definition, and sorts chronologically
    as a string so an index on it is useful without a separate ordinal column.
    """

    key: str
    label: str
    start: date
    end: date
    ordinal: int = 0

    def __lt__(self, other: Period) -> bool:
        return (self.start, self.key) < (other.start, other.key)


@dataclass(frozen=True, slots=True)
class CalendarSnapshot:
    """Holidays and weekends, resolved for the engine to read synchronously.

    Weekend days are per country because the Gulf states rest on different days,
    and the mapping is looked up per country rather than assumed.
    """

    holidays: Mapping[str, frozenset[date]] = field(default_factory=dict)
    weekend_days: Mapping[str, frozenset[int]] = field(default_factory=dict)

    def is_working_day(self, day: date, *, country: str, calendars: Sequence[str] = ()) -> bool:
        # Sunday-only is the default because it is by far the commonest, but any
        # country that differs supplies its own set.
        weekend = self.weekend_days.get(country, frozenset({6}))
        if day.weekday() in weekend:
            return False
        return all(day not in self.holidays.get(key, frozenset()) for key in calendars)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    key: str
    label: str
    kind: str = "DOC"
    mandatory_for_close: bool = False
    file_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DefinitionSnapshot:
    """One published version of a compliance definition, as the engine sees it."""

    code: str
    version: int
    title: str
    country: str
    periodicity: Periodicity
    due_rule: Mapping[str, Any]
    applicability_rule: Mapping[str, Any] = field(default_factory=dict)
    jurisdictions: frozenset[str] = field(default_factory=frozenset)
    category: str = ""
    instance_scope: InstanceScope = InstanceScope.ENTITY
    scope_selector: Mapping[str, Any] = field(default_factory=dict)
    #: FY-anchored or calendar-anchored periods. TDS quarters are FY-anchored
    #: (Q1 is Apr–Jun); some state professional tax is calendar-quarter. Assuming
    #: either one breaks the other.
    period_anchor: str = "FY"
    effective_from: date = date(1900, 1, 1)
    effective_to: date | None = None
    default_owner_role: str = ""
    evidence_requirements: tuple[EvidenceRequirement, ...] = ()
    #: For ``periodicity: EVENT_BASED`` only: ``{event_key, when?}``. What makes
    #: an instance exist at all, as opposed to ``due_rule`` which only says when
    #: an instance that already exists falls due. Empty for every periodic rule.
    trigger: Mapping[str, Any] = field(default_factory=dict)

    def is_effective_for(self, period: Period) -> bool:
        """Whether this version governs the given period.

        Compared against the period *end*: a rule in force when the period closed
        is the rule that governs the filing for it.
        """
        if period.end < self.effective_from:
            return False
        return not (self.effective_to and period.start > self.effective_to)


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtensionRecord:
    """A published government extension, waiver or amnesty."""

    definition_code: str
    kind: ExtensionKind
    notification_reference: str = ""
    period_key: str = ""
    new_due_date: date | None = None
    jurisdictions: frozenset[str] = field(default_factory=frozenset)
    relief: Mapping[str, Any] = field(default_factory=dict)
    published_at: date | None = None

    def covers(self, *, code: str, period_key: str, jurisdictions: frozenset[str]) -> bool:
        if self.definition_code != code:
            return False
        if self.period_key and self.period_key != period_key:
            return False
        # No jurisdictions named means nationwide.
        return not (self.jurisdictions and not (self.jurisdictions & jurisdictions))


@dataclass(frozen=True, slots=True)
class ExtensionSet:
    """Every published extension, indexed for lookup during date resolution."""

    records: tuple[ExtensionRecord, ...] = ()

    def match(
        self, *, code: str, period_key: str, jurisdictions: frozenset[str] = frozenset()
    ) -> ExtensionRecord | None:
        """The extension in force, or ``None``.

        Later publications win, because an extension is frequently extended a
        second time and the most recent notification is the operative one.
        """
        candidates = [
            r
            for r in self.records
            if r.covers(code=code, period_key=period_key, jurisdictions=jurisdictions)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.published_at or date.min)


# ---------------------------------------------------------------------------
# Resolution results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DueDateResolution:
    """The outcome of resolving a due rule for one period.

    ``original_date`` is always retained even when an extension moves the
    effective date, because the client needs to see both — struck through, with
    the notification reference behind it.
    """

    effective_date: date | None
    original_date: date | None = None
    applied_extension: ExtensionRecord | None = None
    relief: Mapping[str, Any] = field(default_factory=dict)
    #: Set when the rule could not be resolved — an AGM date nobody has entered,
    #: a licence with no expiry recorded. The instance still materialises, with a
    #: null due date and a prompt. A guessed date would be worse than none.
    blocking_input: str = ""

    @property
    def resolved(self) -> bool:
        return self.effective_date is not None


@dataclass(frozen=True, slots=True)
class ScopeRef:
    """What an instance is *for*, when a definition fans out."""

    kind: InstanceScope = InstanceScope.ENTITY
    ref: str = ""
    label: str = ""
    jurisdiction: str = ""
    valid_from: date | None = None
    valid_to: date | None = None

    def covers(self, period: Period) -> bool:
        """Whether this scope was live during the period.

        A surrendered GSTIN or a closed factory must stop generating filings from
        the date it lapsed — otherwise clients are chased for returns they no
        longer owe.
        """
        if self.valid_from and period.end < self.valid_from:
            return False
        return not (self.valid_to and period.start > self.valid_to)


@dataclass(frozen=True, slots=True)
class Identity:
    """The natural key of an obligation instance.

    Note what is absent: the definition *version*. Including it would make every
    catalog version bump duplicate every instance. Version is an attribute that
    gets updated in place.
    """

    definition_code: str
    scope_ref: str
    period_key: str
    occurrence: int = 0


@dataclass(frozen=True, slots=True)
class EventOccurrence:
    """One recorded thing that happened, that an obligation may hang off.

    ``ref`` is opaque to the engine, stable for the life of the record, and the
    only input to the occurrence number. That is the whole design: an occurrence
    derived from *position* renumbers when a sibling is removed, and a renumbered
    occurrence is — under the natural key — a different obligation. Two directors
    appointed on one day, then the first record deleted: the second one's
    identity must not move, or the DIR-12 somebody had started preparing is
    superseded and an empty duplicate appears beside it.
    """

    key: str
    occurred_on: date
    ref: str
    scope_ref: str = ""
    label: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)


#: How many distinct occurrences can share one period key. Bounded by the
#: database column, not by the hash.
OCCURRENCE_SPACE = 32768


def occurrence_number(ref: str) -> int:
    """A stable small integer identifying one occurrence within a period key.

    ``blake2s`` rather than the builtin ``hash``: ``hash`` is salted per process
    by ``PYTHONHASHSEED``, so one event would number differently in the web
    process and in the Celery worker. The nightly job would then fail to
    recognise the rows the interactive path created and would duplicate every
    event-driven obligation, every night, indefinitely. This line is the one most
    likely to be "simplified" into a catastrophe, which is why the tests pin its
    output to a literal.

    Masked to fifteen bits because ``ObligationInstance.occurrence`` is a
    ``PositiveSmallIntegerField`` — PostgreSQL ``smallint``, so 0-32767. Sixteen
    bits overflowed it for about half of all inputs, which is not a rare edge
    case but a coin flip on every event recorded.
    """
    digest = int.from_bytes(hashlib.blake2s(ref.encode("utf-8"), digest_size=2).digest(), "big")
    return digest % OCCURRENCE_SPACE


@dataclass(frozen=True, slots=True)
class Diagnostic:
    severity: Severity
    code: str
    message: str
    definition_code: str = ""


# ---------------------------------------------------------------------------
# Small date helpers, kept here so the engine has no external date dependency.
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

_ONE_DAY = timedelta(days=1)

_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def days_in_month(year: int, month: int) -> int:
    if month == 2 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        return 29
    return _DAYS_IN_MONTH[month - 1]


def _add_months(day: date, months: int) -> date:
    """Add months, clamping the day to the target month's length.

    31 January plus one month is 28 (or 29) February. Every alternative — 3
    March, an error — is worse for a due-date calculation.
    """
    total = (day.year * 12 + day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(day.day, days_in_month(year, month)))


def set_day_of_month(day: date, day_of_month: int) -> date:
    """Set the day within its month. ``-1`` means the last day."""
    last = days_in_month(day.year, day.month)
    target = last if day_of_month == -1 else min(day_of_month, last)
    return date(day.year, day.month, target)


def to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
