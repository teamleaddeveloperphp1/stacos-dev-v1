"""
The return preparation screens.

The list is the filing pipeline: what is being made, what is waiting on a
checker, what is waiting on the client. The detail page is where the maker,
checker, approver and filer each do their one thing.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.obligations.models import ObligationInstance
from stacos.returns.forms import FigureForm, FileReturnForm, ReviewForm
from stacos.returns.models import (
    OPEN_PREPARATION_STATES,
    PreparationState,
    ReconciliationDifference,
    ReturnPreparation,
)
from stacos.returns.services import (
    PreparationError,
    approve,
    file_return,
    get_or_create_for,
    mark_prepared,
    resolve_difference,
    review,
    send_back,
    summarise,
)
from stacos.vault.models import LinkTarget
from stacos.vault.services import documents_for

_OPEN = sorted(str(state) for state in OPEN_PREPARATION_STATES)


def _permissions(request: HttpRequest) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


def _filtered(request: HttpRequest) -> QuerySet[ReturnPreparation]:
    queryset = (
        ReturnPreparation.objects.filter(archived_at__isnull=True)
        .select_related("entity", "obligation", "prepared_by", "reviewed_by")
        .prefetch_related("reconciliations__differences")
    )

    status = request.GET.get("status", "")
    if status == "awaiting_review":
        queryset = queryset.filter(state=PreparationState.PREPARED)
    elif status == "awaiting_approval":
        queryset = queryset.filter(state=PreparationState.REVIEWED)
    elif status == "rework":
        queryset = queryset.filter(state=PreparationState.REWORK)
    elif status == "filed":
        queryset = queryset.filter(state=PreparationState.FILED)
    elif status == "mine":
        queryset = queryset.filter(state__in=_OPEN, prepared_by=current_user(request))
    elif status != "all":
        queryset = queryset.filter(state__in=_OPEN)

    search = request.GET.get("q", "").strip()
    if search:
        queryset = queryset.filter(
            Q(form_type__icontains=search)
            | Q(period_key__icontains=search)
            | Q(entity__name__icontains=search)
        )

    return queryset.order_by("obligation__due_date", "-created_at")


@require_permission("returns.preparation.view")
def preparation_list(request: HttpRequest) -> HttpResponse:
    rows = list(_filtered(request)[:100])

    counts = ReturnPreparation.objects.filter(archived_at__isnull=True).aggregate(
        open=Count("id", filter=Q(state__in=_OPEN)),
        awaiting_review=Count("id", filter=Q(state=PreparationState.PREPARED)),
        awaiting_approval=Count("id", filter=Q(state=PreparationState.REVIEWED)),
        rework=Count("id", filter=Q(state=PreparationState.REWORK)),
    )

    context = {
        "preparations": rows,
        "counts": counts,
        "status": request.GET.get("status", ""),
        "search": request.GET.get("q", ""),
        "as_of": timezone.localdate(),
    }
    template = (
        "returns/_fragments/list_body.html" if is_fragment_request(request) else "returns/list.html"
    )
    return render(request, template, context)


def _get(pk: str) -> ReturnPreparation:
    preparation = (
        ReturnPreparation.objects.filter(pk=pk, archived_at__isnull=True)
        .select_related("entity", "obligation", "prepared_by", "reviewed_by", "approved_by")
        .prefetch_related("reconciliations__differences")
        .first()
    )
    if preparation is None:
        raise Http404
    return preparation


def _detail_context(request: HttpRequest, preparation: ReturnPreparation) -> dict[str, Any]:
    permissions = _permissions(request)
    user = current_user(request)
    return {
        "preparation": preparation,
        "reconciliations": [
            {
                "reconciliation": row,
                "summary": summarise(row),
                "differences": list(row.differences.all()),
            }
            for row in preparation.reconciliations.all()
        ],
        "documents": documents_for(target_type=LinkTarget.RETURN, target_id=preparation.pk),
        "figure_form": FigureForm(instance=preparation),
        "review_form": ReviewForm(),
        "file_form": FileReturnForm(),
        "can_prepare": "returns.preparation.prepare" in permissions,
        # The checker cannot be the maker. Decided here so the button is absent,
        # and again in the service so its absence is not the only thing stopping it.
        "can_review": (
            "returns.preparation.review" in permissions
            and preparation.prepared_by_id != getattr(user, "pk", None)
        ),
        "reviewer_is_preparer": preparation.prepared_by_id == getattr(user, "pk", None),
        "can_approve": "returns.preparation.approve" in permissions,
        "can_file": "returns.preparation.file" in permissions,
        "can_resolve": "returns.reconciliation.run" in permissions,
    }


@require_permission("returns.preparation.view")
def preparation_detail(request: HttpRequest, pk: str) -> HttpResponse:
    preparation = _get(pk)
    context = _detail_context(request, preparation)
    template = (
        "returns/_fragments/detail_body.html"
        if is_fragment_request(request)
        else "returns/detail.html"
    )
    return render(request, template, context)


@require_permission("returns.preparation.prepare")
@require_http_methods(["POST"])
def preparation_open(request: HttpRequest, obligation_pk: str) -> HttpResponse:
    """Open working papers for an obligation."""
    obligation = ObligationInstance.objects.filter(pk=obligation_pk).first()
    if obligation is None:
        raise Http404

    preparation, created = get_or_create_for(obligation, actor=current_user(request))
    return oob(
        request,
        "",
        toast=Toast(_("Working papers opened.") if created else _("Working papers already exist.")),
        triggers={"stacos:navigate": f"/app/returns/{preparation.pk}/"},
    )


@require_permission("returns.preparation.prepare")
@require_http_methods(["POST"])
def preparation_save(request: HttpRequest, pk: str) -> HttpResponse:
    """Save figures without submitting for review."""
    preparation = _get(pk)
    form = FigureForm(request.POST, instance=preparation)
    if not form.is_valid():
        return _panel(request, preparation, error=_("Check the figures."), status=422)

    form.save()
    return _panel(request, _get(pk), toast=Toast(_("Figures saved.")))


@require_permission("returns.preparation.prepare")
@require_http_methods(["POST"])
def preparation_submit(request: HttpRequest, pk: str) -> HttpResponse:
    preparation = _get(pk)
    try:
        mark_prepared(preparation, actor=current_user(request))
    except PreparationError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    return _panel(request, _get(pk), toast=Toast(_("Submitted for review.")))


@require_permission("returns.preparation.review")
@require_http_methods(["POST"])
def preparation_review(request: HttpRequest, pk: str) -> HttpResponse:
    preparation = _get(pk)
    form = ReviewForm(request.POST)
    if not form.is_valid():
        return _panel(request, preparation, error=_("Check the form."), status=422)

    action = form.cleaned_data["action"]
    try:
        if action == "SEND_BACK":
            send_back(preparation, actor=current_user(request), notes=form.cleaned_data["notes"])
            message = _("Sent back to the preparer.")
        else:
            review(
                preparation, actor=current_user(request), notes=form.cleaned_data.get("notes", "")
            )
            message = _("Reviewed.")
    except PreparationError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    return _panel(request, _get(pk), toast=Toast(message))


@require_permission("returns.preparation.approve")
@require_http_methods(["POST"])
def preparation_approve(request: HttpRequest, pk: str) -> HttpResponse:
    preparation = _get(pk)
    try:
        approve(preparation, actor=current_user(request))
    except PreparationError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    return _panel(request, _get(pk), toast=Toast(_("Approved for filing.")))


@require_permission("returns.preparation.file")
@require_http_methods(["POST"])
def preparation_file(request: HttpRequest, pk: str) -> HttpResponse:
    preparation = _get(pk)
    form = FileReturnForm(request.POST)
    if not form.is_valid():
        return _panel(request, preparation, error=_("Check the filing details."), status=422)

    try:
        file_return(
            preparation,
            actor=current_user(request),
            reference=form.cleaned_data["reference"],
            filed_on=form.cleaned_data.get("filed_on"),
            permissions=_permissions(request),
        )
    except PreparationError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    return _panel(request, _get(pk), toast=Toast(_("Filed. The obligation has been closed out.")))


@require_permission("returns.reconciliation.run")
@require_http_methods(["POST"])
def difference_resolve(request: HttpRequest, pk: str, difference_pk: str) -> HttpResponse:
    """Explain one reconciliation line."""
    preparation = _get(pk)
    difference = ReconciliationDifference.objects.filter(
        pk=difference_pk, reconciliation__preparation=preparation
    ).first()
    if difference is None:
        raise Http404

    resolve_difference(
        difference,
        resolution=request.POST.get("resolution", ""),
        note=request.POST.get("note", ""),
        actor=current_user(request),
    )
    return _panel(request, _get(pk), toast=Toast(_("Difference explained.")))


def _panel(
    request: HttpRequest,
    preparation: ReturnPreparation,
    *,
    toast: Toast | None = None,
    error: str = "",
    status: int = 200,
) -> HttpResponse:
    context = _detail_context(request, preparation)
    context["error"] = error

    if status != 200:
        return render(request, "returns/_fragments/detail_panel.html", context, status=status)

    return oob(
        request,
        Fragment("returns/_fragments/detail_panel.html", context),
        toast=toast,
        triggers={"stacos:return-changed": {"id": str(preparation.pk)}},
    )
