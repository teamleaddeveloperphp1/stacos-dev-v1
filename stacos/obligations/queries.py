"""
Reading the register: derived status in SQL, and pagination that stays fast.

Two things live here because both have exactly one correct implementation and
several tempting wrong ones.

**Overdue is derived, in the query.** Not a stored boolean stamped by a nightly
job — that is wrong for up to twenty-four hours and generates every "why does
this still say on track" support ticket. Not a PostgreSQL generated column
either: those need an ``IMMUTABLE`` expression and ``CURRENT_DATE`` is only
``STABLE``. So it is a ``Case`` annotation over a partial index, and
:func:`stacos.engine.lifecycle.is_overdue` is the same rule in Python. Two
implementations of one rule is the risk; ``tests/obligations/test_parity.py`` is
the mitigation, and any clause added to one has to be added to the other.

**Pagination is keyset, not ``LIMIT/OFFSET``.** Page 400 of an offset-paginated
list makes PostgreSQL walk and discard ten thousand rows. A practice with three
hundred clients reaches that within a month of using the product.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

from django.db.models import (
    BooleanField,
    Case,
    Count,
    DateField,
    F,
    Func,
    IntegerField,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from stacos.engine.lifecycle import (
    CLOSED_STATES,
    DUE_SOON_DAYS,
    OPEN_STATES,
    DisplayStatus,
    State,
)
from stacos.obligations.models import ObligationInstance

__all__ = [
    "KeysetPage",
    "annotate_status",
    "keyset_page",
    "live",
    "status_counts",
    "upcoming",
]

_OPEN = sorted(str(s) for s in OPEN_STATES)
_CLOSED = sorted(str(s) for s in CLOSED_STATES)


class DateDiffDays(Func):
    """``later - earlier``, in whole days, as an integer.

    Spelled out as a function rather than written as ``F("due_date") - Value(day)``
    because Django models the subtraction of two date expressions as a *duration*
    and installs an interval converter — while PostgreSQL's ``date - date`` returns
    a plain integer. The mismatch surfaces as ``int() argument must be ... not
    'datetime.timedelta'`` when the row is fetched, which is a long way from where
    the annotation was written.
    """

    template = "(%(expressions)s)"
    arg_joiner = " - "
    output_field = IntegerField()


def live(queryset: QuerySet[ObligationInstance] | None = None) -> QuerySet[ObligationInstance]:
    """The working calendar: not archived, not superseded.

    Superseded rows are deliberately excluded here rather than deleted. They stay
    reachable from the audit trail and from the entity's history, and they stop
    cluttering the list somebody works from every morning.
    """
    base = queryset if queryset is not None else ObligationInstance.objects.all()
    return base.filter(archived_at__isnull=True, superseded_at__isnull=True)


def annotate_status(
    queryset: QuerySet[ObligationInstance],
    *,
    as_of: date,
) -> QuerySet[ObligationInstance]:
    """Attach ``is_overdue``, ``days_to_due`` and ``display_status``.

    ``as_of`` is passed in rather than read from the clock so the annotation is
    deterministic under test and so a jurisdiction's local date — not the
    server's — decides what counts as late. A client in Ahmedabad must not see a
    filing marked overdue because a server in Frankfurt has already ticked over.

    The ordering inside ``display_status`` mirrors
    :func:`stacos.engine.lifecycle.derive_display_status` clause for clause. They
    are asserted equal across a fixture matrix; if you change one, change both.
    """
    overdue = Q(due_date__isnull=False) & Q(state__in=_OPEN) & Q(due_date__lt=as_of)
    due_soon_cutoff = as_of + timedelta(days=DUE_SOON_DAYS)

    return queryset.annotate(
        is_overdue=Case(
            When(overdue, then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ),
        # Signed: negative is late. Null-safe, because an unresolved due date is a
        # legitimate state and `None - date` would raise.
        days_to_due=Case(
            When(due_date__isnull=True, then=Value(None, output_field=IntegerField())),
            default=DateDiffDays(F("due_date"), Value(as_of, output_field=DateField())),
            output_field=IntegerField(),
        ),
        filed_late=Case(
            When(
                Q(filed_on__isnull=False)
                & Q(due_date__isnull=False)
                & Q(filed_on__gt=F("due_date")),
                then=Value(True),
            ),
            default=Value(False),
            output_field=BooleanField(),
        ),
        display_status=Case(
            When(state__in=[State.FILED, State.CLOSED], then=Value(DisplayStatus.COMPLETE)),
            When(state=State.NOT_APPLICABLE, then=Value(DisplayStatus.NOT_APPLICABLE)),
            When(state=State.DISPUTED, then=Value(DisplayStatus.DISPUTED)),
            When(overdue, then=Value(DisplayStatus.OVERDUE)),
            When(
                state__in=[State.INFO_REQUESTED, State.PENDING_CLIENT_APPROVAL, State.DEFERRED],
                then=Value(DisplayStatus.WAITING),
            ),
            When(
                Q(due_date__isnull=False)
                & Q(due_date__gte=as_of)
                & Q(due_date__lte=due_soon_cutoff),
                then=Value(DisplayStatus.DUE_SOON),
            ),
            When(
                state__in=[State.IN_PREPARATION, State.PENDING_REVIEW, State.READY_TO_FILE],
                then=Value(DisplayStatus.IN_PROGRESS),
            ),
            default=Value(DisplayStatus.ON_TRACK),
        ),
    )


def upcoming(
    *,
    as_of: date,
    within_days: int = 30,
    entity_ids: Sequence[UUID] | None = None,
) -> QuerySet[ObligationInstance]:
    """Open obligations falling due inside a window, soonest first.

    Includes everything already overdue, because a list of what is coming up that
    silently omits what you have already missed is worse than no list.
    """
    queryset = live().filter(state__in=_OPEN, due_date__lte=as_of + timedelta(days=within_days))
    if entity_ids is not None:
        queryset = queryset.filter(entity_id__in=list(entity_ids))
    return annotate_status(queryset, as_of=as_of).order_by("due_date", "title")


def status_counts(*, as_of: date, entity_ids: Sequence[UUID] | None = None) -> dict[str, int]:
    """Counts behind the dashboard tiles, in one query rather than five.

    Conditional aggregation instead of five ``.count()`` calls: the same table
    scan answers every tile, and the numbers cannot disagree with each other
    because they came from one snapshot.
    """
    queryset = live()
    if entity_ids is not None:
        queryset = queryset.filter(entity_id__in=list(entity_ids))

    open_q = Q(state__in=_OPEN)
    row = queryset.aggregate(
        total=Count("id"),
        open=Count("id", filter=open_q),
        overdue=Count("id", filter=open_q & Q(due_date__isnull=False) & Q(due_date__lt=as_of)),
        due_soon=Count(
            "id",
            filter=open_q
            & Q(due_date__gte=as_of)
            & Q(due_date__lte=as_of + timedelta(days=DUE_SOON_DAYS)),
        ),
        completed=Count("id", filter=Q(state__in=_CLOSED)),
        unconfirmed=Count("id", filter=open_q & Q(confirmed=False)),
        needs_input=Count("id", filter=open_q & ~Q(needs_input="")),
    )
    return {key: int(value or 0) for key, value in row.items()}


# ---------------------------------------------------------------------------
# Keyset pagination
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeysetPage:
    """One page, plus the cursor that fetches the next.

    No total count and no page numbers, deliberately. ``COUNT(*)`` over a filtered
    register is the expensive half of a paginated list, and "next" is what a user
    scanning a work queue actually wants.
    """

    rows: tuple[ObligationInstance, ...]
    next_cursor: str = ""
    has_more: bool = False


#: Sorting on (due_date, id) rather than due_date alone. Two obligations sharing
#: a date is the normal case — the 20th of the month carries several — and
#: without a tiebreaker a keyset cursor can skip or repeat rows across pages.
CURSOR_SEPARATOR = "~"


def keyset_page(
    queryset: QuerySet[ObligationInstance],
    *,
    cursor: str = "",
    page_size: int = 50,
) -> KeysetPage:
    """Fetch one page after ``cursor``, ordered by ``(due_date, id)``.

    Nulls sort last: an obligation whose date could not be resolved belongs at the
    end of the list, not at the top pretending to be the most urgent thing a user
    owns.
    """
    # Sorting on a coalesced key rather than on `due_date` directly, so undated
    # rows land at the end under exactly the same expression the cursor
    # comparison uses. Ordering and seeking must agree or pages overlap.
    sort_key = Coalesce("due_date", Value(date.max, output_field=DateField()))
    ordered = queryset.annotate(_sort_due=sort_key).order_by("_sort_due", "id")

    if cursor:
        after = _parse_cursor(cursor)
        if after is not None:
            after_due, after_id = after
            ordered = ordered.filter(
                Q(_sort_due__gt=after_due) | Q(_sort_due=after_due, id__gt=after_id)
            )

    rows = list(ordered[: page_size + 1])
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    next_cursor = ""
    if has_more and rows:
        last = rows[-1]
        next_cursor = f"{(last.due_date or date.max).isoformat()}{CURSOR_SEPARATOR}{last.id}"

    return KeysetPage(rows=tuple(rows), next_cursor=next_cursor, has_more=has_more)


def _parse_cursor(cursor: str) -> tuple[date, UUID] | None:
    """Decode a cursor, or ``None`` if it is unusable.

    A malformed cursor is a mangled URL — a link pasted into chat and broken by a
    trailing bracket — not an attack. Starting the list again beats a 500.
    """
    due_raw, _, id_raw = cursor.partition(CURSOR_SEPARATOR)
    try:
        return date.fromisoformat(due_raw), UUID(id_raw)
    except ValueError:
        return None
