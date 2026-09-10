"""
The practice's work board, time sheet and profitability view.

Everything here is scoped to the practice tenant, which is what keeps a client
from ever seeing an estimate, an assignment or a margin.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db.models import Count, DecimalField, F, Q, QuerySet, Sum
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.practice.forms import TimeEntryForm, WorkItemForm
from stacos.practice.models import OPEN_WORK_STATES, TimeEntry, WorkItem, WorkItemState

_OPEN = sorted(str(state) for state in OPEN_WORK_STATES)


def _permissions(request: HttpRequest) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


def _uuid_or_none(raw: str) -> UUID | None:
    """A filter from the query string, or nothing.

    A mangled id is a broken link, not an attack — and the scope would refuse it
    anyway. Ignoring it beats a 500 on a URL somebody pasted into chat.
    """
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        return None


@require_permission("practice.work.view")
def board(request: HttpRequest) -> HttpResponse:
    """The board, grouped by state.

    One query for the cards and grouping in Python. A query per column would be
    six round trips to draw one screen, and the whole board is a few hundred rows
    even for a large firm.
    """
    as_of = timezone.localdate()
    queryset: QuerySet[WorkItem] = (
        WorkItem.objects.filter(archived_at__isnull=True)
        .select_related("assigned_to", "client_tenant")
        .prefetch_related("time_entries")
    )

    if request.GET.get("mine"):
        queryset = queryset.filter(assigned_to=current_user(request))
    client_id = _uuid_or_none(request.GET.get("client", ""))
    if client_id is not None:
        queryset = queryset.filter(client_tenant_id=client_id)

    cards = list(queryset.filter(state__in=_OPEN).order_by("due_on", "-priority"))

    columns = [
        {
            "state": state,
            "label": WorkItemState(state).label,
            "cards": [card for card in cards if card.state == state],
        }
        for state in (
            WorkItemState.BACKLOG,
            WorkItemState.ASSIGNED,
            WorkItemState.IN_PROGRESS,
            WorkItemState.BLOCKED,
            WorkItemState.IN_REVIEW,
        )
    ]

    context = {
        "columns": columns,
        "as_of": as_of,
        "overdue": sum(1 for card in cards if card.is_overdue),
        "mine": bool(request.GET.get("mine")),
        "can_manage": "practice.work.manage" in _permissions(request),
    }
    template = (
        "practice/_fragments/board_body.html"
        if is_fragment_request(request)
        else "practice/board.html"
    )
    return render(request, template, context)


def _get(pk: str) -> WorkItem:
    item = (
        WorkItem.objects.filter(pk=pk, archived_at__isnull=True)
        .select_related("assigned_to", "reviewer", "client_tenant")
        .prefetch_related("time_entries__user")
        .first()
    )
    if item is None:
        raise Http404
    return item


@require_permission("practice.work.view")
def work_detail(request: HttpRequest, pk: str) -> HttpResponse:
    item = _get(pk)
    context = _detail_context(request, item)
    template = (
        "practice/_fragments/work_detail_body.html"
        if is_fragment_request(request)
        else "practice/work_detail.html"
    )
    return render(request, template, context)


def _detail_context(request: HttpRequest, item: WorkItem) -> dict[str, Any]:
    permissions = _permissions(request)
    entries = list(item.time_entries.all())
    if "practice.time.view_all" not in permissions:
        # Without the management permission a user sees only their own time.
        # Utilisation is a management view, not a peer-comparison tool.
        user_id = getattr(current_user(request), "pk", None)
        entries = [entry for entry in entries if entry.user_id == user_id]

    return {
        "item": item,
        "entries": entries,
        "hours_logged": item.hours_logged(),
        "wip": item.wip_value() if "practice.wip.view" in permissions else None,
        "time_form": TimeEntryForm(),
        "states": WorkItemState.choices,
        "can_manage": "practice.work.manage" in permissions,
        "can_log": "practice.time.log" in permissions,
    }


@require_permission("practice.work.manage")
@require_http_methods(["GET", "POST"])
def work_create(request: HttpRequest) -> HttpResponse:
    form = WorkItemForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        item = form.save(actor=current_user(request))
        return oob(
            request,
            "",
            toast=Toast(_("Work item created.")),
            triggers={
                "stacos:modal-close": True,
                "stacos:navigate": f"/app/practice/work/{item.pk}/",
            },
        )

    return render(
        request,
        "practice/_fragments/work_form_modal.html",
        {"form": form},
        status=422 if request.method == "POST" else 200,
    )


@require_permission("practice.work.manage")
@require_http_methods(["POST"])
def work_move(request: HttpRequest, pk: str) -> HttpResponse:
    """Move a card between columns."""
    item = _get(pk)
    target = request.POST.get("state", "")
    if target not in WorkItemState.values:
        return _panel(request, item, error=_("That is not a column."), status=422)

    item.state = target
    fields = ["state", "updated_at"]

    if target == WorkItemState.IN_PROGRESS and item.started_at is None:
        item.started_at = timezone.now()
        fields.append("started_at")
    if target == WorkItemState.DONE:
        item.completed_at = timezone.now()
        fields.append("completed_at")
    if target == WorkItemState.BLOCKED:
        item.blocked_reason = request.POST.get("reason", "")[:250]
        fields.append("blocked_reason")

    item.save(update_fields=fields)
    return _panel(request, _get(pk), toast=Toast(_("Moved.")))


@require_permission("practice.time.log")
@require_http_methods(["POST"])
def time_log(request: HttpRequest, pk: str) -> HttpResponse:
    """Log time against a work item, at the rate in force on the day worked."""
    item = _get(pk)
    form = TimeEntryForm(request.POST)
    if not form.is_valid():
        return _panel(request, item, error=_("Check the time entry."), status=422)

    form.save(work_item=item, actor=current_user(request))
    return _panel(request, _get(pk), toast=Toast(_("Time logged.")))


def _panel(
    request: HttpRequest,
    item: WorkItem,
    *,
    toast: Toast | None = None,
    error: str = "",
    status: int = 200,
) -> HttpResponse:
    context = _detail_context(request, item)
    context["error"] = error

    if status != 200:
        return render(request, "practice/_fragments/work_panel.html", context, status=status)

    return oob(
        request,
        Fragment("practice/_fragments/work_panel.html", context),
        toast=toast,
        triggers={"stacos:work-changed": {"id": str(item.pk)}},
    )


@require_permission("practice.wip.view")
def profitability(request: HttpRequest) -> HttpResponse:
    """What each client was worth, and what it cost.

    Computed from the time ledger rather than from a stored balance. Rates change
    and entries get corrected; a recomputed total is always right, and this is the
    number a partner bills from.
    """
    since = timezone.localdate() - timedelta(days=int(request.GET.get("days", 90) or 90))

    # `total_hours`, not `hours`. An annotation named after the column it
    # aggregates shadows that column for every later annotation in the same
    # `.annotate()` call, and `billable_hours` then resolves `Sum("hours")`
    # against the aggregate rather than the field: "Cannot compute Sum('hours'):
    # 'hours' is an aggregate". The page raised on every request.
    rows = (
        TimeEntry.objects.filter(worked_on__gte=since)
        .values("client_tenant__id", "client_tenant__name")
        .annotate(
            total_hours=Sum("hours"),
            billable_hours=Sum("hours", filter=Q(is_billable=True)),
            value=Sum(
                F("hours") * F("rate"),
                filter=Q(is_billable=True),
                output_field=DecimalField(max_digits=18, decimal_places=2),
            ),
            unbilled=Sum(
                F("hours") * F("rate"),
                filter=Q(is_billable=True, invoiced_at__isnull=True),
                output_field=DecimalField(max_digits=18, decimal_places=2),
            ),
            entries=Count("id"),
        )
        .order_by("-value")
    )

    clients = [
        {
            **row,
            "recovery": (
                (row["billable_hours"] or Decimal("0")) / row["total_hours"] * 100
                if row["total_hours"]
                else Decimal("0")
            ),
        }
        for row in rows
    ]

    context = {
        "clients": clients,
        "since": since,
        "days": request.GET.get("days", 90),
        "total_value": sum((row["value"] or Decimal("0")) for row in clients),
        "total_unbilled": sum((row["unbilled"] or Decimal("0")) for row in clients),
    }
    template = (
        "practice/_fragments/profitability_body.html"
        if is_fragment_request(request)
        else "practice/profitability.html"
    )
    return render(request, template, context)
