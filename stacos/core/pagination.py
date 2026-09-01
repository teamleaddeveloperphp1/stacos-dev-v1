"""
Keyset pagination.

``LIMIT/OFFSET`` is the obvious way to paginate and the wrong one here. Page four
hundred of an offset-paginated register makes PostgreSQL walk and discard twenty
thousand rows before returning any, and a practice carrying three hundred clients
reaches page four hundred within a month. A keyset cursor seeks straight to the
row after the last one shown, so page four hundred costs what page one costs.

The alternative this replaces is worse than slow pagination: four lists in this
product simply stopped at a fixed number of rows — notifications at a hundred,
resolutions at a hundred, share transactions at fifty, payouts at twenty — with
no control to go further and nothing telling anyone rows had been left out. The
data was not slow to reach. It was unreachable.

Deliberately no total count and no page numbers. ``COUNT(*)`` over a filtered
list is the expensive half of paginating it, and "next" is what somebody
scanning a work queue actually wants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from django.db import models
from django.db.models import Q, QuerySet, Value
from django.db.models.functions import Coalesce
from django.http import HttpRequest

__all__ = ["CURSOR_SEPARATOR", "KeysetPage", "filters_querystring", "keyset_page"]

#: Separates the sort value from the tiebreaker in a cursor. Not a character that
#: appears in an ISO date or a UUID, and one that survives a URL round trip.
CURSOR_SEPARATOR = "~"


@dataclass(frozen=True, slots=True)
class KeysetPage[M: models.Model]:
    """One page, plus the cursor that fetches the next."""

    rows: tuple[M, ...]
    next_cursor: str = ""
    has_more: bool = False


def keyset_page[M: models.Model](
    queryset: QuerySet[M],
    *,
    order_by: str,
    cursor: str = "",
    page_size: int = 50,
    descending: bool = False,
    null_sentinel: date | datetime | None = None,
) -> KeysetPage[M]:
    """Fetch one page after ``cursor``, ordered by ``(order_by, id)``.

    :param order_by: a date or datetime field on the model.
    :param descending: newest first. Most lists in this product want this; the
        compliance calendar, which reads forwards through time, does not.
    :param null_sentinel: required when ``order_by`` is nullable — the value a
        null sorts as. Where an undated row belongs is a product decision, not a
        default: on the calendar an obligation whose date could not be resolved
        belongs at the end, not at the top pretending to be the most urgent thing
        the user owns.

    The tiebreaker is not optional. Two rows sharing a sort value is the normal
    case — the 20th of the month carries several filings — and without one a
    cursor skips or repeats rows across pages.
    """
    field = queryset.model._meta.get_field(order_by)
    if not isinstance(field, models.DateField):  # DateTimeField subclasses this
        raise TypeError(
            f"keyset_page orders on a date or datetime field; "
            f"{queryset.model.__name__}.{order_by} is a {type(field).__name__}."
        )
    if field.null and null_sentinel is None:
        raise ValueError(
            f"{queryset.model.__name__}.{order_by} is nullable, so keyset_page needs "
            f"`null_sentinel` to say where a null sorts. Ordering and seeking must "
            f"agree on that or pages overlap."
        )

    sort_key = order_by
    sorted_queryset: QuerySet[Any] = queryset
    if null_sentinel is not None:
        # Sorting on a coalesced key rather than the column directly, so null
        # rows land under exactly the same expression the cursor comparison uses.
        sorted_queryset = queryset.annotate(
            _keyset=Coalesce(order_by, Value(null_sentinel, output_field=type(field)()))
        )
        sort_key = "_keyset"

    prefix = "-" if descending else ""
    ordered = sorted_queryset.order_by(f"{prefix}{sort_key}", f"{prefix}id")

    if cursor:
        after = _parse_cursor(cursor, wants_datetime=isinstance(field, models.DateTimeField))
        if after is not None:
            value, row_id = after
            beyond = "lt" if descending else "gt"
            ordered = ordered.filter(
                Q(**{f"{sort_key}__{beyond}": value})
                | Q(**{sort_key: value, f"id__{beyond}": row_id})
            )

    # One more than asked for, so "is there another page?" costs no extra query.
    rows = list(ordered[: page_size + 1])
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    next_cursor = ""
    if has_more and rows:
        last = rows[-1]
        sort_value: date | datetime | None = getattr(last, order_by, None) or null_sentinel
        if sort_value is not None:
            next_cursor = f"{sort_value.isoformat()}{CURSOR_SEPARATOR}{last.pk}"

    return KeysetPage(rows=tuple(rows), next_cursor=next_cursor, has_more=has_more)


def _parse_cursor(cursor: str, *, wants_datetime: bool) -> tuple[Any, UUID] | None:
    """Decode a cursor, or ``None`` if it is unusable.

    A malformed cursor is a mangled URL — a link pasted into chat and broken by a
    trailing bracket — not an attack. Starting the list again beats a 500.
    """
    value_raw, _, id_raw = cursor.partition(CURSOR_SEPARATOR)
    try:
        value = (
            datetime.fromisoformat(value_raw) if wants_datetime else date.fromisoformat(value_raw)
        )
        return value, UUID(id_raw)
    except ValueError:
        return None


def filters_querystring(request: HttpRequest) -> str:
    """The current filters, minus the cursor, ready to append to a "load more" link.

    Without this the second page quietly drops whatever the user had filtered to
    and starts returning rows they did not ask for — which looks like the filter
    breaking rather than the pagination.
    """
    params = request.GET.copy()
    params.pop("cursor", None)
    encoded = params.urlencode()
    return f"&{encoded}" if encoded else ""
