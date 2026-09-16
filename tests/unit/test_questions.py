"""Ranking and re-surfacing the facts worth asking an entity for."""

from __future__ import annotations

from stacos.engine.types import DefinitionSnapshot, Periodicity
from stacos.obligations.questions import answered_questions


def _snapshot(*, code: str, applicability_rule: dict[str, object]) -> DefinitionSnapshot:
    return DefinitionSnapshot(
        code=code,
        version=1,
        title=code,
        country="IN",
        periodicity=Periodicity.ANNUAL,
        due_rule={},
        applicability_rule=applicability_rule,
    )


def test_a_fact_that_decides_nothing_is_not_shown() -> None:
    """A profile fact that never turned on any rule (``entity_type`` here —
    the only definition in this catalog turns on ``aggregate_turnover``) is
    not something a person was ever asked, so it must not show up as a
    "Settles 0 rules" card cluttering the queue.
    """
    catalog = [
        _snapshot(
            code="TEST-TURNOVER",
            applicability_rule={"fact": "aggregate_turnover", "op": "gte", "value": 100},
        )
    ]
    facts = {"aggregate_turnover": 500, "entity_type": "LLP"}

    questions = answered_questions(catalog=catalog, facts=facts)

    keys = {question.key for question in questions}
    assert "entity_type" not in keys, "a fact that settles nothing should not get a card"


def test_an_answered_fact_still_reports_what_it_unlocks() -> None:
    """The fix for the disappearing card must not also flatten every count to
    zero: a fact still deciding something keeps its real "settles" number."""
    catalog = [
        _snapshot(
            code="TEST-TURNOVER",
            applicability_rule={"fact": "aggregate_turnover", "op": "gte", "value": 100},
        )
    ]
    facts = {"aggregate_turnover": 500}

    questions = answered_questions(catalog=catalog, facts=facts)

    assert len(questions) == 1
    assert questions[0].key == "aggregate_turnover"
    assert questions[0].unlocks == 1
    assert questions[0].decisive_for == ("TEST-TURNOVER",)
