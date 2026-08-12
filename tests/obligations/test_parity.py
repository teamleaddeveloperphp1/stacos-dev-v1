"""
Python and SQL must agree about what "overdue" means.

The same rule is implemented twice on purpose — once in
:mod:`stacos.engine.lifecycle` so the pure engine can reason about it without a
database, and once as a ``Case`` annotation in :mod:`stacos.obligations.queries`
so a list of two hundred rows costs one query instead of two hundred.

Two implementations of one rule is a real risk. This is the mitigation: a fixture
matrix spanning every state against every interesting date, asserted equal on
both sides. A clause added to one and forgotten in the other fails here.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stacos.core.scope import platform_scope
from stacos.engine.lifecycle import State, derive_display_status, filed_late, is_overdue
from stacos.obligations.models import ObligationInstance
from stacos.obligations.queries import annotate_status
from stacos.tenancy.models import Entity, Tenant

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 8, 12)

#: Every date that could change the answer, plus two that should not.
DATE_CASES: tuple[date | None, ...] = (
    None,
    AS_OF - timedelta(days=365),
    AS_OF - timedelta(days=1),
    AS_OF,
    AS_OF + timedelta(days=1),
    AS_OF + timedelta(days=7),
    AS_OF + timedelta(days=8),
    AS_OF + timedelta(days=400),
)


@pytest.fixture
def matrix(org: Tenant, entity_a: Entity) -> list[ObligationInstance]:
    """One obligation for every (state, due date) pair.

    Built once and read by both parity tests: the point is coverage of the
    decision surface, not realism.
    """
    rows: list[ObligationInstance] = []
    with platform_scope(reason="test-fixture"):
        for state_index, state in enumerate(State):
            for date_index, due in enumerate(DATE_CASES):
                rows.append(
                    ObligationInstance.objects.create(
                        tenant=org,
                        entity=entity_a,
                        definition_code="IN-TEST-PARITY",
                        definition_version=1,
                        period_key=f"P{state_index:02d}{date_index:02d}",
                        period_label="Parity fixture",
                        title="Parity fixture",
                        category="TAX_DIRECT",
                        state=state,
                        due_date=due,
                        # The FILED/CLOSED check constraint requires a date, and
                        # a filed instance without one could not be tested for
                        # lateness anyway.
                        filed_on=(AS_OF if state in {State.FILED, State.CLOSED} else None),
                    )
                )
    return rows


def test_overdue_agrees_between_python_and_sql(matrix: list[ObligationInstance]) -> None:
    with platform_scope(reason="test"):
        annotated = annotate_status(
            ObligationInstance.objects.filter(definition_code="IN-TEST-PARITY"), as_of=AS_OF
        )
        disagreements = [
            (row.state, row.due_date, row.is_overdue)
            for row in annotated
            if row.is_overdue != is_overdue(state=row.state, due_date=row.due_date, as_of=AS_OF)
        ]

    assert not disagreements, (
        f"SQL and Python disagree about overdue for {len(disagreements)} case(s): "
        f"{disagreements[:5]}"
    )


def test_display_status_agrees_between_python_and_sql(
    matrix: list[ObligationInstance],
) -> None:
    """The word shown to a user is the same whichever side computed it."""
    with platform_scope(reason="test"):
        annotated = annotate_status(
            ObligationInstance.objects.filter(definition_code="IN-TEST-PARITY"), as_of=AS_OF
        )
        disagreements = [
            {
                "state": row.state,
                "due": row.due_date,
                "sql": row.display_status,
                "python": str(
                    derive_display_status(state=row.state, due_date=row.due_date, as_of=AS_OF)
                ),
            }
            for row in annotated
            if row.display_status
            != str(derive_display_status(state=row.state, due_date=row.due_date, as_of=AS_OF))
        ]

    assert not disagreements, (
        f"SQL and Python disagree about display status for {len(disagreements)} "
        f"case(s): {disagreements[:5]}"
    )


def test_filed_late_agrees_between_python_and_sql(matrix: list[ObligationInstance]) -> None:
    with platform_scope(reason="test"):
        annotated = annotate_status(
            ObligationInstance.objects.filter(definition_code="IN-TEST-PARITY"), as_of=AS_OF
        )
        disagreements = [
            (row.state, row.filed_on, row.due_date)
            for row in annotated
            if row.filed_late != filed_late(filed_on=row.filed_on, due_date=row.due_date)
        ]

    assert not disagreements, f"filed_late disagrees for {disagreements[:5]}"


def test_days_to_due_is_an_integer_not_a_duration(matrix: list[ObligationInstance]) -> None:
    """Regression: PostgreSQL returns ``date - date`` as an integer.

    Django models that subtraction as a duration and installs an interval
    converter, which then fails on the integer that actually comes back — and it
    fails at fetch time, a long way from where the annotation was written.
    """
    with platform_scope(reason="test"):
        rows = list(
            annotate_status(
                ObligationInstance.objects.filter(definition_code="IN-TEST-PARITY"),
                as_of=AS_OF,
            )
        )

    for row in rows:
        if row.due_date is None:
            assert row.days_to_due is None
        else:
            assert isinstance(row.days_to_due, int)
            assert row.days_to_due == (row.due_date - AS_OF).days
