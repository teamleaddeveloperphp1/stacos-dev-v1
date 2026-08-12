"""The applicability rule language."""

from __future__ import annotations

import pytest

from stacos.engine.rules import V, evaluate, facts_used, k_all, k_any, k_not, validate_rule

# The brief's own example: monthly GSTR-1 for a regular GST taxpayer.
GSTR1_RULE = {
    "all": [
        {
            "fact": "registrations",
            "op": "includes",
            "value": "GST",
            "explain": "you are registered under GST",
        },
        {"fact": "gst_scheme", "op": "eq", "value": "REGULAR"},
        {
            "any": [
                {
                    "fact": "aggregate_turnover",
                    "op": "gt",
                    "value": 50000000,
                    "explain": "annual turnover above ₹5 crore",
                },
                {"fact": "qrmp_opted", "op": "eq", "value": False},
            ]
        },
    ]
}


# ===========================================================================
# Kleene logic
# ===========================================================================


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([V.TRUE, V.TRUE], V.TRUE),
        ([V.TRUE, V.FALSE], V.FALSE),
        ([V.TRUE, V.UNKNOWN], V.UNKNOWN),
        # FALSE beats UNKNOWN, and this is load-bearing: a definitively
        # unregistered entity does not owe a GST return whatever else is unknown.
        ([V.FALSE, V.UNKNOWN], V.FALSE),
        ([V.UNKNOWN, V.UNKNOWN], V.UNKNOWN),
    ],
)
def test_conjunction_truth_table(values: list[V], expected: V) -> None:
    assert k_all(values) is expected


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([V.FALSE, V.FALSE], V.FALSE),
        ([V.TRUE, V.FALSE], V.TRUE),
        ([V.TRUE, V.UNKNOWN], V.TRUE),
        ([V.FALSE, V.UNKNOWN], V.UNKNOWN),
    ],
)
def test_disjunction_truth_table(values: list[V], expected: V) -> None:
    assert k_any(values) is expected


def test_negation_leaves_unknown_alone() -> None:
    assert k_not(V.UNKNOWN) is V.UNKNOWN
    assert k_not(V.TRUE) is V.FALSE


# ===========================================================================
# Evaluation
# ===========================================================================


def test_applies_to_a_regular_gst_taxpayer_over_the_threshold() -> None:
    verdict = evaluate(
        GSTR1_RULE,
        {
            "registrations": ["PAN", "GST", "TAN"],
            "gst_scheme": "REGULAR",
            "aggregate_turnover": 805000000,
            "qrmp_opted": True,
        },
    )
    assert verdict.result is V.TRUE
    assert verdict.applicable


def test_does_not_apply_to_a_composition_dealer() -> None:
    verdict = evaluate(
        GSTR1_RULE,
        {"registrations": ["GST"], "gst_scheme": "COMPOSITION", "aggregate_turnover": 9000000},
    )
    assert verdict.result is V.FALSE


def test_an_unregistered_entity_is_decided_despite_unknown_turnover() -> None:
    """The property that keeps onboarding short.

    Turnover is unknown, but not being GST-registered settles it — so the user is
    never asked for a figure that cannot change the answer.
    """
    verdict = evaluate(GSTR1_RULE, {"registrations": ["PAN"]})
    assert verdict.result is V.FALSE
    assert verdict.confirmed


def test_a_missing_fact_yields_unknown_not_false() -> None:
    """The single most important behaviour in this module.

    Treating "we haven't asked" as "doesn't apply" silently drops an obligation,
    and the client finds out when the penalty arrives.
    """
    verdict = evaluate(
        GSTR1_RULE,
        {"registrations": ["GST"], "gst_scheme": "REGULAR"},  # turnover and QRMP unknown
    )
    assert verdict.result is V.UNKNOWN
    assert not verdict.confirmed
    assert "aggregate_turnover" in verdict.missing_facts


def test_missing_facts_are_only_reported_when_they_mattered() -> None:
    """The onboarding question queue must not ask about irrelevancies."""
    verdict = evaluate(
        {
            "all": [
                {"fact": "entity_type", "op": "eq", "value": "LLP"},
                {"fact": "has_boiler", "op": "eq", "value": True},
            ]
        },
        {"entity_type": "PVT_LTD"},
    )
    assert verdict.result is V.FALSE
    # Decided by entity_type alone; nobody needs to be asked about the boiler.
    assert verdict.confirmed


def test_an_empty_rule_applies_to_everyone() -> None:
    """How "every company files this" is expressed."""
    assert evaluate({}, {}).result is V.TRUE


# ===========================================================================
# Explanation
# ===========================================================================


