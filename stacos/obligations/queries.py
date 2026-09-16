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
    "PenaltyExposure",
    "WeeklyWorkload",
    "annotate_status",
    "apply_text_filters",
    "category_counts",
    "keyset_page",
    "live",
    "overdue_aging",
    "overdue_penalty_exposure",
    "related_scope_instances",
    "scaled_bars",
    "sibling_instances",
    "status_counts",
    "upcoming",
    "weekly_workload",
]


def scaled_bars(rows: Sequence[tuple[StrOrPromise, int, str]]) -> list[dict[str, object]]:
    """Bar-list rows scaled to the busiest one, not stacked to a 100% total.

    Shared by every ``<c-bar-list>`` caller — the tenancy dashboard's
    category/weekly-workload/overdue-ageing panels, and the calendar's own
    workload forecast — so "scale to the busiest row" lives in one place
    rather than being reimplemented per panel.
    """
    busiest = max((count for _, count, _ in rows), default=0) or 1
    return [
        {"label": label, "count": count, "pct": round(count / busiest * 100, 1), "color": color}
        for label, count, color in rows
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
    """Attach ``is_overdue``, ``days_to_due``, ``display_status`` and ``priority_rank``.

    ``as_of`` is passed in rather than read from the clock so the annotation is
    deterministic under test and so a jurisdiction's local date — not the
    server's — decides what counts as late. A client in Ahmedabad must not see a
    filing marked overdue because a server in Frankfurt has already ticked over.

    The ordering inside ``display_status`` mirrors
    :func:`stacos.engine.lifecycle.derive_display_status` clause for clause. They
    are asserted equal across a fixture matrix; if you change one, change both.

    ``priority_rank`` mirrors the same clauses a third time, as an integer, purely
    for ordering the register: lower is more urgent. It duplicates rather than
    derives from ``display_status`` because Django cannot reference one
    annotation from inside the ``Case`` that builds a sibling in the same
    ``.annotate()`` call. A row not yet confirmed sorts ahead of an otherwise
    identical one that is, within the same bucket — the register should surface
    what needs a decision before what has already had one.
    """
    overdue = Q(due_date__isnull=False) & Q(state__in=_OPEN) & Q(due_date__lt=as_of)
    due_soon_cutoff = as_of + timedelta(days=DUE_SOON_DAYS)
    due_soon = Q(due_date__isnull=False) & Q(due_date__gte=as_of) & Q(due_date__lte=due_soon_cutoff)

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
            When(due_soon, then=Value(DisplayStatus.DUE_SOON)),
            When(
                state__in=[State.IN_PREPARATION, State.PENDING_REVIEW, State.READY_TO_FILE],
                then=Value(DisplayStatus.IN_PROGRESS),
            ),
            When(
                Q(state=State.NOT_STARTED) & ~Q(pending_reason=""),
                then=Value(DisplayStatus.PENDING),
            ),
            When(state=State.NOT_STARTED, then=Value(DisplayStatus.NOT_STARTED)),
            default=Value(DisplayStatus.ON_TRACK),
        ),
        priority_rank=(
            Case(
                When(state__in=[State.FILED, State.CLOSED], then=Value(16)),
                When(state=State.NOT_APPLICABLE, then=Value(18)),
                When(state=State.DISPUTED, then=Value(12)),
                When(overdue, then=Value(0)),
                When(
                    state__in=[
                        State.INFO_REQUESTED,
                        State.PENDING_CLIENT_APPROVAL,
                        State.DEFERRED,
                    ],
                    then=Value(10),
                ),
                When(due_soon, then=Value(2)),
                When(
                    state__in=[State.IN_PREPARATION, State.PENDING_REVIEW, State.READY_TO_FILE],
                    then=Value(8),
                ),
                When(
                    Q(state=State.NOT_STARTED) & ~Q(pending_reason=""),
                    then=Value(6),
                ),
                When(state=State.NOT_STARTED, then=Value(4)),
                default=Value(14),
                output_field=IntegerField(),
            )
            + Case(
                When(confirmed=False, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
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


def apply_text_filters(
    queryset: QuerySet[ObligationInstance],
    *,
    category: str = "",
    search: str = "",
) -> QuerySet[ObligationInstance]:
    """The category/search narrowing shared by the register and the tiles above it.

    Kept in one place so the list and its counts can never disagree about what
    a typed search term or a chosen category matches — five conditional counts
    built on one queryset, and a list built on another with the same two lines
    copied by hand, is exactly how a tile ends up promising a number the list
    it links to does not deliver.
    """
    if category:
        queryset = queryset.filter(category=category)
    if search:
        queryset = queryset.filter(
            Q(title__icontains=search)
            | Q(definition_code__icontains=search)
            | Q(scope_label__icontains=search)
            | Q(entity__name__icontains=search)
        )
    return queryset


def status_counts(
    *,
    as_of: date,
    entity_ids: Sequence[UUID] | None = None,
    category: str = "",
    search: str = "",
) -> dict[str, int]:
    """Counts behind the calendar's tiles, in one query rather than a dozen.

    Conditional aggregation instead of separate ``.count()`` calls: the same
    table scan answers every tile, and the numbers cannot disagree with each
    other because they came from one snapshot.

    ``category``/``search`` narrow the same way the register's own toolbar
    does — see :func:`apply_text_filters` — so that typing "gst" into the
    search box updates the tiles to match what is actually on screen, not the
    tenant's entire backlog. ``entity_ids`` behaves the same way it always has.
    Deliberately *not* narrowed by the register's own ``status`` selection:
    these tiles are the other buckets a click could switch to, and a tile that
    only ever counted the bucket already showing would be pointless.
    """
    queryset = live()
    if entity_ids is not None:
        queryset = queryset.filter(entity_id__in=list(entity_ids))
    queryset = apply_text_filters(queryset, category=category, search=search)

    open_q = Q(state__in=_OPEN)
    row = queryset.aggregate(
        total=Count("id"),
        open=Count("id", filter=open_q),
        overdue=Count("id", filter=open_q & Q(due_date__isnull=False) & Q(due_date__lt=as_of)),
        due_today=Count("id", filter=open_q & Q(due_date=as_of)),
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
        due_90=Count(
            "id",
            filter=open_q & Q(due_date__gte=as_of) & Q(due_date__lte=as_of + timedelta(days=90)),
        ),
        # The calendar's default landing scope: overdue, of any age, plus
        # everything else due within 90 days — unlike `due_30`/`due_90` above,
        # not lower-bounded at `as_of`, so a backlog is never silently dropped
        # from the view a user lands on first.
        latest=Count(
            "id",
            filter=open_q & Q(due_date__isnull=False) & Q(due_date__lte=as_of + timedelta(days=90)),
        ),
        completed=Count("id", filter=Q(state__in=_CLOSED)),
        # Only rows where the rule could not decide and nobody has decided for
        # it. An obligation somebody opted into by hand is confirmed the moment
        # it is added (`stacos.engine.planner`), so it never lands here.
        unconfirmed=Count("id", filter=open_q & Q(confirmed=False)),
        needs_input=Count("id", filter=open_q & ~Q(needs_input="")),
    )
    counts = {key: int(value or 0) for key, value in row.items()}
    # Open, but neither overdue nor due soon. `overdue` and `due_soon` are
    # disjoint subsets of `open` (split on `due_date` vs. `as_of`), so this
    # can never go negative.
    counts["pending"] = counts["open"] - counts["overdue"] - counts["due_soon"]
    return counts


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
    *,
    as_of: date,
    weeks: int = 6,
    entity_ids: Sequence[UUID] | None = None,
    category: str = "",
    search: str = "",
) -> WeeklyWorkload:
    """Open obligations due in each of the next few weeks, one aggregate query.

    Overdue is folded in as its own figure rather than a negative week, so a
    caller does not have to special-case "week -1" — a backlog is a different
    kind of number from "due in nine days."

    ``category``/``search`` narrow the same way ``status_counts`` does — see
    :func:`apply_text_filters` — so the calendar's own toolbar can drive this
    without a second filtering path.
    """
    queryset = live().filter(state__in=_OPEN, due_date__isnull=False)
    if entity_ids is not None:
        queryset = queryset.filter(entity_id__in=list(entity_ids))
    queryset = apply_text_filters(queryset, category=category, search=search)

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


def overdue_aging(
    *,
    as_of: date,
    entity_ids: Sequence[UUID] | None = None,
    category: str = "",
    search: str = "",
) -> dict[str, int]:
    """How late the overdue register is, bucketed rather than one flat number.

    A filing two days late and one six weeks late are not the same problem —
    a single "overdue" count on the dashboard tiles conflates them.

    ``category``/``search`` narrow the same way ``status_counts`` does — see
    :func:`apply_text_filters`.
    """
    queryset = live().filter(state__in=_OPEN, due_date__isnull=False, due_date__lt=as_of)
    if entity_ids is not None:
        queryset = queryset.filter(entity_id__in=list(entity_ids))
    queryset = apply_text_filters(queryset, category=category, search=search)

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


class PenaltyExposure(TypedDict):
    total_minor: int
    partial: bool


def overdue_penalty_exposure(
    *,
    as_of: date,
    entity_ids: Sequence[UUID] | None = None,
    category: str = "",
    search: str = "",
) -> PenaltyExposure:
    """Aggregate potential penalty across the overdue register.

    Not a second penalty formula: this calls the same
    :func:`stacos.engine.penalty.compute_penalties` the detail page already
    calls for one obligation at a time, over every overdue row's own
    ``days_late`` (:func:`stacos.engine.lifecycle.days_late`) — just batched,
    with the catalog lookup done once per distinct ``(definition_code,
    definition_version)`` pair rather than once per row.

    ``total_minor`` only sums rules ``compute_penalties`` could fully price
    (``computed_minor`` is not ``None``). ``partial`` is ``True`` when at
    least one overdue row carries a rule that could not be totalled (an
    uncapped per-day rate, or a fixed statutory range) — the caller should
    read the total as a floor, not the whole exposure, in that case.
    """
    from stacos.catalog.models import DefinitionVersion
    from stacos.engine.lifecycle import days_late
    from stacos.engine.penalty import compute_penalties

    queryset = live().filter(state__in=_OPEN, due_date__isnull=False, due_date__lt=as_of)
    if entity_ids is not None:
        queryset = queryset.filter(entity_id__in=list(entity_ids))
    queryset = apply_text_filters(queryset, category=category, search=search)

    rows = list(queryset.values("due_date", "filed_on", "definition_code", "definition_version"))
    if not rows:
        return {"total_minor": 0, "partial": False}

    pairs = {(row["definition_code"], row["definition_version"]) for row in rows}
    codes = {code for code, _version in pairs}
    rules_by_pair: dict[tuple[str, int], list[dict[str, object]]] = {}
    for definition_version in (
        DefinitionVersion.objects.filter(definition__code__in=codes)
        .exclude(penalty_rules=[])
        .values("definition__code", "version", "penalty_rules")
    ):
        key = (definition_version["definition__code"], definition_version["version"])
        if key in pairs:
            rules_by_pair[key] = definition_version["penalty_rules"]

    total_minor = 0
    partial = False
    for row in rows:
        rules = rules_by_pair.get((row["definition_code"], row["definition_version"]))
        if not rules:
            continue
        late = days_late(due_date=row["due_date"], filed_on=row["filed_on"], as_of=as_of)
        for penalty in compute_penalties(rules, days_late=late):
            if penalty.computed_minor is None:
                partial = True
            else:
                total_minor += penalty.computed_minor

    return {"total_minor": total_minor, "partial": partial}


# ---------------------------------------------------------------------------
# Keyset pagination
# ---------------------------------------------------------------------------


def keyset_page(
    queryset: QuerySet[ObligationInstance],
    *,
    cursor: str = "",
    page_size: int = 50,
) -> KeysetPage[ObligationInstance]:
    """Fetch one page after ``cursor``, ordered by ``(due_date, priority_rank, id)``.

    A thin wrapper over :func:`stacos.core.pagination.keyset_page`, kept for the
    decisions specific to the register. It reads *forwards* through time, unlike
    every other paginated list in the product, which shows newest first. Nulls
    sort last: an obligation whose date could not be resolved belongs at the end
    of the list, not at the top pretending to be the most urgent thing a user
    owns. And within a shared due date, ``priority_rank`` (from
    :func:`annotate_status`, which every caller of this function has already
    run) puts what still needs a decision ahead of what has already had one —
    the register should read as a work queue, not an alphabetised table.
    """
    return core_keyset_page(
        queryset,
        order_by="due_date",
        cursor=cursor,
        page_size=page_size,
        null_sentinel=date.max,
        secondary="priority_rank",
    )


# ---------------------------------------------------------------------------
# Neighbours, for the detail page
# ---------------------------------------------------------------------------


def sibling_instances(
    obligation: ObligationInstance, *, as_of: date
) -> dict[str, ObligationInstance | None]:
    """The occurrence immediately before and after this one, in its own sequence.

    "Sequence" means the same entity, the same rule and the same registration or
    site — GSTR-3B for one GSTIN in March is adjacent to GSTR-3B for *that GSTIN*
    in February and April, never to GSTR-3B for a different GSTIN filed the same
    month. ``period_key`` is the right ordering key rather than ``due_date``: it
    sorts chronologically as a string by construction
    (:class:`stacos.engine.types.Period`) and, unlike ``due_date``, is never null.
    """
    if not obligation.period_key:
        return {"previous": None, "next": None}
    siblings = live().filter(
        entity_id=obligation.entity_id,
        definition_code=obligation.definition_code,
        scope_ref=obligation.scope_ref,
    ).exclude(pk=obligation.pk)
    previous = (
        annotate_status(siblings.filter(period_key__lt=obligation.period_key), as_of=as_of)
        .order_by("-period_key")
        .first()
    )
    upcoming_sibling = (
        annotate_status(siblings.filter(period_key__gt=obligation.period_key), as_of=as_of)
        .order_by("period_key")
        .first()
    )
    return {"previous": previous, "next": upcoming_sibling}


def related_scope_instances(
    obligation: ObligationInstance, *, as_of: date
) -> QuerySet[ObligationInstance]:
    """Other filings the same rule produced for the same period, at other scopes.

    Empty for an ``ENTITY``-scoped obligation, which is unique per period by the
    identity constraint. For a ``REGISTRATION``- or ``PREMISES``-scoped one — GST
    filed per GSTIN, a licence renewed per plant — this is the rest of that same
    month's filing, so an entity with six GSTINs sees all six from any one of them
    rather than having to know to look.
    """
    if not obligation.scope_ref:
        return ObligationInstance.objects.none()
    siblings = (
        live()
        .filter(
            entity_id=obligation.entity_id,
            definition_code=obligation.definition_code,
            period_key=obligation.period_key,
        )
        .exclude(pk=obligation.pk)
    )
    return annotate_status(siblings, as_of=as_of).order_by("scope_label")
