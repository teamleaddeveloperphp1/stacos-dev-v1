"""
Period generation and due-date resolution.

Where the calendar actually comes from. Three things here are easy to get wrong
and expensive to get wrong, so each is called out where it happens:

1. **Offsets are months-plus-day-of-month, not days.** "The 20th of the following
   month" as ``offset_days: 20`` drifts across February.
2. **The horizon filters on the due date, not the period.** GSTR-9 for FY 2025-26
   is due 31 December 2026: a period that closed before the window opened
   produces an obligation inside it. Filtering by period silently loses every
   annual return.
3. **Indian statutory dates do not shift for weekends.** Relief comes from
   government extensions, not from the calendar.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from stacos.engine.types import (
    CalendarSnapshot,
    DefinitionSnapshot,
    DueDateResolution,
    ExtensionKind,
    ExtensionSet,
    FiscalYearConvention,
    Period,
    Periodicity,
    ShiftRule,
    _add_months,
    days_in_month,
    set_day_of_month,
)

__all__ = [
    "generate_periods",
    "resolve_due_date",
    "shift_to_working_day",
    "static_max_lag_days",
]

_ONE_DAY = timedelta(days=1)

#: Anchors that can fail to resolve, and therefore need a recorded event or
#: predecessor before a date exists at all.
_EVENT_ANCHORS = frozenset({"EVENT_DATE", "LICENCE_EXPIRY", "PREVIOUS_INSTANCE_DATE"})


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


def generate_periods(
    *,
    periodicity: Periodicity,
    fy: FiscalYearConvention,
    window_start: date,
    window_end: date,
    period_anchor: str = "FY",
) -> list[Period]:
    """Every period of the given shape overlapping the window.

    ``period_anchor`` decides whether quarters and half-years follow the fiscal
    year or the calendar. TDS quarters are FY-anchored (Q1 is Apr–Jun) while some
    state professional tax is calendar-quarter; hardcoding either breaks the
    other, so it is per definition.
    """
    if window_end < window_start:
        return []

    match periodicity:
        case Periodicity.MONTHLY:
            return _monthly(window_start, window_end)
        case Periodicity.QUARTERLY:
            return _blocked(window_start, window_end, fy, months=3, anchor=period_anchor, unit="Q")
        case Periodicity.HALF_YEARLY:
            return _blocked(window_start, window_end, fy, months=6, anchor=period_anchor, unit="H")
        case Periodicity.ANNUAL:
            return _annual(window_start, window_end, fy, anchor=period_anchor)
        case Periodicity.ONE_TIME:
            return [Period(key="ONCE", label="One time", start=window_start, end=window_end)]
        case Periodicity.EVENT_BASED:
            # Event-based obligations are materialised from recorded events, not
            # generated from a window.
            return []

    return []


_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _monthly(window_start: date, window_end: date) -> list[Period]:
    periods: list[Period] = []
    cursor = date(window_start.year, window_start.month, 1)
    ordinal = 0
    while cursor <= window_end:
        end = date(cursor.year, cursor.month, days_in_month(cursor.year, cursor.month))
        periods.append(
            Period(
                key=f"{cursor.year:04d}-{cursor.month:02d}",
                label=f"{_MONTH_NAMES[cursor.month - 1]} {cursor.year}",
                start=cursor,
                end=end,
                ordinal=ordinal,
            )
        )
        cursor = _add_months(cursor, 1)
        ordinal += 1
    return periods


def _blocked(
    window_start: date,
    window_end: date,
    fy: FiscalYearConvention,
    *,
    months: int,
    anchor: str,
    unit: str,
) -> list[Period]:
    """Quarters or half-years, anchored to either the fiscal or calendar year."""
    calendar_anchored = anchor == "CALENDAR" or fy.is_calendar_year
    start_month = 1 if calendar_anchored else fy.start_month
    start_day = 1 if calendar_anchored else fy.start_day

    # Step back to the block boundary at or before the window start.
    year_start, _, _ = (FiscalYearConvention(1, 1) if calendar_anchored else fy).year_of(
        window_start
    )
    del start_month, start_day

    periods: list[Period] = []
    block_start = year_start
    ordinal = 0
    per_year = 12 // months

    while block_start <= window_end:
        block_end = _add_months(block_start, months) - _ONE_DAY
        if block_end >= window_start:
            index = ordinal % per_year
            if calendar_anchored:
                key = f"{block_start.year:04d}-C{unit}{index + 1}"
                label = f"{_MONTH_NAMES[block_start.month - 1]}–{_MONTH_NAMES[block_end.month - 1]} {block_end.year}"
            else:
                _, _, fy_label = fy.year_of(block_start)
                compact = fy_label.replace(" ", "")
                key = f"{compact}-{unit}{index + 1}"
                label = (
                    f"{unit}{index + 1} {fy_label} "
                    f"({_MONTH_NAMES[block_start.month - 1]}–{_MONTH_NAMES[block_end.month - 1]} "
                    f"{block_end.year})"
                )
            periods.append(
                Period(key=key, label=label, start=block_start, end=block_end, ordinal=index)
            )
        block_start = _add_months(block_start, months)
        ordinal += 1

    return periods


def _annual(
    window_start: date, window_end: date, fy: FiscalYearConvention, *, anchor: str
) -> list[Period]:
    convention = FiscalYearConvention(1, 1, "{start_year}") if anchor == "CALENDAR" else fy

    periods: list[Period] = []
    start, end, label = convention.year_of(window_start)
    ordinal = 0
    while start <= window_end:
        if end >= window_start:
            periods.append(
                Period(
                    key=label.replace(" ", ""),
                    label=label,
                    start=start,
                    end=end,
                    ordinal=ordinal,
                )
            )
        start = _add_months(start, 12)
        end = _add_months(start, 12) - _ONE_DAY
        _, _, label = convention.year_of(start)
        ordinal += 1

    return periods


# ---------------------------------------------------------------------------
# Working days
# ---------------------------------------------------------------------------


def shift_to_working_day(
    day: date,
    *,
    rule: ShiftRule,
    calendars: CalendarSnapshot,
    country: str,
    calendar_keys: Sequence[str] = (),
    limit: int = 15,
) -> date:
    """Move a date off a weekend or holiday, if the rule says to.

    ``ShiftRule.NONE`` is the common case for statutory tax dates and returns the
    date untouched.
    """
    if rule is ShiftRule.NONE:
        return day

    step = timedelta(days=1 if rule is ShiftRule.NEXT_WORKING_DAY else -1)
    cursor = day
    for _ in range(limit):
        if calendars.is_working_day(cursor, country=country, calendars=calendar_keys):
            return cursor
        cursor += step
    # A fortnight of consecutive non-working days means the calendar data is
    # wrong. Returning the original beats looping or raising into a user's page.
    return day


# ---------------------------------------------------------------------------
# Due dates
# ---------------------------------------------------------------------------


def _apply_offset(anchor_date: date, offset: Mapping[str, Any]) -> date:
    """Apply a structured offset to an anchor date."""
    result = anchor_date

    if "years" in offset:
        result = _add_months(result, 12 * int(offset["years"]))
    if "months" in offset:
        result = _add_months(result, int(offset["months"]))
    if "day_of_month" in offset:
        result = set_day_of_month(result, int(offset["day_of_month"]))
    if "month" in offset:
        result = date(
            result.year,
            int(offset["month"]),
            min(result.day, days_in_month(result.year, int(offset["month"]))),
        )
        if "day" in offset:
            result = set_day_of_month(result, int(offset["day"]))
    if "days" in offset:
        result = result + timedelta(days=int(offset["days"]))

    return result


def resolve_due_date(
    definition: DefinitionSnapshot,
    period: Period,
    *,
    calendars: CalendarSnapshot,
    fy: FiscalYearConvention,
    extensions: ExtensionSet | None = None,
    events: Mapping[str, date] | None = None,
    scope_jurisdictions: frozenset[str] = frozenset(),
    previous_completion: date | None = None,
    licence_expiry: date | None = None,
) -> DueDateResolution:
    """Resolve when one period of one obligation is due.

    Extensions are an *input* to resolution rather than a mutation applied
    afterwards, which is what keeps the original statutory date available for the
    struck-through display and makes revoking an extension a simple re-run.
    """
    rule = definition.due_rule or {}
    anchor = str(rule.get("anchor", "PERIOD_END"))
    events = events or {}

    base: date | None
    blocking = ""

    match anchor:
        case "PERIOD_END":
            base = period.end
        case "PERIOD_START":
            base = period.start
        case "FY_END":
            base = fy.year_of(period.end)[1]
        case "FY_START":
            base = fy.year_of(period.end)[0]
        case "FIXED_DATE":
            fixed = rule.get("fixed", {})
            reference = fy.year_of(period.end)[1]
            base = date(
                reference.year,
                int(fixed.get("month", reference.month)),
                min(
                    int(fixed.get("day", 1)),
                    days_in_month(reference.year, int(fixed.get("month", reference.month))),
                ),
            )
        case "EVENT_DATE":
            key = str(rule.get("event_key", ""))
            base = events.get(key)
            if base is None:
                blocking = key
        case "LICENCE_EXPIRY":
            base = licence_expiry
            if base is None:
                blocking = "LICENCE_EXPIRY"
        case "PREVIOUS_INSTANCE_DATE":
            base = previous_completion
            if base is None:
                # A declared fallback covers the first occurrence — the first
                # board meeting of a newly incorporated company has no predecessor.
                fallback = rule.get("fallback")
                if fallback:
                    return resolve_due_date(
                        DefinitionSnapshot(**{**definition.__dict__, "due_rule": fallback}),
                        period,
                        calendars=calendars,
                        fy=fy,
                        extensions=extensions,
                        events=events,
                        scope_jurisdictions=scope_jurisdictions,
                    )
                blocking = "PREVIOUS_COMPLETION"
        case _:
            base = period.end

    if base is None:
        # Materialised anyway, with a prompt. A calendar that says what it does
        # not know is more useful than one that guesses.
        return DueDateResolution(effective_date=None, blocking_input=blocking)

    computed = _apply_offset(base, rule.get("offset", {}))

    shift = ShiftRule(str(rule.get("shift_if_holiday", ShiftRule.NONE)))
    calendar_keys = [str(key) for key in rule.get("calendars", [])]
    original = shift_to_working_day(
        computed,
        rule=shift,
        calendars=calendars,
        country=definition.country,
        calendar_keys=calendar_keys,
    )

    extension = (
        extensions.match(
            code=definition.code,
            period_key=period.key,
            jurisdictions=scope_jurisdictions,
        )
        if extensions
        else None
    )

    if extension and extension.kind in (ExtensionKind.EXTENSION, ExtensionKind.ADVANCEMENT):
        return DueDateResolution(
            effective_date=extension.new_due_date or original,
            original_date=original,
            applied_extension=extension,
            relief=extension.relief,
        )

    # A waiver relieves the penalty and leaves the date alone.
    return DueDateResolution(
        effective_date=original,
        original_date=original,
        applied_extension=extension,
        relief=extension.relief if extension else {},
    )


def static_max_lag_days(due_rule: Mapping[str, Any]) -> int:
    """An upper bound on how long after its period a filing can fall due.

    Used to widen the period-generation window so that annual returns due nine
    months after year end are not silently dropped from an eighteen-month
    horizon. Deliberately generous: over-generating costs a few discarded
    periods, under-generating loses obligations.
    """
    offset = due_rule.get("offset", {}) or {}
    days = int(offset.get("days", 0) or 0)
    months = int(offset.get("months", 0) or 0) + 12 * int(offset.get("years", 0) or 0)
    lag = abs(days) + abs(months) * 31

    if str(due_rule.get("anchor", "")) in {"FY_END", "FIXED_DATE"}:
        # These can sit a whole year past the period they report on.
        lag += 366

    return max(lag, 31)
