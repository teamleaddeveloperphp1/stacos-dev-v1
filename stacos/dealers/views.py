"""
The dealer's own commission statement and payouts.

Note what these views cannot reach: a client's obligations, documents or notices.
The dealer tenant owns commission rows that *name* a client; naming is not
access, and there is no route from here to compliance data.
"""

from __future__ import annotations

from typing import Any

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.billing.models import to_major
from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.pagination import filters_querystring, keyset_page
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.dealers.models import CommissionEntry, CommissionPlan, Payout
from stacos.dealers.services import DealerError, approve_payout, mark_paid, statement

#: One screen's worth of payouts.
PAGE_SIZE = 20


def _permissions(request: HttpRequest) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


@require_permission("dealers.commission.view")
def commission_statement(request: HttpRequest) -> HttpResponse:
    """The ledger, line by line, plus the totals it adds up to."""
    scope = getattr(request, "access_scope", None)
    dealer_id = scope.principal_tenant_id if scope else None
    if dealer_id is None:
        raise Http404

    totals = statement(dealer_tenant_id=dealer_id)
    # Twenty payouts is under two years of monthly ones, and the list had no way
    # to reach anything older — see stacos.core.pagination.
    payouts = keyset_page(
        Payout.objects.all(),
        order_by="period_end",
        cursor=request.GET.get("cursor", ""),
        page_size=PAGE_SIZE,
        descending=True,
    )
    entries = CommissionEntry.objects.select_related("client_tenant", "plan").order_by(
        "-created_at"
    )[:100]

    context = {
        "entries": list(entries),
        "accrued": to_major(totals["accrued_minor"]),
        "approved": to_major(totals["approved_minor"]),
        "paid": to_major(totals["paid_minor"]),
        "clawed_back": to_major(totals["clawed_back_minor"]),
        "plan": CommissionPlan.objects.filter(is_active=True).order_by("-valid_from").first(),
        "payouts": payouts.rows,
        "page": payouts,
        "querystring": filters_querystring(request),
        "as_of": timezone.localdate(),
        "can_approve": "dealers.payout.approve" in _permissions(request),
        "can_pay": "dealers.payout.pay" in _permissions(request),
    }
    if request.GET.get("cursor") and is_fragment_request(request):
        return render(request, "dealers/_fragments/payout_rows.html", context)

    template = (
        "dealers/_fragments/statement_body.html"
        if is_fragment_request(request)
        else "dealers/statement.html"
    )
    return render(request, template, context)


def _get(pk: str) -> Payout:
    payout = Payout.objects.filter(pk=pk).prefetch_related("entries__client_tenant").first()
    if payout is None:
        raise Http404
    return payout


@require_permission("dealers.commission.view")
def payout_detail(request: HttpRequest, pk: str) -> HttpResponse:
    payout = _get(pk)
    return render(
        request, "dealers/_fragments/payout_detail.html", _payout_context(request, payout)
    )


def _payout_context(request: HttpRequest, payout: Payout) -> dict[str, Any]:
    permissions = _permissions(request)
    return {
        "payout": payout,
        "entries": list(payout.entries.all()),
        "total": to_major(payout.total_minor),
        "can_approve": "dealers.payout.approve" in permissions,
        "can_pay": "dealers.payout.pay" in permissions,
    }


@require_permission("dealers.payout.approve")
@require_http_methods(["POST"])
def payout_approve(request: HttpRequest, pk: str) -> HttpResponse:
    payout = _get(pk)
    try:
        approve_payout(payout, actor=current_user(request))
    except DealerError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    return _panel(request, _get(pk), toast=Toast(_("Payout approved.")))


@require_permission("dealers.payout.pay")
@require_http_methods(["POST"])
def payout_pay(request: HttpRequest, pk: str) -> HttpResponse:
    payout = _get(pk)
    try:
        mark_paid(
            payout,
            reference=request.POST.get("reference", ""),
            actor=current_user(request),
        )
    except DealerError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    return _panel(request, _get(pk), toast=Toast(_("Payout recorded as paid.")))


def _panel(
    request: HttpRequest,
    payout: Payout,
    *,
    toast: Toast | None = None,
    error: str = "",
    status: int = 200,
) -> HttpResponse:
    context = _payout_context(request, payout)
    context["error"] = error

    if status != 200:
        return render(request, "dealers/_fragments/payout_detail.html", context, status=status)

    return oob(
        request,
        Fragment("dealers/_fragments/payout_detail.html", context),
        toast=toast,
        triggers={"stacos:payout-changed": {"id": str(payout.pk)}},
    )
