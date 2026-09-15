"""
``compute_penalties`` — pure, no database, no Django.

The point of the module under test is refusing to guess: a rule capped at an
amount nobody entered must come back with ``computed_minor=None``, not a
number that looks plausible and is wrong.
"""

from __future__ import annotations

import pytest

from stacos.engine.penalty import compute_penalties


def test_per_day_capped_accrues_and_stops_at_the_cap() -> None:
    rules = [
        {
            "kind": "PER_DAY_CAPPED",
            "rate_minor": 5000,
            "cap_minor": 500000,
            "statutory_reference": "s47",
        }
    ]

    below_cap = compute_penalties(rules, days_late=5)[0]
    assert below_cap.computed_minor == 25000
    assert below_cap.capped is False

    at_cap = compute_penalties(rules, days_late=100)[0]
    assert at_cap.computed_minor == 500000
    assert at_cap.capped is True


def test_zero_or_negative_days_late_is_zero_exposure() -> None:
    rules = [
        {
            "kind": "PER_DAY_CAPPED",
            "rate_minor": 5000,
            "cap_minor": 500000,
            "statutory_reference": "s47",
        }
    ]
    for days in (0, -3):
        result = compute_penalties(rules, days_late=days)[0]
        assert result.computed_minor == 0
        assert result.capped is False


def test_an_uncapped_rate_reports_no_total() -> None:
    """A cap that depends on an amount STACOS doesn't track (e.g. "the TDS
    amount") must never produce an invented total."""
    rules = [{"kind": "PER_DAY_CAPPED", "rate_minor": 20000, "statutory_reference": "234E"}]
    result = compute_penalties(rules, days_late=40)[0]
    assert result.computed_minor is None
    assert result.capped is False
    assert result.rate_minor == 20000


def test_fixed_range_never_computes_a_total() -> None:
    rules = [
        {
            "kind": "FIXED_RANGE",
            "min_minor": 1000000,
            "max_minor": 10000000,
            "statutory_reference": "271H",
        }
    ]
    result = compute_penalties(rules, days_late=999)[0]
    assert result.computed_minor is None
    assert result.min_minor == 1000000
    assert result.max_minor == 10000000


def test_rules_stack_independently() -> None:
    """234E and 271H apply to the same filing at once — two results, not one
    combined figure."""
    rules = [
        {"kind": "PER_DAY_CAPPED", "rate_minor": 20000, "statutory_reference": "234E"},
        {
            "kind": "FIXED_RANGE",
            "min_minor": 1000000,
            "max_minor": 10000000,
            "statutory_reference": "271H",
        },
    ]
    results = compute_penalties(rules, days_late=10)
    assert len(results) == 2
    assert results[0].kind == "PER_DAY_CAPPED"
    assert results[1].kind == "FIXED_RANGE"


def test_no_rules_is_an_empty_list() -> None:
    assert compute_penalties([], days_late=40) == []


def test_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="unknown penalty rule kind"):
        compute_penalties([{"kind": "SOMETHING_ELSE"}], days_late=1)
