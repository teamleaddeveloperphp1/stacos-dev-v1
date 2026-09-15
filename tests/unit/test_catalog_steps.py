"""
``workflow_steps`` and ``penalty_rules`` parsing — the catalog's opt-in
checklist template and structured penalty data.

Structural validation happens at parse time, the same place ``evidence``
entries are checked (``key`` required) — see ``stacos/catalog/loader.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stacos.catalog.loader import CatalogError, parse_document

SOURCE = Path("test.yaml")


def _raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "code": "IN-TEST-DEFN",
        "country": "IN",
        "category": "TAX_DIRECT",
        "title": "A test definition",
        "periodicity": "QUARTERLY",
        "due": {
            "anchor": "PERIOD_END",
            "offset": {"months": 1, "day_of_month": 31},
            "shift_if_holiday": "NONE",
        },
        "effective_from": "2020-01-01",
    }
    base.update(overrides)
    return base


def test_no_steps_key_parses_to_an_empty_list() -> None:
    document = parse_document(SOURCE, _raw())
    assert document.workflow_steps == []


def test_a_valid_steps_list_parses_in_order() -> None:
    document = parse_document(
        SOURCE,
        _raw(
            steps=[
                {"key": "prepare_it", "label": "Prepare it", "role": "PREPARE"},
                {"key": "sign_it", "label": "Sign it", "role": "SIGN"},
            ]
        ),
    )
    assert document.workflow_steps == [
        {"key": "prepare_it", "label": "Prepare it", "role": "PREPARE"},
        {"key": "sign_it", "label": "Sign it", "role": "SIGN"},
    ]


def test_step_optional_fields_default_when_omitted() -> None:
    document = parse_document(
        SOURCE, _raw(steps=[{"key": "prepare_it", "label": "Prepare it", "role": "PREPARE"}])
    )
    step = document.workflow_steps[0]
    assert "default_owner_role" not in step
    assert "days_before_due" not in step
    assert "requires_evidence" not in step


def test_step_optional_fields_parse_when_present() -> None:
    document = parse_document(
        SOURCE,
        _raw(
            steps=[
                {
                    "key": "prepare_it",
                    "label": "Prepare it",
                    "role": "PREPARE",
                    "default_owner_role": "org-owner",
                    "days_before_due": 10,
                    "requires_evidence": True,
                }
            ]
        ),
    )
    step = document.workflow_steps[0]
    assert step["default_owner_role"] == "org-owner"
    assert step["days_before_due"] == 10
    assert step["requires_evidence"] is True


def test_negative_days_before_due_is_rejected() -> None:
    with pytest.raises(CatalogError, match="days_before_due"):
        parse_document(
            SOURCE,
            _raw(
                steps=[
                    {
                        "key": "prepare_it",
                        "label": "Prepare it",
                        "role": "PREPARE",
                        "days_before_due": -1,
                    }
                ]
            ),
        )


def test_a_step_without_a_key_is_rejected() -> None:
    with pytest.raises(CatalogError, match="key and a label"):
        parse_document(SOURCE, _raw(steps=[{"label": "No key", "role": "PREPARE"}]))


def test_a_step_without_a_label_is_rejected() -> None:
    with pytest.raises(CatalogError, match="key and a label"):
        parse_document(SOURCE, _raw(steps=[{"key": "no_label", "role": "PREPARE"}]))


def test_an_unknown_role_is_rejected() -> None:
    with pytest.raises(CatalogError, match="expected one of"):
        parse_document(
            SOURCE, _raw(steps=[{"key": "step_one", "label": "Step one", "role": "SUBMIT"}])
        )


def test_a_repeated_step_key_is_rejected() -> None:
    with pytest.raises(CatalogError, match="repeated"):
        parse_document(
            SOURCE,
            _raw(
                steps=[
                    {"key": "same", "label": "First", "role": "PREPARE"},
                    {"key": "same", "label": "Second", "role": "REVIEW"},
                ]
            ),
        )


# ===========================================================================
# penalty_rules
# ===========================================================================


def test_no_penalty_rules_key_parses_to_an_empty_list() -> None:
    document = parse_document(SOURCE, _raw())
    assert document.penalty_rules == []


def test_a_per_day_capped_rule_parses_with_and_without_a_cap() -> None:
    document = parse_document(
        SOURCE,
        _raw(
            penalty_rules=[
                {
                    "kind": "PER_DAY_CAPPED",
                    "rate_minor": 5000,
                    "cap_minor": 500000,
                    "statutory_reference": "s47",
                },
                {"kind": "PER_DAY_CAPPED", "rate_minor": 20000, "statutory_reference": "234E"},
            ]
        ),
    )
    assert document.penalty_rules[0]["cap_minor"] == 500000
    assert "cap_minor" not in document.penalty_rules[1]


def test_a_fixed_range_rule_parses() -> None:
    document = parse_document(
        SOURCE,
        _raw(
            penalty_rules=[
                {
                    "kind": "FIXED_RANGE",
                    "min_minor": 1000000,
                    "max_minor": 10000000,
                    "statutory_reference": "271H",
                }
            ]
        ),
    )
    assert document.penalty_rules == [
        {
            "kind": "FIXED_RANGE",
            "min_minor": 1000000,
            "max_minor": 10000000,
            "statutory_reference": "271H",
        }
    ]


def test_an_unknown_penalty_kind_is_rejected() -> None:
    with pytest.raises(CatalogError, match="expected one of"):
        parse_document(
            SOURCE, _raw(penalty_rules=[{"kind": "PERCENTAGE", "statutory_reference": "x"}])
        )


def test_a_penalty_rule_without_a_statutory_reference_is_rejected() -> None:
    with pytest.raises(CatalogError, match="statutory_reference"):
        parse_document(SOURCE, _raw(penalty_rules=[{"kind": "PER_DAY_CAPPED", "rate_minor": 5000}]))


def test_per_day_capped_needs_a_positive_rate() -> None:
    with pytest.raises(CatalogError, match="rate_minor"):
        parse_document(
            SOURCE,
            _raw(
                penalty_rules=[
                    {"kind": "PER_DAY_CAPPED", "rate_minor": 0, "statutory_reference": "s47"}
                ]
            ),
        )


def test_fixed_range_rejects_max_below_min() -> None:
    with pytest.raises(CatalogError, match="min_minor"):
        parse_document(
            SOURCE,
            _raw(
                penalty_rules=[
                    {
                        "kind": "FIXED_RANGE",
                        "min_minor": 500,
                        "max_minor": 100,
                        "statutory_reference": "271H",
                    }
                ]
            ),
        )
