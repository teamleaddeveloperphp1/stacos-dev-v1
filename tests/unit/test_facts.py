"""The fact registry — the thing that keeps applicability rules out of code."""

from __future__ import annotations

import pytest

from stacos.jurisdictions.facts import REGISTRY, FactDef, FactRegistry, FactType


def test_core_facts_are_registered() -> None:
    """These are referenced by name in applicability rules, so a rename is a
    breaking change to authored data, not a refactor."""
    for key in (
        "registrations",
        "gst_scheme",
        "aggregate_turnover",
        "employee_count",
        "entity_type",
        "states_of_operation",
    ):
        assert key in REGISTRY, f"{key} is missing from the fact registry"


def test_effective_dated_facts_are_marked() -> None:
    """Turnover is only known once books close, and gets restated afterwards.

    Facts marked here are stored with validity windows so the engine can ask
    what was true *for a period* rather than what is true today.
    """
    dated = REGISTRY.effective_dated_keys()
    assert "aggregate_turnover" in dated
    assert "employee_count" in dated
    assert "entity_type" not in dated, "an entity's type is not a time series"


def test_boolean_fact_rejects_a_string() -> None:
    definition = REGISTRY.get("has_boiler")
    assert definition is not None
    definition.validate(True)
    with pytest.raises(ValueError, match="true or false"):
        definition.validate("yes")


def test_enum_fact_rejects_an_unlisted_value() -> None:
    definition = REGISTRY.get("gst_scheme")
    assert definition is not None
    definition.validate("REGULAR")
    with pytest.raises(ValueError, match="not one of"):
        definition.validate("SOMETHING_ELSE")


def test_set_fact_rejects_unknown_members() -> None:
    definition = REGISTRY.get("states_of_operation")
    assert definition is not None
    definition.validate(["IN-GJ", "IN-MH"])
    with pytest.raises(ValueError, match="unknown values"):
        definition.validate(["IN-GJ", "XX-ZZ"])


def test_validate_facts_reports_every_problem_at_once() -> None:
    """An onboarding wizard should show all the corrections needed, not one per
    round trip."""
    problems = REGISTRY.validate_facts({"has_boiler": "yes", "gst_scheme": "NOPE"}, strict=True)
    assert len(problems) == 2


def test_unknown_facts_are_ignored_when_not_strict() -> None:
    """A profile authored against a newer catalog than this deployment knows
    about should degrade, not be refused wholesale."""
    assert REGISTRY.validate_facts({"invented_later": 1}, strict=False) == []
    assert REGISTRY.validate_facts({"invented_later": 1}, strict=True) != []


def test_registering_conflicting_definitions_is_rejected() -> None:
    registry = FactRegistry()
    registry.register(FactDef("x", "X", FactType.BOOL))
    registry.register(FactDef("x", "X", FactType.BOOL))  # identical: fine
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FactDef("x", "Different", FactType.INT))


def test_derived_facts_declare_their_dependencies() -> None:
    """The dependency list is what lets a profile edit re-evaluate only the
    definitions it could possibly have affected."""
    band = REGISTRY.get("turnover_band")
    assert band is not None
    assert "aggregate_turnover" in band.depends_on
