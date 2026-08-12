"""
Materialisation, against the live catalog.

The properties here are the ones the product's credibility rests on: the calendar
is idempotent, it fans out per registration, it never destroys work, and it does
not resurrect what a user dismissed.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stacos.catalog.models import ComplianceDefinition, DefinitionVersion
from stacos.core.scope import platform_scope
from stacos.engine.lifecycle import State
from stacos.obligations.models import (
    EntityEvent,
    MaterialisationRun,
    ObligationEvent,
    ObligationInstance,
    ObligationSuppression,
)
from stacos.obligations.services import StaleCatalogError, apply_plan, materialise, preview
from stacos.tenancy.models import Entity, EntityProfile, EntityRegistration
from tests.conftest import AS_OF

pytestmark = pytest.mark.django_db


# ===========================================================================
# The core property
# ===========================================================================


def test_materialisation_is_idempotent(manufacturer: Entity) -> None:
    """Planning against the state a plan produces yields an empty plan.

    The nightly job runs this against every entity every night. A plan that is
    not idempotent produces duplicate obligations at a rate nobody notices until
    a client does.
    """
    with platform_scope(reason="test"):
        first = materialise(manufacturer, as_of=AS_OF, trigger="ONBOARDING")
        assert first.created_count > 0

        second = materialise(manufacturer, as_of=AS_OF, trigger="NIGHTLY")
        assert second.created_count == 0
        assert second.updated_count == 0
        assert second.superseded_count == 0
        assert second.archived_count == 0
        assert second.summary() == "no changes"


def test_the_calendar_is_not_empty_for_a_real_entity(materialised: Entity) -> None:
    """A working catalog produces a working calendar.

    Deliberately a floor rather than an exact count: pinning the number here
    would break on every legitimate catalog addition, and the golden-file suite
    is where exact expectations belong.
    """
    with platform_scope(reason="test"):
        count = ObligationInstance.objects.filter(entity=materialised).count()
    assert count > 100, f"only {count} obligations for a full-profile manufacturer"


# ===========================================================================
# Fan-out
# ===========================================================================


def test_gstr3b_fans_out_per_registration(materialised: Entity) -> None:
    """Two GSTINs means two GSTR-3Bs a month, not one.

    Getting this wrong is not a rendering bug. It is one missing filing per state
    per month, discovered when the department issues a notice.
    """
    with platform_scope(reason="test"):
        rows = ObligationInstance.objects.filter(
            entity=materialised, definition_code="IN-GST-GSTR3B-MONTHLY"
        )
        by_period: dict[str, set[str]] = {}
        for row in rows:
            by_period.setdefault(row.period_key, set()).add(row.scope_ref)

    assert by_period, "no GSTR-3B materialised at all"
    assert all(len(refs) == 2 for refs in by_period.values()), (
        f"expected two registrations per period, got { {k: len(v) for k, v in by_period.items()} }"
    )


def test_registration_scoped_rows_carry_a_readable_label(materialised: Entity) -> None:
    """A row that says which GSTIN it is for, without a join per row."""
    with platform_scope(reason="test"):
        row = ObligationInstance.objects.filter(
            entity=materialised, definition_code="IN-GST-GSTR3B-MONTHLY"
        ).first()
    assert row is not None
    assert row.scope_label
    assert row.scope_jurisdiction in {"IN-GJ", "IN-MH"}


def test_entity_scoped_rows_have_no_scope_reference(materialised: Entity) -> None:
    with platform_scope(reason="test"):
        row = ObligationInstance.objects.filter(
            entity=materialised, definition_code="IN-MCA-DPT3"
        ).first()
    assert row is not None
    assert row.scope_ref == ""


# ===========================================================================
# Dates
# ===========================================================================


def test_the_horizon_filters_on_the_due_date_not_the_period(materialised: Entity) -> None:
    """An annual return whose period closed before the window still appears.

    GSTR-9 for FY 2025-26 closed on 31 March 2026 and falls due 31 December 2026.
    Filtering the horizon by period rather than by due date silently loses every
    annual return, which is the failure this asserts against.
    """
    with platform_scope(reason="test"):
        row = ObligationInstance.objects.filter(
            entity=materialised,
            definition_code="IN-GST-GSTR9",
            period_key="FY2025-26",
        ).first()

    assert row is not None, "GSTR-9 for a closed period was dropped from the horizon"
    assert row.due_date == date(2026, 12, 31)
    assert row.period_end < row.due_date


def test_statutory_dates_do_not_shift_off_a_sunday(materialised: Entity) -> None:
    """Indian tax dates are wrong by law if they move for a weekend.

    The portals accept filings on a Sunday. Relief comes through explicit
    government extensions, never through the calendar.
    """
    with platform_scope(reason="test"):
        rows = ObligationInstance.objects.filter(
            entity=materialised, definition_code="IN-GST-GSTR3B-MONTHLY"
        )
        days = {row.due_date.day for row in rows if row.due_date}

    assert days == {20}, f"GSTR-3B fell on days other than the 20th: {sorted(days)}"


def test_tds_quarter_four_uses_its_own_date(materialised: Entity) -> None:
    """January–March is due 31 May; the other three quarters one month after close.

    Expressed as a month-keyed offset override rather than a second definition,
    because two copies of one applicability rule drift apart within a year.
    """
    with platform_scope(reason="test"):
        rows = {
            row.period_key: row.due_date
            for row in ObligationInstance.objects.filter(
                entity=materialised, definition_code="IN-TDS-26Q"
            )
        }

    assert rows.get("FY2026-27-Q1") == date(2026, 7, 31)
    assert rows.get("FY2025-26-Q4") == date(2026, 5, 31)


def test_an_unresolvable_date_materialises_with_a_prompt(materialised: Entity) -> None:
    """No AGM recorded means AOC-4 has no date, and says so.

    A calendar that tells you what it does not know is a feature. A guessed date
    for a statutory filing is not.
    """
    with platform_scope(reason="test"):
        row = ObligationInstance.objects.filter(
            entity=materialised, definition_code="IN-MCA-AOC4"
        ).first()

    assert row is not None, "AOC-4 was dropped rather than materialised unscheduled"
    assert row.due_date is None
    assert row.needs_input == "AGM_DATE"


def test_recording_the_event_schedules_the_obligation(materialised: Entity) -> None:
    """Answering the prompt produces a date on the next rebuild."""
    with platform_scope(reason="test"):
        EntityEvent.objects.create(
            tenant=materialised.tenant,
            entity=materialised,
            key="AGM_DATE",
            occurred_on=date(2026, 9, 25),
        )
        materialise(materialised, as_of=AS_OF, trigger="MANUAL")

        row = ObligationInstance.objects.filter(
            entity=materialised, definition_code="IN-MCA-AOC4"
        ).first()

    assert row is not None
    assert row.due_date == date(2026, 10, 25)
    assert row.needs_input == ""


# ===========================================================================
# Applicability
# ===========================================================================


def test_an_undecided_rule_materialises_unconfirmed(materialised: Entity) -> None:
    """A missing fact yields UNKNOWN, and UNKNOWN is shown, not dropped.

    Silently dropping an obligation because a question was never asked is the
    failure mode that gets a client penalised.
    """
    with platform_scope(reason="test"):
        unconfirmed = ObligationInstance.objects.filter(entity=materialised, confirmed=False)
        assert unconfirmed.exists()
        assert all(not row.reasons or row.reasons for row in unconfirmed)


def test_a_definitively_false_rule_produces_nothing(materialised: Entity) -> None:
    """The manufacturer is not a composition dealer, so CMP-08 must be absent.

    ``all`` short-circuits to FALSE despite unknowns, which is what keeps
    onboarding from becoming an interrogation.
    """
    with platform_scope(reason="test"):
        assert not ObligationInstance.objects.filter(
            entity=materialised, definition_code="IN-GST-CMP08"
        ).exists()


def test_reasons_explain_why_an_obligation_applies(materialised: Entity) -> None:
    """ "Why is this on my calendar" always has an answer."""
    with platform_scope(reason="test"):
        row = ObligationInstance.objects.filter(
            entity=materialised, definition_code="IN-GST-GSTR3B-MONTHLY"
        ).first()

    assert row is not None
    assert row.reasons
    assert any("GST" in reason for reason in row.reasons)


def test_a_profile_change_removes_what_no_longer_applies(manufacturer: Entity) -> None:
    """Surrendering a registration stops the filings that depended on it."""
    with platform_scope(reason="test"):
        materialise(manufacturer, as_of=AS_OF, trigger="ONBOARDING")
        before = ObligationInstance.objects.filter(
            entity=manufacturer, definition_code="IN-GST-GSTR3B-MONTHLY"
        ).count()

        EntityRegistration.objects.filter(
            entity=manufacturer, type="GST", jurisdiction="IN-MH"
        ).update(valid_to=date(2026, 1, 31))

        materialise(manufacturer, as_of=AS_OF, trigger="PROFILE_CHANGE")
        after = ObligationInstance.objects.filter(
            entity=manufacturer,
            definition_code="IN-GST-GSTR3B-MONTHLY",
            archived_at__isnull=True,
            superseded_at__isnull=True,
        ).count()

    assert after < before, "surrendering a GSTIN did not stop its filings"


# ===========================================================================
# Nothing with history is destroyed
# ===========================================================================


def test_an_obligation_with_history_is_superseded_not_deleted(manufacturer: Entity) -> None:
    """Work already done survives a profile change.

    The nightly rebuild must not be able to erase a client's audit trail. That is
    not a bug, it is a breach.
    """
    with platform_scope(reason="test"):
        materialise(manufacturer, as_of=AS_OF, trigger="ONBOARDING")

        row = (
            ObligationInstance.objects.filter(
                entity=manufacturer,
                definition_code="IN-GST-GSTR3B-MONTHLY",
                scope_jurisdiction="IN-MH",
            )
            .order_by("due_date")
            .first()
        )
        assert row is not None
        row.state = State.IN_PREPARATION
        row.save(update_fields=["state"])
        ObligationEvent.objects.create(
            tenant=manufacturer.tenant,
            entity=manufacturer,
            obligation=row,
            kind=ObligationEvent.Kind.TRANSITION,
            from_state=State.NOT_STARTED,
            to_state=State.IN_PREPARATION,
            actor_label="A person",
        )

        # Remove the registration this filing depended on.
        EntityRegistration.objects.filter(
            entity=manufacturer, type="GST", jurisdiction="IN-MH"
        ).delete()

        materialise(manufacturer, as_of=AS_OF, trigger="PROFILE_CHANGE")
        row.refresh_from_db()

    assert row.superseded_at is not None, "an instance with history was destroyed"
    assert row.supersede_reason
    assert row.state == State.NOT_APPLICABLE

    # Retained, but out of the working calendar: the audit trail keeps it, the
    # morning work queue does not show it.
    with platform_scope(reason="test"):
        from stacos.obligations.queries import live

        assert ObligationInstance.objects.filter(pk=row.pk).exists()
        assert not live().filter(pk=row.pk).exists()


def test_a_filed_obligation_keeps_its_dates(manufacturer: Entity) -> None:
    """A terminal instance is frozen.

    A retroactive change must never reclassify a filing that was late as on time,
    or the record stops being evidence of anything.
    """
    with platform_scope(reason="test"):
        materialise(manufacturer, as_of=AS_OF, trigger="ONBOARDING")
        row = (
            ObligationInstance.objects.filter(
                entity=manufacturer, definition_code="IN-GST-GSTR3B-MONTHLY"
            )
            .order_by("due_date")
            .first()
        )
        assert row is not None
        original_due = row.due_date

        row.state = State.FILED
        row.filed_on = date(2026, 5, 2)
        row.due_date = original_due
        row.save(update_fields=["state", "filed_on", "due_date"])

        materialise(manufacturer, as_of=AS_OF, trigger="NIGHTLY")
        row.refresh_from_db()

    assert row.due_date == original_due
    assert row.state == State.FILED


# ===========================================================================
# User overrides
# ===========================================================================


def test_a_dismissed_obligation_is_not_resurrected(materialised: Entity) -> None:
    """A nightly job that brings back what a user dismissed destroys trust.

    Faster than any bug, and it is the whole reason suppressions are an *input*
    to the planner rather than a consequence of it.
    """
    with platform_scope(reason="test"):
        row = ObligationInstance.objects.filter(
            entity=materialised, definition_code="IN-GST-GSTR9"
        ).first()
        assert row is not None

        ObligationSuppression.objects.create(
            tenant=materialised.tenant,
            entity=materialised,
            definition_code=row.definition_code,
            scope_ref=row.scope_ref,
            period_key=row.period_key,
            kind=ObligationSuppression.Kind.NOT_APPLICABLE,
            reason="Our accounts say this does not apply to us.",
        )
        row.delete()

        materialise(materialised, as_of=AS_OF, trigger="NIGHTLY")

        resurrected = ObligationInstance.objects.filter(
            entity=materialised,
            definition_code="IN-GST-GSTR9",
            period_key=row.period_key,
            scope_ref=row.scope_ref,
        ).exists()

    assert not resurrected, "the nightly rebuild resurrected a dismissed obligation"


# ===========================================================================
# Guards around applying a plan
# ===========================================================================


def test_applying_a_stale_plan_fails_loudly(manufacturer: Entity) -> None:
    """A catalog publication between preview and apply is not silently accepted.

    The window is small. Writing dates the user never saw is not.
    """
    with platform_scope(reason="test"):
        plan = preview(manufacturer, as_of=AS_OF)
        with pytest.raises(StaleCatalogError):
            apply_plan(
                manufacturer,
                plan,
                trigger="MANUAL",
                expect_fingerprint="a-different-fingerprint",
            )


def test_a_run_is_recorded_with_its_fingerprint(materialised: Entity) -> None:
    """ "Why did forty-three obligations appear last Tuesday" has an answer."""
    with platform_scope(reason="test"):
        run = MaterialisationRun.objects.filter(entity=materialised).first()

    assert run is not None
    assert run.catalog_fingerprint
    assert run.created_count > 0
    assert run.horizon_end > run.horizon_start
    assert run.summary() != "no changes"


def test_materialisation_records_an_event_per_instance(materialised: Entity) -> None:
    """Every row can say where it came from."""
    with platform_scope(reason="test"):
        instances = ObligationInstance.objects.filter(entity=materialised).count()
        events = ObligationEvent.objects.filter(
            entity=materialised, kind=ObligationEvent.Kind.MATERIALISED
        ).count()

    assert events == instances


# ===========================================================================
# Catalog integration
# ===========================================================================


def test_the_published_catalog_loaded(db: None) -> None:
    with platform_scope(reason="test"):
        assert ComplianceDefinition.objects.filter(country="IN").count() >= 100
        assert DefinitionVersion.objects.filter(status="PUBLISHED").exists()


def test_no_published_version_has_a_stale_review(db: None) -> None:
    """Legal guidance older than a year fails the build.

    ``plain_language_summary`` is what a client acts on. Shipping it with a
    two-year-old review is worse than shipping nothing.
    """
    with platform_scope(reason="test"):
        stale = [
            version.definition.code
            for version in DefinitionVersion.objects.select_related("definition").filter(
                status="PUBLISHED"
            )
            if version.review_is_stale
        ]
    assert not stale, f"definitions with a stale or missing statutory review: {stale}"


def test_a_dormant_entity_gets_a_thin_calendar(org: object) -> None:
    """The floor case: a dormant holding company owes the statutory minimum.

    Not "almost nothing" — a dormant company still holds an AGM, files AOC-4 and
    MGT-7A, renews DIR-3 KYC and files a return. What it must *not* have is
    anything that follows from trading: GST returns, payroll, a factory. The
    assertion is therefore about which families appear rather than an absolute
    count, because a count moves with every legitimate catalog addition.
    """
    with platform_scope(reason="test"):
        entity = Entity.objects.create(
            tenant=org,
            name="Meridian Holdings Pvt Ltd",
            short_code="MHPL",
            entity_type="PVT_LTD",
            country="IN",
            incorporation_date=date(2017, 3, 28),
            registered_office_state="IN-MH",
        )
        EntityProfile.objects.create(
            tenant=org,
            entity=entity,
            aggregate_turnover=0,
            employee_count=0,
            paid_up_capital=100000,
            states_of_operation=["IN-MH"],
            facts={"is_dormant": True, "is_listed": False},
        )
        materialise(entity, as_of=AS_OF, trigger="ONBOARDING")
        codes = set(
            ObligationInstance.objects.filter(entity=entity).values_list(
                "definition_code", flat=True
            )
        )

    # What trading brings, and a dormant company therefore must not have.
    forbidden = {
        "IN-GST-GSTR1-MONTHLY",
        "IN-GST-GSTR3B-MONTHLY",
        "IN-GST-GSTR9",
        "IN-EPF-ECR",
        "IN-ESIC-CONTRIBUTION",
        "IN-LABOUR-FACTORY-ANNUAL-RETURN",
        "IN-IT-ADVANCE-TAX-Q1",
        "IN-MCA-BOARD-MEETING",
    }
    leaked = sorted(codes & forbidden)
    assert not leaked, f"a dormant company was given trading obligations: {leaked}"

    # And what it genuinely still owes.
    expected = {"IN-MCA-AGM", "IN-MCA-AOC4", "IN-MCA-DIR3-KYC", "IN-MCA-DPT3"}
    missing = sorted(expected - codes)
    assert not missing, f"a dormant company is missing statutory filings: {missing}"


def test_horizon_covers_roughly_eighteen_months(materialised: Entity) -> None:
    with platform_scope(reason="test"):
        run = MaterialisationRun.objects.filter(entity=materialised).first()
    assert run is not None
    span = (run.horizon_end - run.horizon_start).days
    assert timedelta(days=600).days < span < timedelta(days=760).days
