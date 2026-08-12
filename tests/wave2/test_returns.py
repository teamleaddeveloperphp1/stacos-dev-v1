"""
Return preparation, and the one control the module exists for.

Maker-checker is asserted twice — once through the service, once against the
database constraint directly — because a rule enforced only in a code path is a
rule that is quietly not followed on the filings that matter most.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.obligations.models import ObligationInstance
from stacos.returns.models import (
    PreparationState,
    Reconciliation,
    ReconciliationDifference,
    ReturnPreparation,
)
from stacos.returns.services import (
    PreparationError,
    approve,
    file_return,
    get_or_create_for,
    mark_prepared,
    review,
    send_back,
)
from stacos.tenancy.models import Entity, Tenant
from tests.conftest import AS_OF, sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def obligation(an_obligation: ObligationInstance) -> ObligationInstance:
    return an_obligation


@pytest.fixture
def preparation(obligation: ObligationInstance) -> ReturnPreparation:
    with platform_scope(reason="test-fixture"):
        prep, _ = get_or_create_for(obligation)
        return prep


@pytest.fixture
def checker(org: Tenant) -> User:
    """A second person, because maker-checker needs two."""
    from django.core.management import call_command

    from stacos.tenancy.models import Membership, Role

    with platform_scope(reason="test-fixture"):
        user = User.objects.create_user(
            email="checker@acme.example",
            password="test-password-12345",
            full_name="Priya Nair",
            phone_e164="+919800000009",
            email_verified=True,
            phone_verified=True,
        )
        role = Role.objects.filter(
            tenant__isnull=True, code="org-owner", tenant_type=org.type
        ).first()
        if role is None:
            call_command("sync_system_roles", verbosity=0)
            role = Role.objects.get(tenant__isnull=True, code="org-owner", tenant_type=org.type)
        Membership.objects.create(tenant=org, user=user, role=role, status=Membership.Status.ACTIVE)
        return user


# ===========================================================================
# Maker-checker
# ===========================================================================


def test_the_preparer_cannot_review_their_own_return(
    preparation: ReturnPreparation, org_owner: User
) -> None:
    """The rule the module exists for."""
    with platform_scope(reason="test"):
        mark_prepared(preparation, actor=org_owner)

        with pytest.raises(PreparationError) as exc:
            review(preparation, actor=org_owner)

    assert "somebody else" in str(exc.value)


def test_the_database_refuses_it_too(preparation: ReturnPreparation, org_owner: User) -> None:
    """Belt and braces.

    A rule that lives only in a service is a rule a future bulk-import script
    will not know about. This asserts the constraint itself, by writing the row
    the service would have refused.
    """
    with platform_scope(reason="test"):
        preparation.prepared_by = org_owner
        preparation.save(update_fields=["prepared_by"])

        with pytest.raises(IntegrityError), transaction.atomic():
            ReturnPreparation.objects.filter(pk=preparation.pk).update(reviewed_by=org_owner)


def test_a_different_person_can_review(
    preparation: ReturnPreparation, org_owner: User, checker: User
) -> None:
    with platform_scope(reason="test"):
        mark_prepared(preparation, actor=org_owner)
        review(preparation, actor=checker, notes="Agrees to the ledger.")
        preparation.refresh_from_db()

    assert preparation.state == PreparationState.REVIEWED
    assert preparation.reviewed_by_id == checker.pk


def test_sending_back_needs_findings(
    preparation: ReturnPreparation, org_owner: User, checker: User
) -> None:
    with platform_scope(reason="test"):
        mark_prepared(preparation, actor=org_owner)
        with pytest.raises(PreparationError):
            send_back(preparation, actor=checker, notes="  ")


def test_sending_back_returns_it_to_the_maker(
    preparation: ReturnPreparation, org_owner: User, checker: User
) -> None:
    with platform_scope(reason="test"):
        mark_prepared(preparation, actor=org_owner)
        send_back(preparation, actor=checker, notes="ITC in table 4 does not tie to 2B.")
        preparation.refresh_from_db()

    assert preparation.state == PreparationState.REWORK
    assert "table 4" in preparation.review_notes


# ===========================================================================
# Unexplained differences block submission
# ===========================================================================


def test_unexplained_differences_block_submission(
    preparation: ReturnPreparation, org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """A return submitted with forty unexplained differences gets waved through."""
    with platform_scope(reason="test"):
        reconciliation = Reconciliation.objects.create(
            tenant=org,
            entity=entity_a,
            preparation=preparation,
            kind=Reconciliation.Kind.GSTR2B_VS_BOOKS,
            left_total=Decimal("100000"),
            right_total=Decimal("94000"),
        )
        ReconciliationDifference.objects.create(
            tenant=org,
            entity=entity_a,
            reconciliation=reconciliation,
            reference="INV-9912",
            left_amount=Decimal("6000"),
            right_amount=Decimal("0"),
        )

        with pytest.raises(PreparationError) as exc:
            mark_prepared(preparation, actor=org_owner)

    assert "unexplained" in str(exc.value)


def test_explaining_them_unblocks_it(
    preparation: ReturnPreparation, org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        reconciliation = Reconciliation.objects.create(
            tenant=org,
            entity=entity_a,
            preparation=preparation,
            kind=Reconciliation.Kind.GSTR2B_VS_BOOKS,
        )
        difference = ReconciliationDifference.objects.create(
            tenant=org,
            entity=entity_a,
            reconciliation=reconciliation,
            reference="INV-9912",
            left_amount=Decimal("6000"),
        )
        difference.resolution = ReconciliationDifference.Resolution.SUPPLIER_ERROR
        difference.save(update_fields=["resolution"])

        mark_prepared(preparation, actor=org_owner)
        preparation.refresh_from_db()

    assert preparation.state == PreparationState.PREPARED


# ===========================================================================
# Filing
# ===========================================================================


def test_filing_needs_approval_first(preparation: ReturnPreparation, org_owner: User) -> None:
    with platform_scope(reason="test"), pytest.raises(PreparationError):
        file_return(preparation, actor=org_owner, reference="ACK1")


def test_filing_needs_a_reference(
    preparation: ReturnPreparation, org_owner: User, checker: User
) -> None:
    with platform_scope(reason="test"):
        mark_prepared(preparation, actor=org_owner)
        review(preparation, actor=checker)
        approve(preparation, actor=org_owner)

        with pytest.raises(PreparationError):
            file_return(preparation, actor=org_owner, reference="   ")


def test_filing_moves_the_obligation_with_it(
    preparation: ReturnPreparation,
    obligation: ObligationInstance,
    org_owner: User,
    checker: User,
) -> None:
    """A preparation marked filed while its obligation says "ready" is split-brain.

    The dashboard is the product; two halves of it disagreeing is worse than
    either being wrong.
    """
    with platform_scope(reason="test"):
        mark_prepared(preparation, actor=org_owner)
        review(preparation, actor=checker)
        approve(preparation, actor=org_owner)
        file_return(
            preparation,
            actor=org_owner,
            reference="AA240526000123X",
            filed_on=AS_OF,
        )
        preparation.refresh_from_db()
        obligation.refresh_from_db()

    assert preparation.state == PreparationState.FILED
    assert obligation.state == "FILED"
    assert obligation.filing_reference == "AA240526000123X"


def test_working_papers_are_one_per_obligation(
    obligation: ObligationInstance,
) -> None:
    with platform_scope(reason="test"):
        first, created_first = get_or_create_for(obligation)
        second, created_second = get_or_create_for(obligation)

    assert created_first
    assert not created_second
    assert first.pk == second.pk


# ===========================================================================
# Over HTTP
# ===========================================================================


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    return sign_in(client, org_owner, step_up=True)


def test_the_list_renders_both_ways(signed_in: Client, preparation: ReturnPreparation) -> None:
    page = signed_in.get(reverse("returns:list"))
    fragment = signed_in.get(reverse("returns:list"), headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert preparation.period_key.encode() in fragment.content


def test_the_detail_page_hides_the_review_button_from_the_preparer(
    signed_in: Client, preparation: ReturnPreparation, org_owner: User
) -> None:
    """And says why, rather than silently omitting the control."""
    with platform_scope(reason="test"):
        mark_prepared(preparation, actor=org_owner)

    response = signed_in.get(
        reverse("returns:detail", args=[preparation.pk]), headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert b"somebody else has to review it" in response.content


def test_the_list_has_a_bounded_query_count(
    signed_in: Client,
    preparation: ReturnPreparation,
    django_assert_max_num_queries: object,
) -> None:
    """Unexplained counts come from prefetched differences, not a query per row."""
    signed_in.get(reverse("returns:list"))

    with django_assert_max_num_queries(14):  # type: ignore[operator]
        response = signed_in.get(reverse("returns:list"))
    assert response.status_code == 200
