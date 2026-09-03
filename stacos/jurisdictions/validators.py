"""
Tax and registration identifier validators.

One validator per identifier type, registered by key, so ``EntityRegistration``
validates whatever it is given without knowing anything about India. Adding the
UAE means adding a validator here and a jurisdiction pack — never a branch in
application code.

Real check digits are implemented where the format defines one. Regex-only
validation would accept a transposed GSTIN, and a transposed GSTIN means filing
against the wrong taxpayer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from django.core.exceptions import ValidationError

from stacos.jurisdictions.decode import PAN_HOLDER_TYPES
from stacos.jurisdictions.decode import gstin_check_digit as _decode_check_digit
from stacos.jurisdictions.subdivisions import GST_STATE_CODES

__all__ = [
    "RegistrationValidator",
    "get_validator",
    "validate_gstin",
    "validate_pan",
    "validate_registration_value",
]


@dataclass(frozen=True, slots=True)
class RegistrationValidator:
    key: str
    label: str
    country: str
    validate: Callable[[str], None]
    example: str = ""
    help_text: str = ""


# ---------------------------------------------------------------------------
# India
# ---------------------------------------------------------------------------

#: The character set GSTIN check digits are computed over.
_GST_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
TAN_RE = re.compile(r"^[A-Z]{4}[0-9]{5}[A-Z]$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z][Z][0-9A-Z]$")
CIN_RE = re.compile(r"^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$")
LLPIN_RE = re.compile(r"^[A-Z]{3}-[0-9]{4}$")
IEC_RE = re.compile(r"^[0-9A-Z]{10}$")
UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$")
FSSAI_RE = re.compile(r"^[0-9]{14}$")
EPF_RE = re.compile(r"^[A-Z]{2}/[A-Z]{3}/[0-9]{7}/[0-9]{3}$|^[A-Z]{5}[0-9]{7}[0-9]{3}$")
ESIC_RE = re.compile(r"^[0-9]{17}$")

#: State codes that may appear in the first two digits of a GSTIN.
#:
#: Derived from the subdivision table rather than the numeric range 01-38,
#: which accepted codes that have never been issued. Historic codes stay valid
#: because historic GSTINs are on real client records; `decode.decode_gstin`
#: reads them and says which current jurisdiction they map to.
_GST_STATE_CODES = GST_STATE_CODES


def validate_pan(value: str) -> None:
    """Permanent Account Number — ``ABCDE1234F``.

    The fourth character encodes the holder type (``P`` individual, ``C``
    company, ``F`` firm, ``T`` trust, ...). The permitted set is derived from
    ``decode.PAN_HOLDER_TYPES`` so that the character this accepts and the
    character the decoder can read are the same set by construction — the bare
    string literal that used to live here had drifted, and was missing ``B``.

    The cross-check its old docstring promised — that the holder type agrees with
    the declared entity type — lives in ``decode.read`` and is reported as a
    *conflict*, not raised here. A Section 8 company legitimately holds a ``C``
    PAN and an LLP and a partnership legitimately share ``F``; refusing the
    user's own correct answer because a heuristic disagrees would be worse than
    saying nothing.
    """
    value = value.upper().strip()
    if not PAN_RE.match(value):
        raise ValidationError(
            "A PAN is ten characters: five letters, four digits, then a letter (e.g. ABCDE1234F).",
            code="invalid_pan",
        )
    if value[3] not in PAN_HOLDER_TYPES:
        raise ValidationError(
            f"'{value[3]}' is not a recognised PAN holder-type character (position 4).",
            code="invalid_pan_type",
        )


def validate_tan(value: str) -> None:
    """Tax Deduction Account Number — ``ABCD12345E``."""
    if not TAN_RE.match(value.upper().strip()):
        raise ValidationError(
            "A TAN is ten characters: four letters, five digits, then a letter (e.g. ABCD12345E).",
            code="invalid_tan",
        )


def gstin_check_digit(first_fourteen: str) -> str:
    """The fifteenth character of a GSTIN. See :mod:`stacos.jurisdictions.decode`.

    Re-exported here because this is where callers have always looked for it.
    The implementation moved so that ``decode`` can verify a GSTIN it is asked
    to read without importing this module, which imports it.
    """
    try:
        return _decode_check_digit(first_fourteen)
    except ValueError as exc:
        raise ValidationError(str(exc), code="invalid_gstin_char") from None


def validate_gstin(value: str) -> None:
    """Goods and Services Tax Identification Number — ``27AAPFU0939F1ZV``.

    Structure: two-digit state code, the holder's PAN, an entity number, a fixed
    ``Z``, and a check digit. All three of the state code, the embedded PAN and
    the check digit are verified — a GSTIN that merely matches the pattern is not
    good enough when it determines who a return is filed for.
    """
    value = value.upper().strip()

    if len(value) != 15:
        raise ValidationError(
            f"A GSTIN is 15 characters; this one is {len(value)}.", code="invalid_gstin_length"
        )
    if not GSTIN_RE.match(value):
        raise ValidationError(
            "This does not look like a GSTIN. The expected shape is "
            "27AAPFU0939F1ZV — state code, PAN, entity number, 'Z', check digit.",
            code="invalid_gstin_format",
        )
    if value[:2] not in _GST_STATE_CODES:
        raise ValidationError(
            f"'{value[:2]}' is not a valid GST state code.", code="invalid_gstin_state"
        )

    validate_pan(value[2:12])

    expected = gstin_check_digit(value[:14])
    if value[14] != expected:
        raise ValidationError(
            "The GSTIN check digit does not match — this is usually a typo or two "
            "transposed characters.",
            code="invalid_gstin_checksum",
        )


def validate_cin(value: str) -> None:
    """Corporate Identity Number — 21 characters, MCA-issued."""
    if not CIN_RE.match(value.upper().strip()):
        raise ValidationError(
            "A CIN is 21 characters, e.g. U72200KA2015PTC012345.", code="invalid_cin"
        )


def validate_llpin(value: str) -> None:
    if not LLPIN_RE.match(value.upper().strip()):
        raise ValidationError("An LLPIN looks like AAB-1234.", code="invalid_llpin")


def _regex_validator(pattern: re.Pattern[str], message: str, code: str) -> Callable[[str], None]:
    def _validate(value: str) -> None:
        if not pattern.match(value.upper().strip()):
            raise ValidationError(message, code=code)

    return _validate


# ---------------------------------------------------------------------------
# Other jurisdictions — thin, to prove the abstraction holds
# ---------------------------------------------------------------------------

AE_TRN_RE = re.compile(r"^[0-9]{15}$")
GB_UTR_RE = re.compile(r"^[0-9]{10}$")
GB_VAT_RE = re.compile(r"^(GB)?[0-9]{9}([0-9]{3})?$")
SG_UEN_RE = re.compile(
    r"^[0-9]{8,9}[A-Z]$|^[0-9]{4}[0-9]{5}[A-Z]$|^[TSR][0-9]{2}[A-Z]{2}[0-9]{4}[A-Z]$"
)
US_EIN_RE = re.compile(r"^[0-9]{2}-?[0-9]{7}$")


VALIDATORS: dict[str, RegistrationValidator] = {
    v.key: v
    for v in [
        RegistrationValidator("PAN", "PAN", "IN", validate_pan, "ABCDE1234F"),
        RegistrationValidator("TAN", "TAN", "IN", validate_tan, "ABCD12345E"),
        RegistrationValidator("GST", "GSTIN", "IN", validate_gstin, "27AAPFU0939F1ZV"),
        RegistrationValidator("CIN", "CIN", "IN", validate_cin, "U72200KA2015PTC012345"),
        RegistrationValidator("LLPIN", "LLPIN", "IN", validate_llpin, "AAB-1234"),
        RegistrationValidator(
            "IEC",
            "Importer-Exporter Code",
            "IN",
            _regex_validator(IEC_RE, "An IEC is ten alphanumeric characters.", "invalid_iec"),
            "AAACG1234D",
        ),
        RegistrationValidator(
            "UDYAM",
            "Udyam registration",
            "IN",
            _regex_validator(
                UDYAM_RE, "A Udyam number looks like UDYAM-KA-03-1234567.", "invalid_udyam"
            ),
            "UDYAM-KA-03-1234567",
        ),
        RegistrationValidator(
            "FSSAI",
            "FSSAI licence",
            "IN",
            _regex_validator(FSSAI_RE, "An FSSAI licence number is 14 digits.", "invalid_fssai"),
            "12345678901234",
        ),
        RegistrationValidator(
            "ESIC",
            "ESIC code",
            "IN",
            _regex_validator(ESIC_RE, "An ESIC code is 17 digits.", "invalid_esic"),
        ),
        RegistrationValidator(
            "PF",
            "EPF establishment code",
            "IN",
            _regex_validator(EPF_RE, "An EPF code looks like KN/BNG/0012345/000.", "invalid_pf"),
            "KN/BNG/0012345/000",
        ),
        RegistrationValidator(
            "AE_TRN",
            "UAE Tax Registration Number",
            "AE",
            _regex_validator(AE_TRN_RE, "A UAE TRN is 15 digits.", "invalid_trn"),
            "100123456700003",
        ),
        RegistrationValidator(
            "GB_UTR",
            "UK Unique Taxpayer Reference",
            "GB",
            _regex_validator(GB_UTR_RE, "A UK UTR is 10 digits.", "invalid_utr"),
        ),
        RegistrationValidator(
            "GB_VAT",
            "UK VAT number",
            "GB",
            _regex_validator(GB_VAT_RE, "A UK VAT number is 9 or 12 digits.", "invalid_vat"),
        ),
        RegistrationValidator(
            "SG_UEN",
            "Singapore UEN",
            "SG",
            _regex_validator(SG_UEN_RE, "That is not a recognised Singapore UEN.", "invalid_uen"),
        ),
        RegistrationValidator(
            "US_EIN",
            "US Employer Identification Number",
            "US",
            _regex_validator(US_EIN_RE, "An EIN is nine digits, e.g. 12-3456789.", "invalid_ein"),
        ),
    ]
}


def get_validator(registration_type: str) -> RegistrationValidator | None:
    return VALIDATORS.get(registration_type.upper())


def validate_registration_value(registration_type: str, value: str) -> None:
    """Validate a value for its type.

    Unknown types pass. A tenant in a jurisdiction STACOS has not modelled yet
    must still be able to record their registration numbers — refusing them would
    make the product unusable outside the jurisdictions we happen to have
    finished.
    """
    validator = get_validator(registration_type)
    if validator is not None:
        validator.validate(value)
