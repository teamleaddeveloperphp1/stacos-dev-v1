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
from datetime import date, timedelta
from typing import TypedDict
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
from django.utils.functional import Promise

from stacos.core.pagination import KeysetPage
from stacos.core.pagination import keyset_page as core_keyset_page
from stacos.engine.lifecycle import (
    CLOSED_STATES,
    DUE_SOON_DAYS,
    OPEN_STATES,
    DisplayStatus,
    State,
)
from stacos.obligations.models import ObligationInstance

#: A translated string is a ``Promise`` until something renders it, which is
#: what lets one process serve a user in English and another in Hindi.
StrOrPromise = str | Promise

__all__ = [
    "CategoryCount",
    "KeysetPage",
    "WeeklyWorkload",
    "annotate_status",
    "category_counts",
    "keyset_page",
    "live",
    "overdue_aging",
    "status_counts",
    "upcoming",
    "weekly_workload",
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
    """The working calendar: not archived, not superseded, not on an archived entity.

    Superseded rows are deliberately excluded here rather than deleted. They stay
    reachable from the audit trail and from the entity's history, and they stop
    cluttering the list somebody works from every morning.

    ``entity__archived_at`` matters just as much: archiving an entity stops new
    obligations being generated for it (see ``materialise_entity``), but the rows
    already on its calendar are untouched by that — without this filter they would
    keep showing up in every list, count and month grid forever.
    """
    base = queryset if queryset is not None else ObligationInstance.objects.all()
    return base.filter(
        archived_at__isnull=True,
        superseded_at__isnull=True,
        entity__archived_at__isnull=True,
    )


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
        # Independent of `due_soon` above — a wider, separately-filterable
        # window, not a replacement for the 7-day one used by `display_status`.
        due_30=Count(
            "id",
            filter=open_q & Q(due_date__gte=as_of) & Q(due_date__lte=as_of + timedelta(days=30)),
        ),
        completed=Count("id", filter=Q(state__in=_CLOSED)),
        # Only rows where the rule could not decide and nobody has decided for
        # it. An obligation somebody opted into by hand is confirmed the moment
        # it is added (`stacos.engine.planner`), so it never lands here.
        unconfirmed=Count("id", filter=open_q & Q(confirmed=False)),
        needs_input=Count("id", filter=open_q & ~Q(needs_input="")),
    )
    return {key: int(value or 0) for key, value in row.items()}


class CategoryCount(TypedDict):
    code: str
    label: StrOrPromise
    count: int


def category_counts(*, entity_ids: Sequence[UUID] | None = None) -> list[CategoryCount]:
    """Open obligations grouped by compliance category, busiest first.

    Open-only, deliberately: a lifetime count would only ever grow and stop
    telling anyone what their current workload looks like.
    """
    from stacos.tenancy.models import ComplianceCategory

    queryset = live().filter(state__in=_OPEN)
    if entity_ids is not None:
        queryset = queryset.filter(entity_id__in=list(entity_ids))

    rows = queryset.values("category").annotate(count=Count("id")).order_by("-count")
    return [
        {
            "code": row["category"],
            "label": ComplianceCategory(row["category"]).label,
            "count": row["count"],
        }
        for row in rows
    ]


class WeeklyWorkload(TypedDict):
    overdue: int
    weeks: list[int]


def weekly_workload(
    *, as_of: date, weeks: int = 6, entity_ids: Sequence[UUID] | None = None
) -> WeeklyWorkload:
    """Open obligations due in each of the next few weeks, one aggregate query.

    Overdue is folded in as its own figure rather than a negative week, so a
    caller does not have to special-case "week -1" — a backlog is a different
    kind of number from "due in nine days."
    """
    queryset = live().filter(state__in=_OPEN, due_date__isnull=False)
    if entity_ids is not None:
        queryset = queryset.filter(entity_id__in=list(entity_ids))

    aggregates = {"overdue": Count("id", filter=Q(due_date__lt=as_of))}
    for week in range(weeks):
        start = as_of + timedelta(days=week * 7)
        end = start + timedelta(days=7)
        aggregates[f"week_{week}"] = Count("id", filter=Q(due_date__gte=start, due_date__lt=end))

    row = queryset.aggregate(**aggregates)
    return {
        "overdue": int(row["overdue"] or 0),
        "weeks": [int(row[f"week_{week}"] or 0) for week in range(weeks)],
    }


def overdue_aging(*, as_of: date, entity_ids: Sequence[UUID] | None = None) -> dict[str, int]:
    """How late the overdue register is, bucketed rather than one flat number.

    A filing two days late and one six weeks late are not the same problem —
    a single "overdue" count on the dashboard tiles conflates them.
    """
    queryset = live().filter(state__in=_OPEN, due_date__isnull=False, due_date__lt=as_of)
    if entity_ids is not None:
        queryset = queryset.filter(entity_id__in=list(entity_ids))

    row = queryset.aggregate(
        recent=Count("id", filter=Q(due_date__gte=as_of - timedelta(days=7))),
        stale=Count(
            "id",
            filter=Q(due_date__lt=as_of - timedelta(days=7))
            & Q(due_date__gte=as_of - timedelta(days=30)),
        ),
        old=Count("id", filter=Q(due_date__lt=as_of - timedelta(days=30))),
    )
    return {key: int(value or 0) for key, value in row.items()}


# ---------------------------------------------------------------------------
# Keyset pagination
# ---------------------------------------------------------------------------


def keyset_page(
    queryset: QuerySet[ObligationInstance],
    *,
    cursor: str = "",
    page_size: int = 50,
) -> KeysetPage[ObligationInstance]:
    """Fetch one page after ``cursor``, ordered by ``(due_date, id)``.

    A thin wrapper over :func:`stacos.core.pagination.keyset_page`, kept for the
    two decisions specific to the register. It reads *forwards* through time,
    unlike every other paginated list in the product, which shows newest first.
    And nulls sort last: an obligation whose date could not be resolved belongs
    at the end of the list, not at the top pretending to be the most urgent thing
    a user owns.
    """
    return core_keyset_page(
        queryset,
        order_by="due_date",
        cursor=cursor,
        page_size=page_size,
        null_sentinel=date.max,
    )
