"""
The notice tracker screens.

The list is sorted by how little time is left, because that is the only ordering
a notice register should ever have. Everything else is a filter on top of it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.db.models import Count, F, Q, QuerySet, Sum
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.notices.forms import NoticeForm, NoticeResponseForm, TransitionForm
from stacos.notices.models import OPEN_NOTICE_STATES, Notice, NoticeState, NoticeType
from stacos.notices.portals import available_adapters
from stacos.notices.services import NoticeError, close_notice, record_response, transition
from stacos.vault.models import LinkTarget
from stacos.vault.services import documents_for

_OPEN = sorted(str(state) for state in OPEN_NOTICE_STATES)


def _permissions(request: HttpRequest) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


def _filtered(request: HttpRequest, *, as_of: date) -> QuerySet[Notice]:
    queryset = Notice.objects.filter(archived_at__isnull=True).select_related(
        "entity", "authority", "assigned_to"
    )

    status = request.GET.get("status", "")
    if status == "overdue":
        queryset = queryset.filter(state__in=_OPEN, respond_by__lt=as_of)
    elif status == "undated":
        # Notices with no stated deadline. Deliberately surfaced rather than
        # given a guessed date — somebody has to read the notice and decide.
        queryset = queryset.filter(state__in=_OPEN, respond_by__isnull=True)
    elif status == "closed":
        queryset = queryset.filter(state=NoticeState.CLOSED)
    elif status == "mine":
        queryset = queryset.filter(state__in=_OPEN, assigned_to=current_user(request))
    elif status != "all":
        queryset = queryset.filter(state__in=_OPEN)

    search = request.GET.get("q", "").strip()
    if search:
        queryset = queryset.filter(
            Q(reference_number__icontains=search)
            | Q(subject__icontains=search)
            | Q(statutory_reference__icontains=search)
            | Q(entity__name__icontains=search)
        )

    notice_type = request.GET.get("type", "").strip()
    if notice_type:
        queryset = queryset.filter(notice_type=notice_type)

    entity_id = request.GET.get("entity", "").strip()
    if entity_id:
        queryset = queryset.filter(entity_id=entity_id)

    # Nulls last: a notice with no stated deadline is not the most urgent thing
    # in the register, and sorting it to the top would make the list useless.
    return queryset.order_by(F("respond_by").asc(nulls_last=True), "-received_on")


@require_permission("notices.notice.view")
def notice_list(request: HttpRequest) -> HttpResponse:
    as_of = timezone.localdate()
    rows = list(_filtered(request, as_of=as_of)[:100])

    summary = Notice.objects.filter(archived_at__isnull=True).aggregate(
        open=Count("id", filter=Q(state__in=_OPEN)),
        overdue=Count("id", filter=Q(state__in=_OPEN) & Q(respond_by__lt=as_of)),
        undated=Count("id", filter=Q(state__in=_OPEN) & Q(respond_by__isnull=True)),
        demanded=Sum("demand_amount", filter=Q(state__in=_OPEN)),
    )

    context = {
        "notices": rows,
        "summary": summary,
        "as_of": as_of,
        "status": request.GET.get("status", ""),
        "search": request.GET.get("q", ""),
        "notice_type": request.GET.get("type", ""),
        "notice_types": NoticeType.choices,
        # Empty today, and the template says so plainly rather than offering a
        # button that does nothing. See stacos/notices/portals.py.
        "adapters": available_adapters(),
    }
    template = (
        "notices/_fragments/list_body.html" if is_fragment_request(request) else "notices/list.html"
    )
    return render(request, template, context)


def _get(pk: str) -> Notice:
    notice = (
        Notice.objects.filter(pk=pk, archived_at__isnull=True)
        .select_related("entity", "authority", "assigned_to")
        .prefetch_related("events__actor")
        .first()
    )
    if notice is None:
        raise Http404
    return notice


def _detail_context(request: HttpRequest, notice: Notice) -> dict[str, Any]:
    permissions = _permissions(request)
    return {
        "notice": notice,
        "events": list(notice.events.all()[:50]),
        "documents": documents_for(target_type=LinkTarget.NOTICE, target_id=notice.pk),
        "states": NoticeState.choices,
        "transition_form": TransitionForm(),
        "response_form": NoticeResponseForm(),
        "can_respond": "notices.notice.respond" in permissions,
        "can_close": "notices.notice.close" in permissions,
        "can_edit": "notices.notice.edit" in permissions,
        "as_of": timezone.localdate(),
    }


@require_permission("notices.notice.view")
def notice_detail(request: HttpRequest, pk: str) -> HttpResponse:
    notice = _get(pk)
    context = _detail_context(request, notice)
    template = (
        "notices/_fragments/detail_body.html"
        if is_fragment_request(request)
        else "notices/detail.html"
    )
    return render(request, template, context)


@require_permission("notices.notice.create")
@require_http_methods(["GET", "POST"])
def notice_create(request: HttpRequest) -> HttpResponse:
    form = NoticeForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        notice = form.save(actor=current_user(request))
        return oob(
            request,
            "",
            toast=Toast(_("Notice recorded.")),
            triggers={
                "stacos:modal-close": True,
                "stacos:navigate": reverse("notices:detail", args=[notice.pk]),
            },
        )

    return render(
        request,
        "notices/_fragments/notice_form_modal.html",
        {"form": form},
        status=422 if request.method == "POST" else 200,
    )


@require_permission("notices.notice.view")
@require_http_methods(["POST"])
def notice_transition(request: HttpRequest, pk: str) -> HttpResponse:
    """Move a notice along.

    Declares only ``view``; which *target* is permitted is decided per state
    below, because recording a response and closing a matter are sensitive while
    moving something to "under review" is not.
    """
    notice = _get(pk)
    form = TransitionForm(request.POST)
    if not form.is_valid():
        return _panel(request, notice, error=_("That form could not be read."), status=422)

    target = form.cleaned_data["target"]
    permissions = _permissions(request)
    needed = {
        NoticeState.RESPONDED: "notices.notice.respond",
        NoticeState.CLOSED: "notices.notice.close",
    }.get(target, "notices.notice.edit")

    if needed not in permissions:
        return _panel(
            request, notice, error=_("You do not have permission to do that."), status=403
        )

    try:
        transition(
            notice,
            target=target,
            actor=current_user(request),
            note=form.cleaned_data.get("note", ""),
        )
    except NoticeError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    return _panel(request, _get(pk), toast=Toast(_("Notice updated.")))


@require_permission("notices.notice.respond")
@require_http_methods(["POST"])
def notice_respond(request: HttpRequest, pk: str) -> HttpResponse:
    notice = _get(pk)
    form = NoticeResponseForm(request.POST)
    if not form.is_valid():
        return _panel(request, notice, error=_("Check the response details."), status=422)

    try:
        record_response(
            notice,
            responded_on=form.cleaned_data["responded_on"],
            reference=form.cleaned_data["reference"],
            note=form.cleaned_data.get("note", ""),
            actor=current_user(request),
        )
    except NoticeError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    return _panel(request, _get(pk), toast=Toast(_("Response recorded.")))


@require_permission("notices.notice.close")
@require_http_methods(["POST"])
def notice_close(request: HttpRequest, pk: str) -> HttpResponse:
    notice = _get(pk)
    try:
        close_notice(
            notice,
            outcome=request.POST.get("outcome", ""),
            actor=current_user(request),
        )
    except NoticeError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    return _panel(request, _get(pk), toast=Toast(_("Notice closed.")))


def _panel(
    request: HttpRequest,
    notice: Notice,
    *,
    toast: Toast | None = None,
    error: str = "",
    status: int = 200,
) -> HttpResponse:
    context = _detail_context(request, notice)
    context["error"] = error

    if status != 200:
        return render(request, "notices/_fragments/detail_panel.html", context, status=status)

    return oob(
        request,
        Fragment("notices/_fragments/detail_panel.html", context),
        toast=toast,
        triggers={"stacos:notice-changed": {"id": str(notice.pk)}},
    )


@require_permission("notices.notice.view")
def entity_notices(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """Open notices for one entity, for its detail page."""
    rows = (
        Notice.objects.filter(entity_id=entity_pk, archived_at__isnull=True, state__in=_OPEN)
        .select_related("authority")
        .order_by("respond_by")[:10]
    )
    return render(
        request,
        "notices/_fragments/entity_notices.html",
        {"notices": list(rows), "entity_pk": entity_pk, "as_of": timezone.localdate()},
    )