def test_explains_why_it_applies_using_the_authors_wording() -> None:
    verdict = evaluate(
        GSTR1_RULE,
        {
            "registrations": ["GST"],
            "gst_scheme": "REGULAR",
            "aggregate_turnover": 805000000,
            "qrmp_opted": True,
        },
    )
    reasons = verdict.reasons()
    assert "you are registered under GST" in reasons
    assert "annual turnover above ₹5 crore" in reasons


def test_a_failure_is_explained_by_the_first_failing_clause_only() -> None:
    """One sentence, not the whole decision tree."""
    verdict = evaluate(GSTR1_RULE, {"registrations": ["PAN"]})
    reasons = verdict.reasons()
    assert len(reasons) == 1
    assert "registered under GST" in reasons[0]


def test_generates_readable_wording_when_the_author_supplied_none() -> None:
    verdict = evaluate({"fact": "employee_count", "op": "gte", "value": 20}, {"employee_count": 50})
    assert verdict.reasons() == ["employee count is at least 20"]


# ===========================================================================
# Operators
# ===========================================================================


@pytest.mark.parametrize(
    ("rule", "facts", "expected"),
    [
        ({"fact": "n", "op": "between", "value": [10, 20]}, {"n": 15}, V.TRUE),
        ({"fact": "n", "op": "between", "value": [10, 20]}, {"n": 25}, V.FALSE),
        ({"fact": "s", "op": "in", "value": ["A", "B"]}, {"s": "A"}, V.TRUE),
        ({"fact": "s", "op": "not_in", "value": ["A"]}, {"s": "B"}, V.TRUE),
        ({"fact": "xs", "op": "includes", "value": ["A", "B"]}, {"xs": ["A", "B", "C"]}, V.TRUE),
        ({"fact": "xs", "op": "includes", "value": ["A", "Z"]}, {"xs": ["A", "B"]}, V.FALSE),
        ({"fact": "xs", "op": "excludes", "value": "Z"}, {"xs": ["A"]}, V.TRUE),
        ({"fact": "xs", "op": "count_gte", "value": 2}, {"xs": ["A", "B"]}, V.TRUE),
        ({"fact": "s", "op": "matches", "value": "^IN-"}, {"s": "IN-GJ"}, V.TRUE),
        ({"fact": "d", "op": "gte", "value": "2020-01-01"}, {"d": "2021-06-01"}, V.TRUE),
    ],
)
def test_operators(rule: dict, facts: dict, expected: V) -> None:
    assert evaluate(rule, facts).result is expected


def test_exists_is_the_only_operator_that_is_never_unknown() -> None:
    """Absence is the answer, not a gap in the answer."""
    assert evaluate({"fact": "gone", "op": "exists", "value": False}, {}).result is V.TRUE
    assert evaluate({"fact": "gone", "op": "exists", "value": True}, {}).result is V.FALSE


# ===========================================================================
# Authoring-time validation
# ===========================================================================


def test_valid_rule_passes() -> None:
    assert (
        validate_rule(
            GSTR1_RULE,
            known_facts=["registrations", "gst_scheme", "aggregate_turnover", "qrmp_opted"],
        )
        == []
    )


def test_unknown_fact_is_rejected_at_authoring_time() -> None:
    """An unknown fact is a typo in the rule, not merely an unknown value.

    Caught here, before it silently makes a definition apply to nobody.
    """
    errors = validate_rule({"fact": "trunover", "op": "gt", "value": 1}, known_facts=["turnover"])
    assert len(errors) == 1
    assert "unknown fact" in str(errors[0])


def test_unknown_operator_is_rejected() -> None:
    errors = validate_rule({"fact": "x", "op": "approximately", "value": 1})
    assert any("unknown operator" in str(e) for e in errors)


def test_between_requires_two_ordered_bounds() -> None:
    assert validate_rule({"fact": "x", "op": "between", "value": [5]})
    assert validate_rule({"fact": "x", "op": "between", "value": [20, 10]})


def test_invalid_regex_is_rejected() -> None:
    assert validate_rule({"fact": "x", "op": "matches", "value": "([unclosed"})


def test_every_error_is_reported_not_just_the_first() -> None:
    errors = validate_rule(
        {"all": [{"fact": "a", "op": "nope", "value": 1}, {"fact": "b", "op": "alsonope"}]}
    )
    assert len(errors) >= 2


def test_deeply_nested_rules_are_refused() -> None:
    rule: dict = {"fact": "x", "op": "eq", "value": 1}
    for _ in range(12):
        rule = {"all": [rule]}
    assert validate_rule(rule)


# ===========================================================================
# Fact extraction
# ===========================================================================


def test_facts_used_powers_targeted_recomputation() -> None:
    """Lets a profile edit re-evaluate only what it could have affected."""
    assert facts_used(GSTR1_RULE) == {
        "registrations",
        "gst_scheme",
        "aggregate_turnover",
        "qrmp_opted",
    }
