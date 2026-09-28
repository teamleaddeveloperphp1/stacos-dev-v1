"""
Obligations an entity writes for itself.

The claim under test is that a custom obligation is a *definition*, not a
reminder: its instances are ordinary register rows that the planner creates,
dates, keeps idempotent and retires exactly as it does a catalog filing's — and
that editing or withdrawing one never destroys what it already produced.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.catalog.loader import iter_documents, parse_document
from stacos.catalog.validation import _check_structure
from stacos.core.models import AuditAction, AuditLog
from stacos.core.scope import platform_scope, tenant_context
from stacos.engine.lifecycle import State
from stacos.obligations.custom import (
    CustomObligationError,
    create_custom_obligation,
    custom_snapshots,
    update_custom_obligation,
    withdraw_custom_obligation,
)
from stacos.obligations.library import LibraryError, build_library, remove_definition
from stacos.obligations.models import (
    CustomObligation,
    CustomObligationVersion,
    ObligationEvent,
    ObligationInstance,
)
from stacos.obligations.services import materialise, preview
from stacos.obligations.transitions import ensure_steps, outstanding_mandatory_evidence
from stacos.tenancy.models import Entity, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 8, 12)

MONTHLY_ON_THE_7TH: dict[str, Any] = {
    "periodicity": "MONTHLY",
    "period_anchor": "FY",
    "due_mode": CustomObligationVersion.DueMode.DAY_OF_NEXT_MONTH,
    "due_days": 0,
    "due_day_of_month": 7,
    "shift_to_working_day": False,
}

COVENANT: dict[str, Any] = {
    "title": "DSCR covenant certificate",
    "description": "Send the lender a certificate of the debt service coverage ratio.",
    "source_reference": "Facility agreement cl. 14.2",
    "consequence": "Event of default after 30 days.",
    "category": "INTERNAL_GOVERNANCE",
    "evidence_labels": ["Signed certificate"],
    "evidence_mandatory": True,
}


def _create(entity: Entity, actor: Any = None, **overrides: Any) -> CustomObligation:
    return create_custom_obligation(
        entity,
        actor=actor,
        details={**COVENANT, **overrides.pop("details", {})},
        schedule={**MONTHLY_ON_THE_7TH, **overrides.pop("schedule", {})},
        starts_on=overrides.pop("starts_on", date(2026, 4, 1)),
        as_of=AS_OF,
    )


def _instances(obligation: CustomObligation, **filters: Any) -> list[ObligationInstance]:
    return list(
        ObligationInstance.objects.filter(
            entity_id=obligation.entity_id, definition_code=obligation.code, **filters
        ).order_by("period_start")
    )


def _by_period(obligation: CustomObligation) -> dict[str, ObligationInstance]:
    return {row.period_key: row for row in _instances(obligation, archived_at__isnull=True)}


# ---------------------------------------------------------------------------
# It behaves like any other obligation
# ---------------------------------------------------------------------------


def test_creating_one_puts_recurring_instances_on_the_calendar(
    materialised: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
        rows = _by_period(obligation)

        assert obligation.code.startswith("CUSTOM-")
        september = rows["2026-09"]
        assert september.due_date == date(2026, 10, 7)
        assert september.title == COVENANT["title"]
        assert september.category == "INTERNAL_GOVERNANCE"
        assert september.state == State.NOT_STARTED
        # Nothing to be unsure about: the entity wrote the rule itself.
        assert september.confirmed
        assert not september.missing_facts
        # One per month across the horizon, not a single reminder.
        assert len(rows) > 12
        assert AuditLog.objects.filter(
            action=AuditAction.CREATE, object_id=str(obligation.pk)
        ).exists()


def test_its_instances_carry_the_same_checklist_and_evidence_guard(
    materialised: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
        instance = _by_period(obligation)["2026-09"]

        assert [step.key for step in ensure_steps(instance)] == [
            "prepare",
            "review",
            "approve",
            "file",
        ]
        assert outstanding_mandatory_evidence(instance) == ["Signed certificate"]


def test_a_rebuild_after_creating_one_changes_nothing(
    materialised: Entity, org_owner: User
) -> None:
    """Idempotence, the planner's first invariant, holds for these too."""
    with platform_scope(reason="test"):
        _create(materialised, org_owner)
        assert preview(materialised, as_of=AS_OF).is_empty


