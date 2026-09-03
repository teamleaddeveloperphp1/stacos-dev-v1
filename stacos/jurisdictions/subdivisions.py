"""
Indian states and union territories, in the four spellings the product needs.

Before this module the same 36 places were written out in five places: the fact
registry's ``IN_STATE_CODES``, a hand-maintained label dict in the tenancy forms,
``sub_jurisdictions`` in the jurisdiction pack, the numeric codes inside a GSTIN,
and the two-letter code inside a CIN. Nothing connected them, so a GSTIN could be
validated without anyone being able to say which state it belonged to.

The pack YAML remains the *authored* source; this is a pure in-process mirror,
and ``tests/unit/test_subdivision_parity.py`` fails the build if they disagree.
It has to be a pure module rather than a database read, because
``manage.py validatecatalog`` is a merge gate that runs without Postgres and the
fact registry is imported at module scope by the catalog loader.

Not a ``TextChoices`` enum, for the reason rule 4 gives: thirty-six hardcoded
members *is* jurisdiction logic in code. This is a table of data that happens to
live in a ``.py`` file so it can be imported without a database.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "IN_SUBDIVISIONS",
    "Subdivision",
    "by_cin_code",
    "by_code",
    "by_gst_code",
    "choices",
]


@dataclass(frozen=True, slots=True)
class Subdivision:
    #: ISO 3166-2, the canonical form used everywhere in the product.
    code: str
    name: str
    #: First two digits of a GSTIN. Empty where the place has never had one.
    gst_code: str = ""
    #: Characters 7-8 of a CIN. Close to the ISO suffix but not always equal —
    #: Odisha is OR in a CIN and IN-OR in ISO, Uttarakhand is UR and IN-UT.
    cin_code: str = ""
    #: Set for a code that still appears in historic identifiers but no longer
    #: names a live jurisdiction. Decoding one is correct; offering it in a
    #: dropdown is not.
    superseded_by: str = ""

    @property
    def is_current(self) -> bool:
        return not self.superseded_by


IN_SUBDIVISIONS: tuple[Subdivision, ...] = (
    Subdivision("IN-AN", "Andaman and Nicobar Islands", "35", "AN"),
    Subdivision("IN-AP", "Andhra Pradesh", "37", "AP"),
    Subdivision("IN-AR", "Arunachal Pradesh", "12", "AR"),
    Subdivision("IN-AS", "Assam", "18", "AS"),
    Subdivision("IN-BR", "Bihar", "10", "BR"),
    Subdivision("IN-CH", "Chandigarh", "04", "CH"),
    Subdivision("IN-CT", "Chhattisgarh", "22", "CT"),
    Subdivision("IN-DH", "Dadra and Nagar Haveli and Daman and Diu", "26", "DN"),
    Subdivision("IN-DL", "Delhi", "07", "DL"),
    Subdivision("IN-GA", "Goa", "30", "GA"),
    Subdivision("IN-GJ", "Gujarat", "24", "GJ"),
    Subdivision("IN-HP", "Himachal Pradesh", "02", "HP"),
    Subdivision("IN-HR", "Haryana", "06", "HR"),
    Subdivision("IN-JH", "Jharkhand", "20", "JH"),
    Subdivision("IN-JK", "Jammu and Kashmir", "01", "JK"),
    Subdivision("IN-KA", "Karnataka", "29", "KA"),
    Subdivision("IN-KL", "Kerala", "32", "KL"),
    Subdivision("IN-LA", "Ladakh", "38", "LA"),
    Subdivision("IN-LD", "Lakshadweep", "31", "LD"),
    Subdivision("IN-MH", "Maharashtra", "27", "MH"),
    Subdivision("IN-ML", "Meghalaya", "17", "ML"),
    Subdivision("IN-MN", "Manipur", "14", "MN"),
    Subdivision("IN-MP", "Madhya Pradesh", "23", "MP"),
    Subdivision("IN-MZ", "Mizoram", "15", "MI"),
    Subdivision("IN-NL", "Nagaland", "13", "NL"),
    Subdivision("IN-OR", "Odisha", "21", "OR"),
    Subdivision("IN-PB", "Punjab", "03", "PB"),
    Subdivision("IN-PY", "Puducherry", "34", "PY"),
    Subdivision("IN-RJ", "Rajasthan", "08", "RJ"),
    Subdivision("IN-SK", "Sikkim", "11", "SK"),
    Subdivision("IN-TG", "Telangana", "36", "TG"),
    Subdivision("IN-TN", "Tamil Nadu", "33", "TN"),
    Subdivision("IN-TR", "Tripura", "16", "TR"),
    Subdivision("IN-UP", "Uttar Pradesh", "09", "UP"),
    Subdivision("IN-UT", "Uttarakhand", "05", "UR"),
    Subdivision("IN-WB", "West Bengal", "19", "WB"),
    # Retired, and still decodable. A GSTIN issued in Daman and Diu before the
    # 2020 merger, or in undivided Andhra Pradesh before 2014, is on real client
    # records. Refusing to read one would be worse than reading it and saying so.
    Subdivision(
        "IN-DD", "Daman and Diu (merged into Dadra and Nagar Haveli in 2020)", "25", "DD", "IN-DH"
    ),
    Subdivision("IN-AP-OLD", "Andhra Pradesh (before the 2014 reorganisation)", "28", "", "IN-AP"),
)

#: GSTINs issued outside the state series: 97 is "other territory" (offshore),
#: 99 was used for centralised UIN allotment.
_SPECIAL_GST_CODES: frozenset[str] = frozenset({"97", "99"})

_BY_CODE = {s.code: s for s in IN_SUBDIVISIONS}
_BY_GST = {s.gst_code: s for s in IN_SUBDIVISIONS if s.gst_code}
_BY_CIN = {s.cin_code: s for s in IN_SUBDIVISIONS if s.cin_code}

#: Every numeric prefix a GSTIN may legitimately carry.
GST_STATE_CODES: frozenset[str] = frozenset(_BY_GST) | _SPECIAL_GST_CODES


def by_code(code: str) -> Subdivision | None:
    return _BY_CODE.get(code)


def by_gst_code(numeric: str) -> Subdivision | None:
    """The state a GSTIN's first two digits name, or ``None``."""
    return _BY_GST.get(numeric)


def by_cin_code(alpha: str) -> Subdivision | None:
    """The state a CIN's seventh and eighth characters name, or ``None``."""
    return _BY_CIN.get(alpha.upper())


def choices(*, include_retired: bool = False) -> list[tuple[str, str]]:
    """``(code, name)`` pairs for a form field, ordered by name.

    Retired subdivisions are excluded by default: they can be decoded from an old
    identifier but must not be offered as somewhere a business operates today.
    """
    rows = [s for s in IN_SUBDIVISIONS if include_retired or s.is_current]
    return sorted(((s.code, s.name) for s in rows), key=lambda pair: pair[1])
