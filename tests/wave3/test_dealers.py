"""
The channel programme: commission as a ledger, and the boundary that matters.

The last test in this file is the important one. A dealer must not be able to
reach a client's compliance data, and that has to be true of the *models*, not
only of the views.
"""

from __future__ import annotations

from datetime import date

import pytest

from stacos.billing.models import Invoice, Plan, Subscription, to_minor
from stacos.billing.services import issue_invoice, void_invoice
from stacos.core.scope import platform_scope, tenant_context
from stacos.dealers.models import CommissionEntry, CommissionPlan, Payout
from stacos.dealers.services import DealerError, accrue, approve_payout, mark_paid, statement
from stacos.tenancy.models import Entity, Tenant

pytestmark = pytest.mark.django_db

TODAY = date(2026, 8, 12)


@pytest.fixture
def dealer(india: object) -> Tenant:
    with platform_scope(reason="test-fixture"):
        return Tenant.objects.create(
            type=Tenant.Type.DEALER,
            name="Sharma Associates",
            slug="sharma-channel",
            status=Tenant.Status.ACTIVE,
        )


@pytest.fixture
def commission_plan(dealer: Tenant) -> CommissionPlan:
    with platform_scope(reason="test-fixture"):
        return CommissionPlan.objects.create(
            tenant=dealer,
            name="Standard channel",
            basis=CommissionPlan.Basis.RECURRING,
            rate_bps=1500,
            valid_from=date(2026, 1, 1),
        )


@pytest.fixture
def plan(db: None) -> Plan:
    with platform_scope(reason="test-fixture"):
        return Plan.objects.create(
            code="growth-dealer",
            name="Growth",
            tenant_type="ORGANISATION",
            amount_minor=to_minor("2999.00"),
        )


@pytest.fixture
def sold_subscription(org: Tenant, plan: Plan, dealer: Tenant) -> Subscription:
    with platform_scope(reason="test-fixture"):
        return Subscription.objects.create(
            tenant=org,
            plan=plan,
            status=Subscription.Status.ACTIVE,
            current_period_start=date(2026, 8, 1),
            current_period_end=date(2026, 8, 31),
            sold_by_dealer=dealer,
        )


# ===========================================================================
# Accrual
# ===========================================================================


def test_invoicing_a_sold_subscription_accrues_commission(
    sold_subscription: Subscription, commission_plan: CommissionPlan, dealer: Tenant
) -> None:
    """Booked in the same transaction as the invoice.

    A commission without its invoice, or an invoice whose commission was missed,
    are both reconciliation problems nobody enjoys.
    """
    with platform_scope(reason="test"):
        invoice = issue_invoice(sold_subscription, issued_on=TODAY)
        entry = CommissionEntry.objects.get(tenant=dealer, invoice_id=invoice.pk)

    assert entry.rate_bps == 1500
    assert entry.amount_minor == invoice.subtotal_minor * 1500 // 10_000
    assert entry.status == CommissionEntry.Status.ACCRUED


def test_a_rerun_does_not_accrue_twice(
    sold_subscription: Subscription, commission_plan: CommissionPlan, dealer: Tenant
) -> None:
    """A webhook replay or a rerun of the billing job must not pay twice."""
    with platform_scope(reason="test"):
        invoice = issue_invoice(sold_subscription, issued_on=TODAY)
        duplicate = accrue(
            dealer_tenant_id=dealer.pk,
            client_tenant_id=sold_subscription.tenant_id,
            invoice_id=invoice.pk,
            invoice_number=invoice.number,
            base_amount_minor=invoice.subtotal_minor,
        )

        assert duplicate is None
        assert CommissionEntry.objects.filter(invoice_id=invoice.pk).count() == 1


def test_no_plan_means_no_accrual(sold_subscription: Subscription, dealer: Tenant) -> None:
    """A dealer with no active plan earns nothing, and nothing breaks."""
    with platform_scope(reason="test"):
        invoice = issue_invoice(sold_subscription, issued_on=TODAY)
        assert not CommissionEntry.objects.filter(invoice_id=invoice.pk).exists()
        assert invoice.status == Invoice.Status.ISSUED


def test_a_subscription_with_no_dealer_accrues_nothing(
    org: Tenant, plan: Plan, commission_plan: CommissionPlan
) -> None:
    with platform_scope(reason="test"):
        direct = Subscription.objects.create(
            tenant=org,
            plan=plan,
            current_period_start=date(2026, 8, 1),
            current_period_end=date(2026, 8, 31),
        )
        invoice = issue_invoice(direct, issued_on=TODAY)
        assert not CommissionEntry.objects.filter(invoice_id=invoice.pk).exists()


