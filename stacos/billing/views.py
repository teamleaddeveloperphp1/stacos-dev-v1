"""
Billing screens: the customer's own subscription, invoices and payments.

A customer sees their own billing here. Recording a payment is separated out
because offline collection — cheque and transfer, still the majority of Indian
B2B — is somebody asserting that money arrived, and that assertion deserves
step-up authentication and an audit row.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet, Sum
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.billing.forms import PaymentForm
from stacos.billing.models import Invoice, Plan, Subscription, to_major
from stacos.billing.services import BillingError, issue_invoice, record_payment, void_invoice
from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user

UNPAID = [Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID, Invoice.Status.OVERDUE]


def _permissions(request: HttpRequest) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


@require_permission("billing.view")
def overview(request: HttpRequest) -> HttpResponse:
    """Plan, next renewal, and what is outstanding."""
    subscription = (
        Subscription.objects.select_related("plan")
        .filter(~Q(status=Subscription.Status.CANCELLED))
        .first()
    )

    invoices: QuerySet[Invoice] = (
        Invoice.objects.select_related("subscription__plan")
        .prefetch_related("payments")
        .order_by("-issued_on", "-created_at")
    )

    outstanding = (
        Invoice.objects.filter(status__in=UNPAID).aggregate(total=Sum("total_minor"))["total"] or 0
    )

    context = {
        "subscription": subscription,
        "invoices": list(invoices[:50]),
        "outstanding": to_major(outstanding),
        "as_of": timezone.localdate(),
        "can_record_payment": "billing.payment.record" in _permissions(request),
        "can_void": "billing.invoice.void" in _permissions(request),
        "payment_form": PaymentForm(),
    }
    template = (
        "billing/_fragments/overview_body.html"
        if is_fragment_request(request)
        else "billing/overview.html"
    )
    return render(request, template, context)


@require_permission("billing.view")
def plans(request: HttpRequest) -> HttpResponse:
    """The price list, for the in-app upgrade screen."""
    tenant = getattr(request, "tenant", None)
    rows = Plan.objects.filter(is_active=True, is_public=True)
    if tenant is not None:
        rows = rows.filter(tenant_type=tenant.type)

    # Linked from the billing overview, so it needs both render paths: a refresh
    # or a deep link must return the page rather than a bare fragment.
    template = (
        "billing/_fragments/plans.html" if is_fragment_request(request) else "billing/plans.html"
    )
    return render(
        request,
        template,
        {"plans": list(rows), "current": _current_plan_id()},
    )


def _current_plan_id() -> Any:
    """The plan this tenant is on, or ``None``.

    Reads through the scoped manager rather than taking the tenant as an
    argument: the scope is already bound, and passing it would invite a caller
    to pass somebody else's.
    """
    subscription = Subscription.objects.filter(~Q(status=Subscription.Status.CANCELLED)).first()
    return subscription.plan_id if subscription else None


def _get(pk: str) -> Invoice:
    invoice = (
        Invoice.objects.filter(pk=pk)
        .select_related("subscription__plan")
        .prefetch_related("lines", "payments")
        .first()
    )
    if invoice is None:
        raise Http404
    return invoice


@require_permission("billing.view")
def invoice_detail(request: HttpRequest, pk: str) -> HttpResponse:
    invoice = _get(pk)
    return render(
        request,
        "billing/_fragments/invoice_detail.html",
        {
            "invoice": invoice,
            "lines": list(invoice.lines.all()),
            "payments": list(invoice.payments.all()),
            "outstanding": to_major(invoice.outstanding_minor),
            "payment_form": PaymentForm(),
            "can_record_payment": "billing.payment.record" in _permissions(request),
            "can_void": "billing.invoice.void" in _permissions(request),
        },
    )


@require_permission("billing.payment.record")
@require_http_methods(["POST"])
def payment_record(request: HttpRequest, pk: str) -> HttpResponse:
    """Record an offline payment against an invoice."""
    invoice = _get(pk)
    form = PaymentForm(request.POST)
    if not form.is_valid():
        return _panel(request, invoice, error=_("Check the payment details."), status=422)

    try:
        record_payment(
            invoice,
            amount_minor=form.cleaned_data["amount_minor"],
            method=form.cleaned_data["method"],
            external_reference=form.cleaned_data.get("external_reference", ""),
            received_on=form.cleaned_data.get("received_on"),
            actor=current_user(request),
        )
    except BillingError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    return _panel(request, _get(pk), toast=Toast(_("Payment recorded.")))


@require_permission("billing.invoice.void")
@require_http_methods(["POST"])
def invoice_void(request: HttpRequest, pk: str) -> HttpResponse:
    invoice = _get(pk)
    try:
        void_invoice(invoice, reason=request.POST.get("reason", ""), actor=current_user(request))
    except BillingError as exc:
        return _panel(request, _get(pk), error=str(exc), status=422)

    return _panel(request, _get(pk), toast=Toast(_("Invoice voided.")))


@require_permission("billing.invoice.issue")
@require_http_methods(["POST"])
def invoice_issue(request: HttpRequest) -> HttpResponse:
    """Raise an invoice for the current period, out of cycle."""
    subscription = Subscription.objects.filter(~Q(status=Subscription.Status.CANCELLED)).first()
    if subscription is None:
        raise Http404

    invoice = issue_invoice(subscription, actor=current_user(request))
    return oob(
        request,
        "",
        toast=Toast(_("Invoice %(number)s issued.") % {"number": invoice.number}),
        triggers={"stacos:billing-changed": True},
    )


def _panel(
    request: HttpRequest,
    invoice: Invoice,
    *,
    toast: Toast | None = None,
    error: str = "",
    status: int = 200,
) -> HttpResponse:
    context = {
        "invoice": invoice,
        "lines": list(invoice.lines.all()),
        "payments": list(invoice.payments.all()),
        "outstanding": to_major(invoice.outstanding_minor),
        "payment_form": PaymentForm(),
        "can_record_payment": "billing.payment.record" in _permissions(request),
        "can_void": "billing.invoice.void" in _permissions(request),
        "error": error,
    }

    if status != 200:
        return render(request, "billing/_fragments/invoice_panel.html", context, status=status)

    return oob(
        request,
        Fragment("billing/_fragments/invoice_panel.html", context),
        toast=toast,
        triggers={"stacos:billing-changed": {"id": str(invoice.pk)}},
    )
