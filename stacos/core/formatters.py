"""
Locale-aware number and date formatting.

Indian digit grouping is not "thousands separators with different spacing" — it
groups the last three digits, then in pairs: ``12345678`` is ``1,23,45,678``, not
``12,345,678``. Getting this wrong is immediately visible to every Indian user
and reads as a foreign product.

Grouping is chosen per jurisdiction, never hardcoded, so a UAE or UK tenant sets
Western grouping through its :class:`JurisdictionPack` rather than through a
branch in application code.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum

__all__ = [
    "DigitGrouping",
    "format_compact",
    "format_currency",
    "format_number",
    "group_digits",
]


class DigitGrouping(StrEnum):
    #: 1,23,45,678 — lakh/crore. India, and broadly South Asia.
    INDIAN = "INDIAN"
    #: 12,345,678 — thousands. Everywhere else STACOS ships.
    WESTERN = "WESTERN"


def group_digits(digits: str, grouping: DigitGrouping = DigitGrouping.INDIAN) -> str:
    """Insert separators into a string of digits.

    >>> group_digits("12345678")
    '1,23,45,678'
    >>> group_digits("12345678", DigitGrouping.WESTERN)
    '12,345,678'
    """
    if grouping is DigitGrouping.WESTERN:
        return f"{int(digits):,}" if digits else ""

    if len(digits) <= 3:
        return digits

    head, tail = digits[:-3], digits[-3:]
    pairs: list[str] = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join([*pairs, tail])


def format_number(
    value: Decimal | float | int | str | None,
    *,
    grouping: DigitGrouping = DigitGrouping.INDIAN,
    decimals: int = 0,
) -> str:
    """Format a number with locale-appropriate grouping.

    >>> format_number(12345678)
    '1,23,45,678'
    >>> format_number(Decimal("1234.5"), decimals=2)
    '1,234.50'
    """
    amount = _to_decimal(value)
    if amount is None:
        return ""

    quantised = amount.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    sign = "-" if quantised < 0 else ""
    text = format(abs(quantised), "f")
    integer_part, _, fraction_part = text.partition(".")

    grouped = group_digits(integer_part, grouping)
    if decimals:
        fraction_part = (fraction_part or "").ljust(decimals, "0")[:decimals]
        return f"{sign}{grouped}.{fraction_part}"
    return f"{sign}{grouped}"


def format_currency(
    value: Decimal | float | int | str | None,
    *,
    symbol: str = "₹",
    grouping: DigitGrouping = DigitGrouping.INDIAN,
    decimals: int = 2,
) -> str:
    """Format money.

    >>> format_currency(12345678)
    '₹1,23,45,678.00'
    """
    formatted = format_number(value, grouping=grouping, decimals=decimals)
    if not formatted:
        return ""
    if formatted.startswith("-"):
        return f"-{symbol}{formatted[1:]}"
    return f"{symbol}{formatted}"


#: (threshold, divisor, suffix) — ordered largest first.
_INDIAN_SCALE = (
    (Decimal("10000000"), Decimal("10000000"), "Cr"),
    (Decimal("100000"), Decimal("100000"), "L"),
    (Decimal("1000"), Decimal("1000"), "K"),
)
_WESTERN_SCALE = (
    (Decimal("1000000000"), Decimal("1000000000"), "B"),
    (Decimal("1000000"), Decimal("1000000"), "M"),
    (Decimal("1000"), Decimal("1000"), "K"),
)


def format_compact(
    value: Decimal | float | int | str | None,
    *,
    symbol: str = "₹",
    grouping: DigitGrouping = DigitGrouping.INDIAN,
    decimals: int = 2,
) -> str:
    """Abbreviate a large figure the way the local market reads it.

    Turnover bands and dashboard tiles need "₹5.00 Cr", not "₹5,00,00,000".

    >>> format_compact(80000000)
    '₹8.00 Cr'
    >>> format_compact(1500000000, grouping=DigitGrouping.WESTERN, symbol="$")
    '$1.50 B'
    """
    amount = _to_decimal(value)
    if amount is None:
        return ""

    scale = _INDIAN_SCALE if grouping is DigitGrouping.INDIAN else _WESTERN_SCALE
    magnitude = abs(amount)
    sign = "-" if amount < 0 else ""

    for threshold, divisor, suffix in scale:
        if magnitude >= threshold:
            scaled = (magnitude / divisor).quantize(
                Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP
            )
            return f"{sign}{symbol}{scaled} {suffix}"

    return format_currency(amount, symbol=symbol, grouping=grouping, decimals=decimals)


def _to_decimal(value: Decimal | float | int | str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