# ===========================================================================
# Clawback is a row, never an edit
# ===========================================================================


def test_voiding_an_invoice_claws_the_commission_back(
    sold_subscription: Subscription, commission_plan: CommissionPlan, dealer: Tenant
) -> None:
    """Both the accrual and its reversal stay visible.

    Deleting the accrual would make the ledger add up while telling the dealer
    nothing about what happened.
    """
    with platform_scope(reason="test"):
        invoice = issue_invoice(sold_subscription, issued_on=TODAY)
        void_invoice(invoice, reason="Raised against the wrong entity")

        rows = list(CommissionEntry.objects.filter(invoice_id=invoice.pk).order_by("created_at"))
        totals = statement(dealer_tenant_id=dealer.pk)

    assert len(rows) == 2, "the accrual and its reversal should both be visible"
    assert rows[0].amount_minor == -rows[1].amount_minor
    assert all(row.status == CommissionEntry.Status.CLAWED_BACK for row in rows)
    assert totals["accrued_minor"] == 0


# ===========================================================================
# Payouts
# ===========================================================================


def test_a_payout_totals_from_its_lines(
    sold_subscription: Subscription, commission_plan: CommissionPlan, dealer: Tenant
) -> None:
    with platform_scope(reason="test"):
        invoice = issue_invoice(sold_subscription, issued_on=TODAY)
        payout = Payout.objects.create(
            tenant=dealer, period_start=date(2026, 8, 1), period_end=date(2026, 8, 31)
        )
        approve_payout(payout)
        payout.refresh_from_db()

        expected = CommissionEntry.objects.get(invoice_id=invoice.pk).amount_minor

    assert payout.status == Payout.Status.APPROVED
    assert payout.total_minor == expected


def test_an_empty_payout_is_refused(dealer: Tenant) -> None:
    with platform_scope(reason="test"):
        payout = Payout.objects.create(
            tenant=dealer, period_start=date(2026, 8, 1), period_end=date(2026, 8, 31)
        )
        with pytest.raises(DealerError):
            approve_payout(payout)


def test_marking_paid_needs_a_bank_reference(
    sold_subscription: Subscription, commission_plan: CommissionPlan, dealer: Tenant
) -> None:
    """A payout that cannot be matched to a statement is the conversation this prevents."""
    with platform_scope(reason="test"):
        issue_invoice(sold_subscription, issued_on=TODAY)
        payout = Payout.objects.create(
            tenant=dealer, period_start=date(2026, 8, 1), period_end=date(2026, 8, 31)
        )
        approve_payout(payout)

        with pytest.raises(DealerError):
            mark_paid(payout, reference="  ")

        mark_paid(payout, reference="NEFT/2026/0912")
        payout.refresh_from_db()
        line_statuses = [entry.status for entry in payout.entries.all()]

    assert payout.status == Payout.Status.PAID
    assert line_statuses
    assert all(status == CommissionEntry.Status.PAID for status in line_statuses)


# ===========================================================================
# The boundary
# ===========================================================================


@pytest.mark.isolation
def test_a_dealer_cannot_reach_a_clients_compliance_data(
    dealer: Tenant,
    commission_plan: CommissionPlan,
    sold_subscription: Subscription,
    materialised: Entity,
) -> None:
    """The line the whole channel design rests on.

    A dealer sells subscriptions. Naming a client on a commission row is not
    access to that client. Sight of their obligations requires an explicit,
    time-boxed, client-approved engagement — and without one, the scoped manager
    returns nothing.
    """
    from stacos.obligations.models import ObligationInstance

    with platform_scope(reason="test"):
        issue_invoice(sold_subscription, issued_on=TODAY)

    with tenant_context(tenant_ids=dealer.id, reason="test:dealer"):
        # Their own ledger: visible, and it names the client.
        entries = list(CommissionEntry.objects.all())
        assert entries, "a dealer must be able to see their own commission"
        assert entries[0].client_tenant_id == materialised.tenant_id

        # The client's actual data: nothing.
        assert ObligationInstance.objects.count() == 0


@pytest.mark.isolation
def test_one_dealer_cannot_see_anothers_ledger(
    dealer: Tenant, commission_plan: CommissionPlan, sold_subscription: Subscription, india: object
) -> None:
    with platform_scope(reason="test"):
        issue_invoice(sold_subscription, issued_on=TODAY)
        rival = Tenant.objects.create(
            type=Tenant.Type.DEALER, name="Rival Channel", slug="rival-channel"
        )

    with tenant_context(tenant_ids=rival.id, reason="test:rival-dealer"):
        assert CommissionEntry.objects.count() == 0
        assert Payout.objects.count() == 0