def test_the_catalog_refuses_a_code_in_the_reserved_namespace() -> None:
    path, raw = next(iter_documents())
    document = parse_document(path, raw)
    assert not any(f.check == "reserved-code" for f in _check_structure(document))

    document.code = "CUSTOM-SOMETHING"
    assert any(f.check == "reserved-code" for f in _check_structure(document))


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def test_editing_details_renames_open_instances_but_not_completed_ones(
    materialised: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
        done = _by_period(obligation)["2026-05"]
        ObligationInstance.objects.filter(pk=done.pk).update(
            state=State.FILED, filed_on=date(2026, 6, 5)
        )

        update_custom_obligation(
            obligation,
            actor=org_owner,
            details={**COVENANT, "title": "DSCR certificate to lender", "category": "LICENSING"},
            schedule=MONTHLY_ON_THE_7TH,
            applies_from=None,
            as_of=AS_OF,
        )

        rows = _by_period(obligation)
        assert rows["2026-09"].title == "DSCR certificate to lender"
        assert rows["2026-05"].title == COVENANT["title"]
        # Category is an access boundary, so every row follows it.
        assert {row.category for row in rows.values()} == {"LICENSING"}
        assert obligation.versions.count() == 1


def test_a_schedule_change_keeps_earlier_periods_on_the_old_rule(
    materialised: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)

        new_version = update_custom_obligation(
            obligation,
            actor=org_owner,
            details=COVENANT,
            schedule={
                **MONTHLY_ON_THE_7TH,
                "due_mode": CustomObligationVersion.DueMode.DAYS_AFTER_PERIOD,
                "due_days": 15,
                "due_day_of_month": None,
            },
            applies_from=date(2026, 10, 15),
            as_of=AS_OF,
        )

        assert new_version is not None and new_version.version == 2
        rows = _by_period(obligation)
        # Ended before the change: the rule that dated it still does.
        assert rows["2026-09"].due_date == date(2026, 10, 7)
        assert rows["2026-09"].definition_version == 1
        # In progress on the change date, and after: the new rule.
        assert rows["2026-10"].due_date == date(2026, 11, 15)
        assert rows["2026-10"].definition_version == 2
        # One October, not one under each rule.
        assert len(_instances(obligation, period_key="2026-10", archived_at__isnull=True)) == 1
        assert preview(materialised, as_of=AS_OF).is_empty


def test_a_frequency_change_follows_the_rule_in_force_when_each_period_closes(
    materialised: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
        update_custom_obligation(
            obligation,
            actor=org_owner,
            details=COVENANT,
            schedule={
                **MONTHLY_ON_THE_7TH,
                "periodicity": "QUARTERLY",
                "due_mode": CustomObligationVersion.DueMode.DAYS_AFTER_PERIOD,
                "due_days": 30,
                "due_day_of_month": None,
            },
            applies_from=date(2026, 11, 15),
            as_of=AS_OF,
        )

        rows = _by_period(obligation)
        assert "2026-10" in rows  # closed before the change
        assert "2026-11" not in rows  # would have closed after it, as a month
        quarter = next(
            row
            for row in rows.values()
            if (row.period_start, row.period_end) == (date(2026, 10, 1), date(2026, 12, 31))
        )
        assert quarter.definition_version == 2
        assert quarter.due_date == date(2027, 1, 30)


def test_a_schedule_change_cannot_reach_back_before_the_current_one(
    materialised: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner, starts_on=date(2026, 6, 1))
        with pytest.raises(CustomObligationError):
            update_custom_obligation(
                obligation,
                actor=org_owner,
                details=COVENANT,
                schedule={**MONTHLY_ON_THE_7TH, "due_day_of_month": 10},
                applies_from=date(2026, 5, 1),
                as_of=AS_OF,
            )


def test_a_schedule_change_from_the_very_start_retires_the_first_version(
    materialised: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner, starts_on=date(2026, 6, 1))
        update_custom_obligation(
            obligation,
            actor=org_owner,
            details=COVENANT,
            schedule={**MONTHLY_ON_THE_7TH, "due_day_of_month": 10},
            applies_from=date(2026, 6, 1),
            as_of=AS_OF,
        )

        assert [s.version for s in custom_snapshots(materialised)] == [2]
        assert all(row.due_date.day == 10 for row in _by_period(obligation).values())


# ---------------------------------------------------------------------------
# Withdrawing
# ---------------------------------------------------------------------------


def test_withdrawing_keeps_everything_it_already_produced(
    materialised: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
        rows = _by_period(obligation)
        filed, worked, untouched = rows["2026-05"], rows["2026-08"], rows["2026-12"]
        ObligationInstance.objects.filter(pk=filed.pk).update(
            state=State.FILED, filed_on=date(2026, 6, 5)
        )
        ObligationEvent.objects.create(
            tenant=materialised.tenant,
            entity=materialised,
            obligation=worked,
            kind=ObligationEvent.Kind.NOTE,
            note="Asked the CFO for the ratio.",
        )

        withdraw_custom_obligation(
            obligation, actor=org_owner, reason="Loan repaid early.", as_of=AS_OF
        )

        filed.refresh_from_db()
        worked.refresh_from_db()
        untouched.refresh_from_db()
        assert filed.state == State.FILED and filed.superseded_at is None
        assert worked.superseded_at is not None
        assert worked.supersede_reason == "Withdrawn: Loan repaid early."
        assert untouched.archived_at is not None
        assert not custom_snapshots(materialised)
        assert AuditLog.objects.filter(
            action=AuditAction.ARCHIVE, object_id=str(obligation.pk)
        ).exists()


def test_a_superseded_row_is_not_superseded_again_on_every_rebuild(
    materialised: Entity, org_owner: User
) -> None:
    """Regression: every replan used to stamp a fresh supersession and append
    another "no longer applicable" entry to the row's timeline — nightly."""
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
        worked = _by_period(obligation)["2026-08"]
        ObligationEvent.objects.create(
            tenant=materialised.tenant,
            entity=materialised,
            obligation=worked,
            kind=ObligationEvent.Kind.NOTE,
            note="Started.",
        )
        withdraw_custom_obligation(obligation, actor=org_owner, reason="Repaid.", as_of=AS_OF)

        materialise(materialised, as_of=AS_OF)
        materialise(materialised, as_of=AS_OF)

        assert (
            ObligationEvent.objects.filter(
                obligation=worked, kind=ObligationEvent.Kind.SUPERSEDED
            ).count()
            == 1
        )


def test_a_withdrawn_obligation_cannot_be_edited_or_withdrawn_again(
    materialised: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
        withdraw_custom_obligation(obligation, actor=org_owner, reason="Done.", as_of=AS_OF)

        with pytest.raises(CustomObligationError):
            withdraw_custom_obligation(obligation, actor=org_owner, reason="Again.", as_of=AS_OF)
        with pytest.raises(CustomObligationError):
            update_custom_obligation(
                obligation,
                actor=org_owner,
                details={**COVENANT, "title": "Renamed"},
                schedule=MONTHLY_ON_THE_7TH,
                applies_from=None,
                as_of=AS_OF,
            )


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------


def test_it_sits_in_the_library_beside_the_catalog(materialised: Entity, org_owner: User) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
        rows = build_library(materialised, as_of=AS_OF)

        own = next(row for row in rows if row.code == obligation.code)
        assert own.is_custom
        assert own.state == "added"
        assert own.occurrence is not None
        assert "Monthly" in own.schedule
        assert any(not row.is_custom for row in rows)

        withdraw_custom_obligation(obligation, actor=org_owner, reason="Done.", as_of=AS_OF)
        own = next(row for row in build_library(materialised, as_of=AS_OF) if row.is_custom)
        assert own.state == "removed"


def test_the_library_stays_bounded_with_custom_obligations(
    materialised: Entity, org_owner: User, django_assert_max_num_queries: Any
) -> None:
    with platform_scope(reason="test"):
        for index in range(3):
            _create(materialised, org_owner, details={"title": f"Control {index}"})
        with django_assert_max_num_queries(15):
            build_library(materialised, as_of=AS_OF)


def test_catalog_actions_refuse_a_custom_obligation(materialised: Entity, org_owner: User) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
        with pytest.raises(LibraryError):
            remove_definition(materialised, obligation.code, actor=org_owner, reason="x")


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


def test_a_category_limited_member_sees_only_their_own_categories(
    materialised: Entity, org: Tenant
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised)

    with tenant_context(tenant_ids=org.id, reason="test", categories=["TAX_DIRECT"]):
        assert not CustomObligation.objects.filter(pk=obligation.pk).exists()
        assert not CustomObligationVersion.objects.filter(obligation_id=obligation.pk).exists()
    with tenant_context(tenant_ids=org.id, reason="test", categories=["INTERNAL_GOVERNANCE"]):
        assert CustomObligation.objects.filter(pk=obligation.pk).exists()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _form_data(**overrides: Any) -> dict[str, Any]:
    data = {
        "title": "Monthly stock audit",
        "description": "Count and reconcile the warehouse.",
        "source_reference": "Internal control IC-7",
        "category": "INTERNAL_GOVERNANCE",
        "consequence": "",
        "periodicity": "MONTHLY",
        "period_anchor": "FY",
        "due_mode": CustomObligationVersion.DueMode.DAYS_AFTER_PERIOD,
        "due_days": "5",
        "due_day_of_month": "",
        "evidence_labels": "Count sheet\nVariance report",
        "starts_on": "2026-04-01",
    }
    data.update(overrides)
    return data


def test_creating_through_the_library_lands_on_its_own_page(
    client: Client, materialised: Entity, org_owner: User
) -> None:
    sign_in(client, org_owner)
    url = reverse("compliance:custom_create", args=[materialised.pk])

    modal = client.get(url, headers={"HX-Request": "true"})
    response = client.post(url, _form_data(), headers={"HX-Request": "true"})

    assert modal.status_code == 200
    assert response.status_code == 200
    assert response["HX-Retarget"] == "#main"
    assert b"Monthly stock audit" in response.content
    with platform_scope(reason="test"):
        obligation = CustomObligation.objects.get(entity=materialised)
        assert obligation.evidence_labels == ["Count sheet", "Variance report"]
        assert response["HX-Push-Url"] == reverse(
            "compliance:custom_detail", args=[materialised.pk, obligation.pk]
        )


def test_an_invalid_form_rerenders_with_422(
    client: Client, materialised: Entity, org_owner: User
) -> None:
    sign_in(client, org_owner)
    response = client.post(
        reverse("compliance:custom_create", args=[materialised.pk]),
        _form_data(due_days=""),
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    with platform_scope(reason="test"):
        assert not CustomObligation.objects.filter(entity=materialised).exists()


def test_its_page_renders_both_ways_within_a_fixed_query_budget(
    client: Client, materialised: Entity, org_owner: User, django_assert_max_num_queries: Any
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
    sign_in(client, org_owner)
    url = reverse("compliance:custom_detail", args=[materialised.pk, obligation.pk])

    page = client.get(url)
    with django_assert_max_num_queries(30):
        fragment = client.get(url, headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert fragment.status_code == 200
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b"Schedule history" in fragment.content


def test_its_page_lists_the_soonest_due_first(
    client: Client, materialised: Entity, org_owner: User
) -> None:
    """The next filing on top, not the one furthest out."""
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
    sign_in(client, org_owner)

    response = client.get(
        reverse("compliance:custom_detail", args=[materialised.pk, obligation.pk]),
        headers={"HX-Request": "true"},
    )
    due_dates = [row.due_date for row in response.context["instances"] if row.due_date]
    assert len(due_dates) > 1, "fixture sanity: the schedule produced several occurrences"
    assert due_dates == sorted(due_dates)


def test_edit_and_withdraw_over_http(client: Client, materialised: Entity, org_owner: User) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
    sign_in(client, org_owner)
    args = [materialised.pk, obligation.pk]

    edit_form = client.get(reverse("compliance:custom_edit", args=args))
    edited = client.post(
        reverse("compliance:custom_edit", args=args),
        _form_data(
            title="DSCR certificate",
            due_mode=CustomObligationVersion.DueMode.DAY_OF_NEXT_MONTH,
            due_days="",
            due_day_of_month="7",
            evidence_labels="Signed certificate",
            evidence_mandatory="on",
            applies_from="2026-10-01",
        ),
        headers={"HX-Request": "true"},
    )
    withdrawn = client.post(
        reverse("compliance:custom_withdraw", args=args),
        {"reason": "Loan repaid."},
        headers={"HX-Request": "true"},
    )

    assert edit_form.status_code == 200
    assert b"DSCR covenant certificate" in edit_form.content
    assert edited.status_code == 200
    assert withdrawn.status_code == 200
    with platform_scope(reason="test"):
        obligation.refresh_from_db()
        assert obligation.title == "DSCR certificate"
        assert obligation.is_withdrawn


def test_an_instance_page_says_who_wrote_the_rule(
    client: Client, materialised: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised, org_owner)
        instance = _by_period(obligation)["2026-09"]
    sign_in(client, org_owner)

    response = client.get(reverse("compliance:detail", args=[instance.pk]))

    assert response.status_code == 200
    assert b"What you wrote" in response.content
    assert b"What the law says" not in response.content
    assert b"Facility agreement cl. 14.2" in response.content
    assert b"Signed certificate" in response.content


def test_a_view_only_member_cannot_create(
    client: Client, materialised: Entity, org: Tenant
) -> None:
    from stacos.tenancy.models import Membership, Role

    with platform_scope(reason="test"):
        role = Role.objects.create(
            tenant=org,
            code="library-viewer",
            name="Library viewer",
            tenant_type=Tenant.Type.ORGANISATION,
            permissions=["compliance.library.view"],
        )
        user = User.objects.create_user(
            email="viewer@example.com",
            password="test-password-12345",
            full_name="Viewer",
            phone_e164="+919800099002",
            email_verified=True,
            phone_verified=True,
        )
        Membership.objects.create(tenant=org, user=user, role=role, status=Membership.Status.ACTIVE)
    sign_in(client, user)

    library = client.get(reverse("compliance:library", args=[materialised.pk]))
    response = client.post(
        reverse("compliance:custom_create", args=[materialised.pk]),
        _form_data(),
        headers={"HX-Request": "true"},
    )

    assert b"Add your own" not in library.content
    assert response.status_code == 403
    with platform_scope(reason="test"):
        assert not CustomObligation.objects.filter(entity=materialised).exists()


def test_another_tenant_cannot_reach_it(
    client: Client, materialised: Entity, rival_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised)
    sign_in(client, rival_owner, step_up=True)
    args = [materialised.pk, obligation.pk]

    assert client.get(reverse("compliance:custom_detail", args=args)).status_code == 404
    assert client.get(reverse("compliance:custom_edit", args=args)).status_code == 404
    assert (
        client.post(reverse("compliance:custom_withdraw", args=args), {"reason": "x"}).status_code
        == 404
    )
    with platform_scope(reason="test"):
        obligation.refresh_from_db()
        assert not obligation.is_withdrawn


def test_a_forged_obligation_id_under_the_wrong_entity_is_a_404(
    client: Client, materialised: Entity, entity_a: Entity, org_owner: User
) -> None:
    with platform_scope(reason="test"):
        obligation = _create(materialised)
    sign_in(client, org_owner)

    response = client.get(reverse("compliance:custom_detail", args=[entity_a.pk, obligation.pk]))
    assert response.status_code == 404
