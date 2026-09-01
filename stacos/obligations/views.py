"""
The compliance calendar.

Every view here renders two ways — the full page on a direct GET, the fragment on
an HTMX request — which is what keeps deep links, the back button and "open in a
new tab" working while navigation still swaps only ``#main``.

Every view also declares a permission, including the fragment endpoints. An HTMX
fragment is a directly reachable URL; "it is only loaded from an authenticated
page" is how HTMX applications leak data.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.db.models import Count, Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.catalog.models import ComplianceDefinition, DefinitionVersion, PublicationStatus
from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.pagination import filters_querystring
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.engine.lifecycle import DUE_SOON_DAYS, OPEN_STATES, State
from stacos.obligations.forms import (
    STATUS_FILTERS,
    EntityEventForm,
    TransitionForm,
    obligation_display,
)
from stacos.obligations.models import EntityEvent, ObligationEvent, ObligationInstance
from stacos.obligations.queries import annotate_status, keyset_page, live, status_counts
from stacos.obligations.services import materialise
from stacos.obligations.transitions import (
    TransitionError,
    apply_transition,
    available_actions,
)
from stacos.tenancy.models import ComplianceCategory, Entity

PAGE_SIZE = 50

#: Rendered once rather than per request. Sorted so generated SQL is stable and
#: query-plan caching is not defeated by set iteration order.
_OPEN = sorted(str(state) for state in OPEN_STATES)


def _today() -> date:
    """The date every request is judged against.

    Local date rather than UTC: a client in Ahmedabad must not see a filing marked
    overdue because a server elsewhere has already ticked past midnight. Django's
    ``TIME_ZONE`` carries the jurisdiction, which comes from the pack.
    """
    return timezone.localdate()


def _permissions(request: HttpRequest) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@require_permission("compliance.obligation.view")
def calendar_list(request: HttpRequest) -> HttpResponse:
    """The register, filtered and keyset-paginated.

    Keyset rather than ``LIMIT/OFFSET``: a practice with three hundred clients
    reaches page four hundred within a month, and offset pagination makes
    PostgreSQL walk and discard every row before it.
    """
    as_of = _today()
    queryset = _filtered(request, as_of=as_of)
    page = keyset_page(queryset, cursor=request.GET.get("cursor", ""), page_size=PAGE_SIZE)

    context = {
        "obligations": page.rows,
        "page": page,
        "as_of": as_of,
        "counts": status_counts(as_of=as_of),
        "status_filters": STATUS_FILTERS,
        "status": request.GET.get("status", ""),
        "search": request.GET.get("q", ""),
        "category": request.GET.get("category", ""),
        "categories": ComplianceCategory.choices,
        "querystring": filters_querystring(request),
    }

    # A cursor request is asking for more rows, not for the whole screen again.
    if request.GET.get("cursor") and is_fragment_request(request):
        return render(request, "obligations/_fragments/calendar_rows.html", context)

    template = (
        "obligations/_fragments/calendar_body.html"
        if is_fragment_request(request)
        else "obligations/calendar.html"
    )
    return render(request, template, context)


def _filtered(request: HttpRequest, *, as_of: date) -> QuerySet[ObligationInstance]:
    """Apply the toolbar filters.

    ``select_related`` on entity and tenant is not optional here: without it a
    fifty-row page issues a hundred extra queries, and the query-count assertion
    in the test suite exists to keep it that way.
    """
    queryset = live().select_related("entity", "assigned_to")

    status = request.GET.get("status", "")
    if status == "overdue":
        queryset = queryset.filter(state__in=_OPEN, due_date__lt=as_of)
    elif status == "due_soon":
        queryset = queryset.filter(
            state__in=_OPEN,
            due_date__gte=as_of,
            due_date__lte=as_of + timedelta(days=DUE_SOON_DAYS),
        )
    elif status == "unconfirmed":
        queryset = queryset.filter(state__in=_OPEN, confirmed=False)
    elif status == "needs_input":
        queryset = queryset.filter(state__in=_OPEN).exclude(needs_input="")
    elif status == "completed":
        queryset = queryset.filter(state__in=[State.FILED, State.CLOSED])
    elif status != "all":
        queryset = queryset.filter(state__in=_OPEN)

    search = request.GET.get("q", "").strip()
    if search:
        queryset = queryset.filter(
            Q(title__icontains=search)
            | Q(definition_code__icontains=search)
            | Q(scope_label__icontains=search)
            | Q(entity__name__icontains=search)
        )

    category = request.GET.get("category", "").strip()
    if category:
        queryset = queryset.filter(category=category)

    entity_id = request.GET.get("entity", "").strip()
    if entity_id:
        queryset = queryset.filter(entity_id=entity_id)

    return annotate_status(queryset, as_of=as_of)


# ---------------------------------------------------------------------------
# Month grid
# ---------------------------------------------------------------------------


@require_permission("compliance.obligation.view")
def calendar_month(request: HttpRequest) -> HttpResponse:
    """A month at a glance.

    The month grid is a second read of the same data rather than a second source
    of truth: it runs one query for the window and groups in Python, because
    thirty-one days of obligations is a few hundred rows at most and a query per
    cell would be thirty-one round trips.
    """
    as_of = _today()
    anchor = _parse_month(request.GET.get("month", ""), fallback=as_of)

    first = anchor.replace(day=1)
    last = _month_end(first)

    rows = annotate_status(
        live().filter(due_date__gte=first, due_date__lte=last).select_related("entity"),
        as_of=as_of,
    ).order_by("due_date", "title")

    by_day: dict[date, list[ObligationInstance]] = {}
    for row in rows:
        if row.due_date is not None:
            by_day.setdefault(row.due_date, []).append(row)

    context = {
        "as_of": as_of,
        "month_start": first,
        "month_end": last,
        "weeks": _weeks(first, last, by_day),
        "previous_month": (first - timedelta(days=1)).replace(day=1),
        "next_month": (last + timedelta(days=1)),
        "total": sum(len(v) for v in by_day.values()),
    }

    template = (
        "obligations/_fragments/month_body.html"
        if is_fragment_request(request)
        else "obligations/month.html"
    )
    return render(request, template, context)


def _parse_month(raw: str, *, fallback: date) -> date:
    try:
        year, month = raw.split("-")
        return date(int(year), int(month), 1)
    except (ValueError, AttributeError):
        return fallback.replace(day=1)


def _month_end(first: date) -> date:
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


def _weeks(
    first: date,
    last: date,
    by_day: dict[date, list[ObligationInstance]],
) -> list[list[dict[str, Any]]]:
    """Lay the month out as calendar weeks, Monday first.

    Leading and trailing cells belong to adjacent months and are rendered muted
    rather than omitted — a grid with ragged edges is harder to read than one with
    greyed-out days.
    """
    start = first - timedelta(days=first.weekday())
    end = last + timedelta(days=6 - last.weekday())

    weeks: list[list[dict[str, Any]]] = []
    cursor = start
    while cursor <= end:
        week: list[dict[str, Any]] = []
        for _index in range(7):
            week.append(
                {
                    "date": cursor,
                    "in_month": first <= cursor <= last,
                    "obligations": by_day.get(cursor, []),
                }
            )
            cursor += timedelta(days=1)
        weeks.append(week)
    return weeks


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@require_permission("compliance.obligation.view")
def obligation_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """One obligation: why it exists, when it is due, and what to do next."""
    as_of = _today()
    obligation = _get(pk, as_of=as_of)

    definition = (
        DefinitionVersion.objects.select_related("definition", "definition__authority")
        .filter(
            definition__code=obligation.definition_code,
            version=obligation.definition_version,
        )
        .first()
    )

    context = {
        "obligation": obligation,
        "definition": definition,
        "as_of": as_of,
        "events": obligation.events.select_related("actor")[:50],
        "actions": available_actions(obligation, permissions=_permissions(request)),
        "form": TransitionForm(),
        "event_form": (
            EntityEventForm(initial={"key": obligation.needs_input})
            if obligation.needs_input
            else None
        ),
    }

    template = (
        "obligations/_fragments/detail_body.html"
        if is_fragment_request(request)
        else "obligations/detail.html"
    )
    return render(request, template, context)


def _get(pk: str, *, as_of: date) -> ObligationInstance:
    """Fetch through the scoped manager, 404 on anything out of reach.

    404 rather than 403: confirming that an obligation exists in another tenant is
    itself a disclosure, and the hostile-client test asserts exactly this.
    """
    obligation = (
        annotate_status(ObligationInstance.objects.all(), as_of=as_of)
        .select_related("entity", "assigned_to")
        .filter(pk=pk)
        .first()
    )
    if obligation is None:
        raise Http404
    return obligation


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@require_permission("compliance.obligation.view")
@require_http_methods(["POST"])
def obligation_transition(request: HttpRequest, pk: str) -> HttpResponse:
    """Move an obligation along, updating several regions in one response.

    The declared permission is only ``view``: which *transition* is allowed is
    decided by the transition table, per target state, inside
    :func:`~stacos.obligations.transitions.apply_transition`. Declaring every
    workflow permission here with ``any_of`` would pass the CI check and enforce
    nothing useful.
    """
    as_of = _today()
    obligation = _get(pk, as_of=as_of)
    form = TransitionForm(request.POST)

    if not form.is_valid():
        return _detail_error(request, obligation, _("That form could not be read."))

    try:
        result = apply_transition(
            obligation,
            target=form.cleaned_data["target"],
            actor=current_user(request),
            permissions=_permissions(request),
            note=form.cleaned_data.get("note", ""),
            filing_reference=form.cleaned_data.get("filing_reference", ""),
            filed_on=form.cleaned_data.get("filed_on"),
            as_of=as_of,
        )
    except TransitionError as exc:
        return _detail_error(
            request, obligation, str(exc), status=409 if exc.code == "stale" else 422
        )

    refreshed = _get(pk, as_of=as_of)
    counts = status_counts(as_of=as_of)

    return oob(
        request,
        Fragment(
            "obligations/_fragments/detail_panel.html",
            {
                "obligation": refreshed,
                "as_of": as_of,
                "actions": available_actions(refreshed, permissions=_permissions(request)),
                "form": TransitionForm(),
                "events": refreshed.events.select_related("actor")[:50],
            },
        ),
        also=[
            Fragment(
                "obligations/_fragments/status_counts.html",
                {"counts": counts},
                oob_target="calendar-counts",
            )
        ],
        toast=Toast(
            _("%(action)s — %(what)s")
            % {"action": result.transition.label, "what": obligation_display(refreshed)}
        ),
        triggers={"stacos:obligation-changed": {"id": str(refreshed.pk)}},
    )


def _detail_error(
    request: HttpRequest,
    obligation: ObligationInstance,
    message: str,
    *,
    status: int = 422,
) -> HttpResponse:
    """Re-render the action panel with the problem stated, keeping the page put."""
    return render(
        request,
        "obligations/_fragments/detail_panel.html",
        {
            "obligation": obligation,
            "as_of": _today(),
            "actions": available_actions(obligation, permissions=_permissions(request)),
            "form": TransitionForm(),
            "events": obligation.events.select_related("actor")[:50],
            "error": message,
        },
        status=status,
    )


@require_permission("compliance.event.record")
@require_http_methods(["POST"])
def record_entity_event(request: HttpRequest, pk: str) -> HttpResponse:
    """Answer a "we cannot schedule this until you tell us X" prompt.

    Rebuilds the entity's calendar immediately afterwards, because the whole
    reason the user typed a date is that something was waiting on it — asking them
    to come back tomorrow for the nightly run would be absurd.
    """
    as_of = _today()
    obligation = _get(pk, as_of=as_of)
    form = EntityEventForm(request.POST)

    if not form.is_valid():
        return render(
            request,
            "obligations/_fragments/needs_input.html",
            {"obligation": obligation, "event_form": form},
            status=422,
        )

    event: EntityEvent = form.save(commit=False)
    event.tenant = obligation.entity.tenant
    event.entity = obligation.entity
    event.recorded_by = current_user(request)
    event.save()

    materialise(obligation.entity, as_of=as_of, trigger="MANUAL", actor=current_user(request))

    refreshed = _get(pk, as_of=as_of)
    return oob(
        request,
        Fragment(
            "obligations/_fragments/detail_panel.html",
            {
                "obligation": refreshed,
                "as_of": as_of,
                "actions": available_actions(refreshed, permissions=_permissions(request)),
                "form": TransitionForm(),
                "events": refreshed.events.select_related("actor")[:50],
            },
        ),
        toast=Toast(_("Date recorded. The calendar has been rebuilt.")),
    )


@require_permission("compliance.calendar.rebuild")
@require_http_methods(["POST"])
def rebuild_calendar(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """Re-run materialisation for one entity, on demand.

    Safe by construction: the planner never destroys an obligation carrying
    history, and re-running against unchanged inputs produces an empty plan. The
    button exists because "I updated the profile, where are my filings" is the
    first thing a user asks during onboarding.
    """
    as_of = _today()
    entity = Entity.objects.filter(pk=entity_pk, archived_at__isnull=True).first()
    if entity is None:
        raise Http404

    run = materialise(entity, as_of=as_of, trigger="MANUAL", actor=current_user(request))

    return oob(
        request,
        Fragment(
            "obligations/_fragments/status_counts.html",
            {"counts": status_counts(as_of=as_of)},
        ),
        toast=Toast(
            _("Calendar rebuilt: %(summary)s.") % {"summary": run.summary()},
            level="success" if not run.needs_review else "warning",
        ),
        triggers={"stacos:calendar-rebuilt": {"summary": run.summary()}},
    )


# ---------------------------------------------------------------------------
# Dashboard fragment
# ---------------------------------------------------------------------------


@require_permission("catalog.view")
def definition_detail(request: HttpRequest, code: str) -> HttpResponse:
    """What the law says, behind an obligation.

    A client asking "why do I have to file this" deserves the statutory reference
    and a plain-language summary, not an assertion. Shown with the review date, and
    with a caveat when that review is stale.
    """
    definition = ComplianceDefinition.objects.filter(code=code).first()
    if definition is None:
        raise Http404

    version = (
        definition.versions.filter(status=PublicationStatus.PUBLISHED)
        .order_by("-effective_from")
        .first()
    )
    if version is None:
        raise Http404

    return render(
        request,
        "obligations/_fragments/definition_detail.html",
        {"definition": definition, "version": version},
    )


@require_permission("compliance.obligation.view")
def entity_summary(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """Per-category counts for one entity, for the entity detail page."""
    as_of = _today()
    entity = Entity.objects.filter(pk=entity_pk).first()
    if entity is None:
        raise Http404

    rows = (
        live()
        .filter(entity=entity, state__in=_OPEN)
        .values("category")
        .annotate(
            total=Count("id"),
            overdue=Count("id", filter=Q(due_date__lt=as_of)),
        )
        .order_by("category")
    )

    labels = dict(ComplianceCategory.choices)
    return render(
        request,
        "obligations/_fragments/entity_summary.html",
        {
            "entity": entity,
            "as_of": as_of,
            "rows": [
                {**row, "label": labels.get(row["category"], row["category"])} for row in rows
            ],
            "timeline": ObligationEvent.objects.filter(entity=entity).select_related("actor")[:10],
        },
    )
