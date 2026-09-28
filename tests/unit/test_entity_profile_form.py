"""Turnover is entered in crores and stored in rupees."""

from __future__ import annotations

from decimal import Decimal

import pytest

from stacos.tenancy.forms import EntityProfileForm, _rupees_to_crore


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("12.5", Decimal("125000000.00")),
        ("0.4", Decimal("4000000.00")),
        ("0.4723512", Decimal("4723512.00")),
        ("0", Decimal("0.00")),
    ],
)
def test_turnover_typed_in_crores_is_stored_in_rupees(typed: str, stored: Decimal) -> None:
    form = EntityProfileForm({"aggregate_turnover": typed, "employee_count": ""})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["aggregate_turnover"] == stored


def test_blank_turnover_stays_unknown() -> None:
    form = EntityProfileForm({"aggregate_turnover": "", "employee_count": ""})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["aggregate_turnover"] is None


def test_negative_turnover_is_rejected() -> None:
    form = EntityProfileForm({"aggregate_turnover": "-1", "employee_count": ""})
    assert not form.is_valid()


@pytest.mark.parametrize(
    ("rupees", "shown"),
    [
        (Decimal("805000000.00"), "80.5"),
        (Decimal("4723512.00"), "0.4723512"),
        (Decimal("100000000.00"), "10"),
        (Decimal("0.00"), "0"),
    ],
)
def test_stored_rupees_display_as_crores_and_round_trip(rupees: Decimal, shown: str) -> None:
    assert _rupees_to_crore(rupees) == shown
    form = EntityProfileForm({"aggregate_turnover": shown, "employee_count": ""})
    assert form.is_valid(), form.errors
    assert form.cleaned_data["aggregate_turnover"] == rupees
