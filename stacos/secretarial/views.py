"""
The secretarial screens: meetings, resolutions, registers and the cap table.

The cap table is behind its own permission and its own view. A compliance
manager who legitimately keeps the minute book has no business seeing who owns
what.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet, Sum
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.secretarial.forms import AttendeeForm, MeetingForm, MinutesForm
from stacos.secretarial.models import (
    Meeting,
    Resolution,
    Shareholder,
    ShareTransaction,
    StatutoryRegister,
)
from stacos.secretarial.services import (
    MeetingError,
    board_meeting_gaps,
    mark_held,
    next_board_meeting_due,
    pending_mgt14,
    record_attendance,
)
from stacos.tenancy.models import Entity
from stacos.vault.models import LinkTarget
from stacos.vault.services import documents_for


def _permissions(request: HttpRequest) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


def _filtered(request: HttpRequest) -> QuerySet[Meeting]:
    queryset = Meeting.objects.filter(archived_at__isnull=True).select_related("entity")

    state = request.GET.get("state", "")
    if state == "upcoming":
        queryset = queryset.filter(state__in=[Meeting.State.PLANNED, Meeting.State.NOTICE_ISSUED])
    elif state == "unminuted":
        # Held but never written up. The gap that turns into a diligence finding.
        queryset = queryset.filter(state=Meeting.State.HELD)
    elif state and state != "all":
        queryset = queryset.filter(state=state)

    kind = request.GET.get("kind", "")
    if kind:
        queryset = queryset.filter(kind=kind)

    entity_id = request.GET.get("entity", "")
    if entity_id:
        queryset = queryset.filter(entity_id=entity_id)

    return queryset.order_by("-scheduled_for")


@require_permission("secretarial.view")
def meeting_list(request: HttpRequest) -> HttpResponse:
    rows = list(_filtered(request)[:100])
    context = {
        "meetings": rows,
        "kinds": Meeting.Kind.choices,
        "states": Meeting.State.choices,
        "state": request.GET.get("state", ""),
        "kind": request.GET.get("kind", ""),
        "as_of": timezone.localdate(),
    }
    template = (
        "secretarial/_fragments/meeting_list_body.html"
        if is_fragment_request(request)
        else "secretarial/meeting_list.html"
    )
    return render(request, template, context)


def _get(pk: str) -> Meeting:
    meeting = (
        Meeting.objects.filter(pk=pk, archived_at__isnull=True)
        .select_related("entity")
        .prefetch_related("attendees", "resolutions")
        .first()
    )
    if meeting is None:
        raise Http404
    return meeting


def _detail_context(request: HttpRequest, meeting: Meeting) -> dict[str, Any]:
    permissions = _permissions(request)
    return {
        "meeting": meeting,
        "attendees": list(meeting.attendees.all()),
        "resolutions": list(meeting.resolutions.all()),
        "documents": documents_for(target_type=LinkTarget.MEETING, target_id=meeting.pk),
        "attendee_form": AttendeeForm(),
        "minutes_form": MinutesForm(),
        "can_manage": "secretarial.meeting.manage" in permissions,
        "can_sign": "secretarial.minutes.sign" in permissions,
    }


@require_permission("secretarial.view")
def meeting_detail(request: HttpRequest, pk: str) -> HttpResponse:
    meeting = _get(pk)
    context = _detail_context(request, meeting)
    template = (
        "secretarial/_fragments/meeting_detail_body.html"
        if is_fragment_request(request)
        else "secretarial/meeting_detail.html"
    )
    return render(request, template, context)


@require_permission("secretarial.meeting.manage")
@require_http_methods(["GET", "POST"])
def meeting_create(request: HttpRequest) -> HttpResponse:
    form = MeetingForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        meeting = form.save(actor=current_user(request))
        return oob(
            request,
            "",
            toast=Toast(_("Meeting scheduled.")),
            triggers={
                "stacos:modal-close": True,
                "stacos:navigate": f"/app/secretarial/meetings/{meeting.pk}/",
            },
        )

    return render(
        request,
        "secretarial/_fragments/meeting_form_modal.html",
        {"form": form},
        status=422 if request.method == "POST" else 200,
    )


@require_permission("secretarial.meeting.manage")
@require_http_methods(["POST"])
def attendee_add(request: HttpRequest, pk: str) -> HttpResponse:
    meeting = _get(pk)
    form = AttendeeForm(request.POST)
    if not form.is_valid():
        return _panel(request, meeting, error=_("Check the attendee details."), status=422)

    record_attendance(
        meeting,
        name=form.cleaned_data["name"],
        role=form.cleaned_data["role"],
        attendance=form.cleaned_data["attendance"],
        din=form.cleaned_data.get("din", ""),
        is_interested=form.cleaned_data.get("is_interested", False),
    )
    return _panel(request, _get(pk), toast=Toast(_("Attendance recorded.")))


@require_permission("secretarial.meeting.manage")
@require_http_methods(["POST"])
def meeting_hold(request: HttpRequest, pk: str) -> HttpResponse:
    """Mark a meeting held — which is what unblocks the ROC filings."""
    meeting = _get(pk)
    form = MinutesForm(request.POST)
    if not form.is_valid():
        return _panel(request, meeting, error=_("Enter the date it was held."), status=422)

    try:
        mark_held(
            meeting,
            held_on=form.cleaned_data["held_on"],
            actor=current_user(request),
        )
    except MeetingError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    message = _("Recorded as held.")
    if meeting.event_key:
        message = _("Recorded as held. The compliance calendar has been rebuilt.")

    return _panel(request, _get(pk), toast=Toast(message))


def _panel(
    request: HttpRequest,
    meeting: Meeting,
    *,
    toast: Toast | None = None,
    error: str = "",
    status: int = 200,
) -> HttpResponse:
    context = _detail_context(request, meeting)
    context["error"] = error

    if status != 200:
        return render(request, "secretarial/_fragments/meeting_panel.html", context, status=status)

    return oob(
        request,
        Fragment("secretarial/_fragments/meeting_panel.html", context),
        toast=toast,
        triggers={"stacos:meeting-changed": {"id": str(meeting.pk)}},
    )


# ---------------------------------------------------------------------------
# Registers and compliance health
# ---------------------------------------------------------------------------


@require_permission("secretarial.view")
def entity_secretarial(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """One entity's secretarial position: registers, gaps and pending filings."""
    entity = Entity.objects.filter(pk=entity_pk).first()
    if entity is None:
        raise Http404

    as_of = timezone.localdate()
    return render(
        request,
        "secretarial/_fragments/entity_summary.html",
        {
            "entity": entity,
            "registers": list(StatutoryRegister.objects.filter(entity=entity).order_by("kind")),
            # Computed from meetings actually held, never projected forward.
            "gaps": board_meeting_gaps(entity.pk, as_of=as_of),
            "next_board_meeting_due": next_board_meeting_due(entity.pk),
            "pending_mgt14": pending_mgt14(entity.pk),
            "as_of": as_of,
        },
    )


