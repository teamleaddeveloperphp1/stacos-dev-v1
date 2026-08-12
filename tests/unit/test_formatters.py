"""Indian digit grouping and currency formatting."""

from __future__ import annotations

from decimal import Decimal

import pytest

from stacos.core.formatters import (
    DigitGrouping,
    format_compact,
    format_currency,
    format_number,
    group_digits,
)


@pytest.mark.parametrize(
    ("digits", "expected"),
    [
        ("1", "1"),
        ("12", "12"),
        ("123", "123"),
        ("1234", "1,234"),
        ("12345", "12,345"),
        ("123456", "1,23,456"),
        ("1234567", "12,34,567"),
        ("12345678", "1,23,45,678"),
        ("123456789", "12,34,56,789"),
        ("1234567890", "1,23,45,67,890"),
    ],
)
def test_indian_grouping(digits: str, expected: str) -> None:
    """Last three digits, then pairs. Not thousands with different spacing."""
    assert group_digits(digits, DigitGrouping.INDIAN) == expected


@pytest.mark.parametrize(
    ("digits", "expected"),
    [("1234", "1,234"), ("12345678", "12,345,678"), ("123456789", "123,456,789")],
)
def test_western_grouping(digits: str, expected: str) -> None:
    assert group_digits(digits, DigitGrouping.WESTERN) == expected


def test_currency_formatting() -> None:
    assert format_currency(12345678) == "₹1,23,45,678.00"
    assert format_currency(Decimal("1234.5")) == "₹1,234.50"
    assert format_currency(0) == "₹0.00"


def test_negative_amounts_put_the_sign_before_the_symbol() -> None:
    assert format_currency(-1500) == "-₹1,500.00"


def test_none_and_blank_render_as_empty() -> None:
    assert format_currency(None) == ""
    assert format_number("") == ""


def test_rounding_is_half_up() -> None:
    """Banker's rounding surprises accountants; half-up is what a tax computation
    is expected to do."""
    assert format_number(Decimal("2.5"), decimals=0) == "3"
    assert format_number(Decimal("3.5"), decimals=0) == "4"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (80_000_000, "₹8.00 Cr"),
        (805_000_000, "₹80.50 Cr"),
        (4_700_000, "₹47.00 L"),
        (95_000, "₹95.00 K"),
        (500, "₹500.00"),
    ],
)
def test_compact_uses_lakh_and_crore(value: int, expected: str) -> None:
    assert format_compact(value) == expected


def test_compact_uses_million_and_billion_for_western_grouping() -> None:
    assert format_compact(1_500_000_000, grouping=DigitGrouping.WESTERN, symbol="$") == "$1.50 B"
