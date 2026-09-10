"""
The information request screens.

The list is the practice's daily queue. The detail page is where a client answers
and a preparer accepts or rejects — one screen, two audiences, and the action set
differs entirely by permission rather than by a separate portal.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.db.models import Count, Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.requests.forms import ItemResponseForm, RejectForm, RequestForm
from stacos.requests.models import (
    OPEN_REQUEST_STATES,
    InformationRequest,
    RequestState,
)
from stacos.requests.services import (
    RESPONDER_TOKEN_DAYS,
    RequestError,
    close_request,
    issue_responder_token,
    record_response,
    reject_response,
    send_request,
    send_responder_link,
)
from stacos.vault.models import LinkTarget
from stacos.vault.services import documents_for

_OPEN = sorted(str(state) for state in OPEN_REQUEST_STATES)

#: What the default list shows. Wider than ``OPEN_REQUEST_STATES``, which means
#: "sent and awaiting the client" — a draft is not waiting on anybody, but it is
#: very much the preparer's outstanding work, and a queue that hides the request
#: you just wrote is a queue you cannot use.
_WORKING = sorted({*_OPEN, str(RequestState.DRAFT)})


def _permissions(request: HttpRequest) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


def _filtered(request: HttpRequest, *, as_of: date) -> QuerySet[InformationRequest]:
    """Apply the toolbar filters.

    ``prefetch_related("items")`` is doing real work here: the list renders a
    progress fraction per row, and that is computed from the items. Without it a
    fifty-row page issues fifty extra queries, which the query-count test exists
    to prevent.
    """
    queryset = (
        InformationRequest.objects.filter(archived_at__isnull=True)
        .select_related("entity", "assigned_to", "obligation")
        .prefetch_related("items")
    )

    status = request.GET.get("status", "")
    if status == "overdue":
        queryset = queryset.filter(state__in=_OPEN, due_on__lt=as_of)
    elif status == "awaiting_review":
        queryset = queryset.filter(state=RequestState.ANSWERED)
    elif status == "closed":
        queryset = queryset.filter(state__in=[RequestState.CLOSED, RequestState.CANCELLED])
    elif status == "mine":
        queryset = queryset.filter(state__in=_WORKING, assigned_to=current_user(request))
    elif status != "all":
        queryset = queryset.filter(state__in=_WORKING)

    search = request.GET.get("q", "").strip()
    if search:
        queryset = queryset.filter(
            Q(title__icontains=search)
            | Q(message__icontains=search)
            | Q(entity__name__icontains=search)
        )

    entity_id = request.GET.get("entity", "").strip()
    if entity_id:
        queryset = queryset.filter(entity_id=entity_id)

    return queryset.order_by("due_on", "-created_at")


@require_permission("rfi.request.view")
def request_list(request: HttpRequest) -> HttpResponse:
    """The queue: what has been asked, and what has come back."""
    as_of = timezone.localdate()
    rows = list(_filtered(request, as_of=as_of)[:100])

    counts = InformationRequest.objects.filter(archived_at__isnull=True).aggregate(
        open=Count("id", filter=Q(state__in=_WORKING)),
        overdue=Count("id", filter=Q(state__in=_OPEN) & Q(due_on__lt=as_of)),
        awaiting_review=Count("id", filter=Q(state=RequestState.ANSWERED)),
    )

    context = {
        "requests": rows,
        "counts": counts,
        "as_of": as_of,
        "status": request.GET.get("status", ""),
        "search": request.GET.get("q", ""),
    }
    template = (
        "requests/_fragments/list_body.html"
        if is_fragment_request(request)
        else "requests/list.html"
    )
    return render(request, template, context)


def _get(pk: str) -> InformationRequest:
    """Fetch through the scoped manager. 404 on anything out of reach."""
    found = (
        InformationRequest.objects.filter(pk=pk)
        .select_related("entity", "assigned_to", "requested_by", "obligation")
        .prefetch_related("items", "events__actor")
        .first()
    )
    if found is None:
        raise Http404
    return found


def _detail_context(
    request: HttpRequest, information_request: InformationRequest
) -> dict[str, Any]:
    items = list(information_request.items.all())
    permissions = _permissions(request)
    return {
        "request_obj": information_request,
        "items": [
            {
                "item": item,
                "documents": documents_for(target_type=LinkTarget.REQUEST_ITEM, target_id=item.pk),
                "form": ItemResponseForm(item=item),
            }
            for item in items
        ],
        "events": list(information_request.events.all()[:50]),
        "can_respond": "rfi.request.respond" in permissions,
        "can_review": "rfi.request.review" in permissions,
        "can_send": "rfi.request.send" in permissions,
        "can_close": "rfi.request.close" in permissions,
        "reject_form": RejectForm(),
        "as_of": timezone.localdate(),
    }


@require_permission("rfi.request.view")
def request_detail(request: HttpRequest, pk: str) -> HttpResponse:
    information_request = _get(pk)
    context = _detail_context(request, information_request)
    template = (
        "requests/_fragments/detail_body.html"
        if is_fragment_request(request)
        else "requests/detail.html"
    )
    return render(request, template, context)


@require_permission("rfi.request.create")
@require_http_methods(["GET", "POST"])
def request_create(request: HttpRequest) -> HttpResponse:
    """Raise a request, in a modal.

    The items are entered as one per line rather than as a formset. A preparer
    listing six documents wants to type six lines, not open six sub-forms, and
    the structure is recovered on save.
    """
    form = RequestForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        information_request = form.save(actor=current_user(request))
        return oob(
            request,
            "",
            toast=Toast(_("Request drafted. Review the items, then send it.")),
            triggers={
                "stacos:modal-close": True,
                "stacos:navigate": reverse("rfi:detail", args=[information_request.pk]),
            },
        )

    return render(
        request,
        "requests/_fragments/request_form_modal.html",
        {"form": form},
        status=422 if request.method == "POST" else 200,
    )


@require_permission("rfi.request.send")
@require_http_methods(["POST"])
def request_send(request: HttpRequest, pk: str) -> HttpResponse:
    information_request = _get(pk)
    try:
        send_request(information_request, actor=current_user(request))
    except RequestError as exc:
        return _detail_panel(request, _get(pk), error=str(exc), status=422)

    return _detail_panel(
        request,
        _get(pk),
        toast=Toast(_("Request sent.")),
    )


@require_permission("rfi.request.respond")
@require_http_methods(["POST"])
def item_respond(request: HttpRequest, pk: str, item_pk: str) -> HttpResponse:
    """Answer one item. Documents arrive through the vault upload endpoint."""
    information_request = _get(pk)
    item = information_request.items.filter(pk=item_pk).first()
    if item is None:
        raise Http404

    form = ItemResponseForm(request.POST, item=item)
    if not form.is_valid():
        return _detail_panel(request, _get(pk), error=_("Enter an answer."), status=422)

    try:
        record_response(item, value=form.cleaned_data.get("value", ""), actor=current_user(request))
    except RequestError as exc:
        return _detail_panel(request, _get(pk), error=str(exc), status=422)

    return _detail_panel(request, _get(pk), toast=Toast(_("Answer recorded.")))


@require_permission("rfi.request.review")
@require_http_methods(["POST"])
def item_reject(request: HttpRequest, pk: str, item_pk: str) -> HttpResponse:
    """Send an item back with a reason."""
    information_request = _get(pk)
    item = information_request.items.filter(pk=item_pk).first()
    if item is None:
        raise Http404

    form = RejectForm(request.POST)
    if not form.is_valid():
        return _detail_panel(
            request,
            _get(pk),
            error=_("Say what was wrong with it — otherwise the same thing comes back."),
            status=422,
        )

    try:
        reject_response(item, reason=form.cleaned_data["reason"], actor=current_user(request))
    except RequestError as exc:
        return _detail_panel(request, _get(pk), error=str(exc), status=422)

    return _detail_panel(request, _get(pk), toast=Toast(_("Sent back to the client.")))


@require_permission("rfi.request.close")
@require_http_methods(["POST"])
def request_close(request: HttpRequest, pk: str) -> HttpResponse:
    information_request = _get(pk)
    close_request(
        information_request,
        actor=current_user(request),
        note=request.POST.get("note", ""),
    )
    return _detail_panel(request, _get(pk), toast=Toast(_("Request closed.")))


@require_permission("rfi.request.send")
@require_http_methods(["POST"])
def request_invite_responder(request: HttpRequest, pk: str) -> HttpResponse:
    """Email the outside contact a link they can actually answer on.

    The gap this closes: a request addressed to ``assigned_email`` was delivered
    to nobody and answerable by nobody. ``services.notify`` says as much in a
    comment — it returns early for a recipient with no account, deferring to a
    delivery path that did not exist.

    Deliberately an explicit action rather than something that fires on send. The
    link is a bearer credential for one request; issuing it is a decision
    somebody takes, and the audit trail says who took it.
    """
    information_request = _get(pk)

    try:
        token, raw = issue_responder_token(
            information_request,
            actor=current_user(request),
        )
    except RequestError as exc:
        return _detail_panel(request, information_request, error=str(exc), status=422)

    send_responder_link(information_request, token=token, raw_token=raw, request=request)

    return _detail_panel(
        request,
        _get(pk),
        toast=Toast(
            _("Link sent to %(email)s. It works for %(days)s days.")
            % {"email": token.email, "days": RESPONDER_TOKEN_DAYS}
        ),
    )


def _detail_panel(
    request: HttpRequest,
    information_request: InformationRequest,
    *,
    toast: Toast | None = None,
    error: str = "",
    status: int = 200,
) -> HttpResponse:
    """Re-render the working panel, plus the queue counters out of band."""
    context = _detail_context(request, information_request)
    context["error"] = error

    if status != 200:
        return render(request, "requests/_fragments/detail_panel.html", context, status=status)

    return oob(
        request,
        Fragment("requests/_fragments/detail_panel.html", context),
        toast=toast,
        triggers={"stacos:request-changed": {"id": str(information_request.pk)}},
    )


@require_permission("rfi.request.view")
def obligation_requests(request: HttpRequest, obligation_pk: str) -> HttpResponse:
    """Requests raised against one obligation, for its detail page."""
    rows = (
        InformationRequest.objects.filter(obligation_id=obligation_pk, archived_at__isnull=True)
        .select_related("assigned_to")
        .prefetch_related("items")
        .order_by("-created_at")
    )
    return render(
        request,
        "requests/_fragments/obligation_requests.html",
        {"requests": list(rows), "obligation_pk": obligation_pk},
    )
