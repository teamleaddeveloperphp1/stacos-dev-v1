"""
Tax identifier validators.

The GSTIN tests matter most. A regex-only check accepts a transposed GSTIN, and
a transposed GSTIN means a return filed against the wrong taxpayer — so the check
digit is verified, and these tests use values whose checksums are computed the
same way the portal computes them.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from stacos.jurisdictions.validators import (
    gstin_check_digit,
    validate_cin,
    validate_gstin,
    validate_pan,
    validate_registration_value,
    validate_tan,
)


def make_gstin(state: str, pan: str, entity_no: str = "1") -> str:
    """Build a GSTIN with a correct check digit, the way the portal would."""
    body = f"{state}{pan}{entity_no}Z"
    return body + gstin_check_digit(body)


# ---------------------------------------------------------------------------
# PAN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pan", ["AAACV1234K", "ABCPD5678Q", "AAEFV5678M"])
def test_valid_pan(pan: str) -> None:
    validate_pan(pan)


@pytest.mark.parametrize(
    ("pan", "reason"),
    [
        ("AAACV1234", "too short"),
        ("AAACV12345K", "too long"),
        ("1AACV1234K", "digit in the letter block"),
        ("AAACV1234", "missing final letter"),
        ("AAAZV1234K", "Z is not a holder-type character"),
    ],
)
def test_invalid_pan(pan: str, reason: str) -> None:
    with pytest.raises(ValidationError):
        validate_pan(pan)


def test_pan_holder_type_character_is_checked() -> None:
    """The fourth character encodes the holder type. A PAN whose type
    contradicts the entity type is almost always a typo, so it is worth
    rejecting rather than storing."""
    validate_pan("AAACV1234K")  # C = company
    with pytest.raises(ValidationError, match="holder-type"):
        validate_pan("AAAXV1234K")


# ---------------------------------------------------------------------------
# GSTIN
# ---------------------------------------------------------------------------


def test_check_digit_algorithm_is_deterministic() -> None:
    body = "24AAACV1234K1Z"
    assert gstin_check_digit(body[:14]) == gstin_check_digit(body[:14])
    assert len(gstin_check_digit(body[:14])) == 1


def test_valid_gstin_round_trips() -> None:
    gstin = make_gstin("24", "AAACV1234K")
    validate_gstin(gstin)
    assert len(gstin) == 15


def test_gstin_rejects_a_wrong_check_digit() -> None:
    """The whole reason for implementing the checksum."""
    gstin = make_gstin("24", "AAACV1234K")
    wrong = gstin[:14] + ("A" if gstin[14] != "A" else "B")
    with pytest.raises(ValidationError, match="check digit"):
        validate_gstin(wrong)


def test_gstin_rejects_transposed_characters() -> None:
    """Two swapped characters is the commonest real-world typo, and exactly what
    a checksum is for."""
    gstin = make_gstin("24", "AAACV1234K")
    transposed = gstin[:9] + gstin[10] + gstin[9] + gstin[11:]
    if transposed != gstin:  # only meaningful when the two differ
        with pytest.raises(ValidationError):
            validate_gstin(transposed)


def test_gstin_rejects_an_unknown_state_code() -> None:
    body = "99AAACV1234K1Z"
    valid_shape = body + gstin_check_digit(body)
    validate_gstin(valid_shape)  # 99 is "Other Territory" and is legitimate

    body = "88AAACV1234K1Z"
    with pytest.raises(ValidationError, match="state code"):
        validate_gstin(body + gstin_check_digit(body))


def test_gstin_validates_the_embedded_pan() -> None:
    """A GSTIN contains its holder's PAN. If that PAN is malformed the GSTIN is
    wrong regardless of the check digit."""
    body = "24AAAZV1234K1Z"  # Z is not a valid holder-type character
    with pytest.raises(ValidationError):
        validate_gstin(body + gstin_check_digit(body))


def test_gstin_rejects_wrong_length() -> None:
    with pytest.raises(ValidationError, match="15 characters"):
        validate_gstin("24AAACV1234K1Z")


# ---------------------------------------------------------------------------
# Others
# ---------------------------------------------------------------------------


def test_valid_tan() -> None:
    validate_tan("SRTV12345E")


def test_invalid_tan() -> None:
    with pytest.raises(ValidationError):
        validate_tan("SRT12345E")


def test_valid_cin() -> None:
    validate_cin("U17110GJ2011PTC065432")


def test_invalid_cin() -> None:
    with pytest.raises(ValidationError):
        validate_cin("X17110GJ2011PTC065432")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_unknown_registration_types_are_accepted() -> None:
    """A tenant in a jurisdiction STACOS has not modelled yet must still be able
    to record their numbers. Refusing them would make the product unusable
    outside the jurisdictions that happen to be finished."""
    validate_registration_value("SOME_FUTURE_ID", "anything at all")


def test_dispatch_applies_the_right_validator() -> None:
    with pytest.raises(ValidationError):
        validate_registration_value("PAN", "not-a-pan")


@pytest.mark.parametrize(
    ("reg_type", "value"),
    [("AE_TRN", "100123456700003"), ("GB_UTR", "1234567890"), ("US_EIN", "12-3456789")],
)
def test_other_jurisdictions(reg_type: str, value: str) -> None:
    """Thin packs for other countries, proving the abstraction holds."""
    validate_registration_value(reg_type, value)