@require_permission("secretarial.resolution.manage")
def resolution_list(request: HttpRequest) -> HttpResponse:
    """Resolutions, with the ones needing an MGT-14 filing surfaced first."""
    rows = (
        Resolution.objects.select_related("entity", "meeting")
        .filter(Q(requires_mgt14=True) | Q(passed_on__isnull=False))
        .order_by("-passed_on")[:100]
    )
    return render(
        request,
        "secretarial/_fragments/resolution_list.html",
        {"resolutions": list(rows), "as_of": timezone.localdate()},
    )


# ---------------------------------------------------------------------------
# Cap table
# ---------------------------------------------------------------------------


@require_permission("secretarial.captable.view")
def cap_table(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """Holdings, derived by replaying the ledger.

    Totalled from the transactions on every render rather than read from a
    stored balance. A stored balance and a transaction history disagree
    eventually, and when they do nobody can tell which is right.
    """
    entity = Entity.objects.filter(pk=entity_pk).first()
    if entity is None:
        raise Http404

    holdings = (
        Shareholder.objects.filter(entity=entity)
        .annotate(shares=Sum("transactions__quantity"))
        .order_by("-shares", "name")
    )
    rows = [row for row in holdings if (row.shares or 0) > 0]
    total = sum(row.shares or 0 for row in rows)

    context = {
        "entity": entity,
        "holdings": [
            {
                "shareholder": row,
                "shares": row.shares or 0,
                # Percentages computed here rather than stored, for the same
                # reason the holdings are.
                "percent": (row.shares or 0) * 100 / total if total else 0,
            }
            for row in rows
        ],
        "total_shares": total,
        "transactions": list(
            ShareTransaction.objects.filter(entity=entity)
            .select_related("shareholder")
            .order_by("-executed_on")[:50]
        ),
        "can_manage": "secretarial.captable.manage" in _permissions(request),
    }
    template = (
        "secretarial/_fragments/cap_table_body.html"
        if is_fragment_request(request)
        else "secretarial/cap_table.html"
    )
    return render(request, template, context)
