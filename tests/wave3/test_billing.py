"""
Billing: money arithmetic, invoice immutability and idempotent payments.

Every assertion here is about a place where "mostly right" is not a grade.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from stacos.billing.models import Invoice, Payment, Plan, Subscription, to_major, to_minor
from stacos.billing.services import (
    BillingError,
    issue_invoice,
    mark_overdue,
    next_invoice_number,
    record_payment,
    renew,
    void_invoice,
)
from stacos.core.scope import platform_scope
from stacos.tenancy.models import Tenant

pytestmark = pytest.mark.django_db

TODAY = date(2026, 8, 12)


@pytest.fixture
def plan(db: None) -> Plan:
    with platform_scope(reason="test-fixture"):
        return Plan.objects.create(
            code="growth",
            name="Growth",
            tenant_type="ORGANISATION",
            interval=Plan.Interval.MONTHLY,
            amount_minor=to_minor("2999.00"),
            included_entities=3,
            included_users=5,
            extra_entity_minor=to_minor("399.00"),
            extra_user_minor=to_minor("199.00"),
        )


@pytest.fixture
def subscription(org: Tenant, plan: Plan) -> Subscription:
    with platform_scope(reason="test-fixture"):
        return Subscription.objects.create(
            tenant=org,
            plan=plan,
            status=Subscription.Status.ACTIVE,
            current_period_start=date(2026, 8, 1),
            current_period_end=date(2026, 8, 31),
            entity_count=3,
            user_count=5,
        )


# ===========================================================================
# Money arithmetic
# ===========================================================================


def test_minor_units_round_trip() -> None:
    """Rupees in, paise stored, rupees out — with no float anywhere."""
    assert to_minor("2999.00") == 299900
    assert to_minor(Decimal("0.01")) == 1
    assert to_major(299900) == Decimal("2999.00")
    assert to_major(1) == Decimal("0.01")


def test_extras_and_discount_are_exact(subscription: Subscription, plan: Plan) -> None:
    """Basis points, so 12.5% is exact rather than a float that drifts."""
    with platform_scope(reason="test"):
        subscription.entity_count = 5  # two beyond the three included
        subscription.user_count = 6  # one beyond the five included
        subscription.discount_bps = 1250  # 12.5%
        subscription.save()

        gross = 299900 + 2 * 39900 + 1 * 19900
        expected = gross - gross * 1250 // 10_000
        assert subscription.compute_amount_minor() == expected


# ===========================================================================
# Invoice numbering
# ===========================================================================


def test_invoice_numbers_are_sequential_within_a_financial_year(
    subscription: Subscription,
) -> None:
    """Gaps read as missing invoices; reuse reads as two documents with one identity."""
    with platform_scope(reason="test"):
        first = issue_invoice(subscription, issued_on=date(2026, 8, 12))
        second = issue_invoice(subscription, issued_on=date(2026, 9, 12))

    assert first.number == "INV/2627/00001"
    assert second.number == "INV/2627/00002"


def test_the_series_restarts_in_the_new_financial_year(subscription: Subscription) -> None:
    with platform_scope(reason="test"):
        issue_invoice(subscription, issued_on=date(2026, 8, 12))
        # 1 April opens a new Indian financial year.
        next_year = next_invoice_number(prefix="INV", issued_on=date(2027, 4, 1))

    assert next_year == "INV/2728/00001"


def test_an_issued_invoice_has_a_number_and_a_due_date(subscription: Subscription) -> None:
    with platform_scope(reason="test"):
        invoice = issue_invoice(subscription, issued_on=TODAY)

    assert invoice.status == Invoice.Status.ISSUED
    assert invoice.number
    assert invoice.due_on == TODAY + timedelta(days=15)
    assert invoice.tax_minor == invoice.subtotal_minor * 1800 // 10_000
    assert invoice.total_minor == invoice.subtotal_minor + invoice.tax_minor


def test_a_draft_cannot_masquerade_as_issued(org: Tenant) -> None:
    """The constraint, not the service: an issued invoice must carry a number."""
    with platform_scope(reason="test"), pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.create(tenant=org, status=Invoice.Status.ISSUED, number="")


# ===========================================================================
# Payments are idempotent
# ===========================================================================


def test_a_replayed_webhook_does_not_pay_twice(subscription: Subscription) -> None:
    """Every gateway retries. A second row is a refund conversation."""
    with platform_scope(reason="test"):
        invoice = issue_invoice(subscription, issued_on=TODAY)

        first, created_first = record_payment(
            invoice,
            amount_minor=invoice.total_minor,
            method=Payment.Method.CARD,
            provider="stripe",
            provider_reference="pi_12345",
        )
        second, created_second = record_payment(
            invoice,
            amount_minor=invoice.total_minor,
            method=Payment.Method.CARD,
            provider="stripe",
            provider_reference="pi_12345",
        )

        assert created_first
        assert not created_second
        assert first.pk == second.pk
        assert invoice.payments.count() == 1


def test_paying_in_full_settles_the_invoice(subscription: Subscription) -> None:
    with platform_scope(reason="test"):
        invoice = issue_invoice(subscription, issued_on=TODAY)
        record_payment(
            invoice,
            amount_minor=invoice.total_minor,
            method=Payment.Method.OFFLINE,
            external_reference="UTR123",
        )
        invoice.refresh_from_db()
        # Read inside the scope: `outstanding_minor` queries the payments.
        outstanding = invoice.outstanding_minor

    assert invoice.status == Invoice.Status.PAID
    assert outstanding == 0


def test_a_part_payment_is_recorded_as_such(subscription: Subscription) -> None:
    with platform_scope(reason="test"):
        invoice = issue_invoice(subscription, issued_on=TODAY)
        record_payment(
            invoice,
            amount_minor=invoice.total_minor // 2,
            method=Payment.Method.OFFLINE,
        )
        invoice.refresh_from_db()
        outstanding = invoice.outstanding_minor

    assert invoice.status == Invoice.Status.PARTIALLY_PAID
    assert outstanding > 0


# ===========================================================================
# Voiding
# ===========================================================================


def test_a_paid_invoice_cannot_be_voided(subscription: Subscription) -> None:
    """That is a credit note — a different document, with its own number."""
    with platform_scope(reason="test"):
        invoice = issue_invoice(subscription, issued_on=TODAY)
        record_payment(invoice, amount_minor=invoice.total_minor, method=Payment.Method.OFFLINE)

        with pytest.raises(BillingError) as exc:
            void_invoice(invoice, reason="Raised in error")

    assert "credit note" in str(exc.value)


def test_voiding_needs_a_reason(subscription: Subscription) -> None:
    with platform_scope(reason="test"):
        invoice = issue_invoice(subscription, issued_on=TODAY)
        with pytest.raises(BillingError):
            void_invoice(invoice, reason="  ")


# ===========================================================================
# Renewal and ageing
# ===========================================================================


def test_renewal_rolls_the_period_and_invoices_it(subscription: Subscription) -> None:
    with platform_scope(reason="test"):
        original_end = subscription.current_period_end
        invoice = renew(subscription)
        subscription.refresh_from_db()

    assert subscription.current_period_start == original_end + timedelta(days=1)
    assert subscription.current_period_end > subscription.current_period_start
    assert invoice.status == Invoice.Status.ISSUED


def test_overdue_is_a_status_change_not_a_suspension(subscription: Subscription) -> None:
    """Data is never made unavailable for non-payment."""
    with platform_scope(reason="test"):
        invoice = issue_invoice(subscription, issued_on=TODAY - timedelta(days=30))
        moved = mark_overdue(as_of=TODAY)
        invoice.refresh_from_db()
        subscription.refresh_from_db()

    assert moved == 1
    assert invoice.status == Invoice.Status.OVERDUE
    # The subscription is untouched: suspension is a separate, later decision.
    assert subscription.status == Subscription.Status.ACTIVE


def test_only_one_live_subscription_per_tenant(org: Tenant, plan: Plan) -> None:
    with platform_scope(reason="test"), pytest.raises(IntegrityError), transaction.atomic():
        for _ in range(2):
            Subscription.objects.create(
                tenant=org,
                plan=plan,
                current_period_start=date(2026, 8, 1),
                current_period_end=date(2026, 8, 31),
            )
