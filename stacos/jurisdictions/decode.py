"""
Reading what an Indian tax identifier already tells you.

A PAN, a CIN, a GSTIN and an LLPIN are not opaque strings. Between them they
encode the legal form, the state, the year of incorporation, whether the company
is listed and its industry code — all of which onboarding otherwise asks a user
to type in, and half of which they get wrong.

``validators.py`` has always *checked* these characters. It has never read them:
``validate_pan`` confirms the fourth character is one of eleven letters and its
own docstring promises a cross-check against the declared entity type that was
never written. This module is that reading.

Three rules govern everything here, and they are the difference between help and
harm:

**Ambiguity is reported, never resolved.** PAN character ``C`` means "a company"
and cannot distinguish a private limited from a public limited from a Section 8.
A CIN can. So a PAN yields four candidates marked ``POSSIBLE`` and the UI leaves
the field unset with those four at the top of the list; a CIN yields one marked
``CERTAIN`` and the UI fills it in and says where it came from.

**A contradiction is surfaced, never silently preferred.** If the PAN says a firm
and the CIN says a private company, somebody has mistyped one of them, and
quietly believing the CIN hides that.

**Nothing here decides anything.** These functions return hints. Every field
stays editable, because a Section 8 company legitimately holds a ``C`` PAN and a
partnership and an LLP legitimately share ``F`` — a heuristic that overrides the
user's own correct answer is worse than no heuristic.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from stacos.jurisdictions.subdivisions import by_cin_code, by_gst_code

__all__ = [
    "CIN_OWNERSHIP",
    "PAN_HOLDER_TYPES",
    "Confidence",
    "Hint",
    "IdentityReport",
    "Reading",
    "decode_cin",
    "decode_gstin",
    "decode_llpin",
    "decode_pan",
    "read",
    "sniff",
]


class Confidence(StrEnum):
    #: The format encodes exactly this. A CIN ending PTC is a private company.
    CERTAIN = "CERTAIN"
    #: One strongly dominant reading. A PAN with T is almost always a trust.
    LIKELY = "LIKELY"
    #: Several readings, none preferable. A PAN with C is *a* company.
    POSSIBLE = "POSSIBLE"


@dataclass(frozen=True, slots=True)
class Hint:
    """One thing an identifier says about the entity."""

    field: str
    #: Candidates, most likely first. More than one *is* the ambiguity — a caller
    #: that takes ``values[0]`` and discards the rest has thrown away the honesty.
    values: tuple[Any, ...]
    confidence: Confidence
    #: Which identifier, and which part of it. Shown to the user verbatim.
    source: str
    explanation: str

    @property
    def is_definite(self) -> bool:
        return self.confidence is Confidence.CERTAIN and len(self.values) == 1

    @property
    def value(self) -> Any:
        return self.values[0] if self.values else None


@dataclass(frozen=True, slots=True)
class Conflict:
    field: str
    explanation: str


@dataclass(frozen=True, slots=True)
class Reading:
    """What one identifier turned out to be."""

    registration_type: str
    value: str
    valid: bool
    error: str = ""
    hints: tuple[Hint, ...] = ()
    #: Things worth saying that are not facts about the entity — "this GST state
    #: code was retired in 2020".
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IdentityReport:
    readings: tuple[Reading, ...] = ()
    #: Merged and de-duplicated: the best-confidence hint per field.
    hints: tuple[Hint, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    notes: tuple[str, ...] = ()

    def hint_for(self, field_name: str) -> Hint | None:
        return next((hint for hint in self.hints if hint.field == field_name), None)

    def prefill(self) -> dict[str, Any]:
        """Fields confident enough to fill in for the user.

        Deliberately only the definite ones. An ambiguous hint belongs in the
        field's *ordering* — candidates promoted to the top of the list, nothing
        selected — because "we guessed, pick one" is honest and "we filled it in
        from a guess" is not.
        """
        return {hint.field: hint.value for hint in self.hints if hint.is_definite}


# ---------------------------------------------------------------------------
# The maps
# ---------------------------------------------------------------------------

#: PAN character 4: the holder type. Exactly the eleven characters
#: ``validators.validate_pan`` already accepts, now with meanings attached.
#: An empty tuple means the character is valid but names no entity type this
#: product models — a local authority does not appear in ``ENTITY_TYPES``.
PAN_HOLDER_TYPES: dict[str, tuple[str, ...]] = {
    "C": ("PVT_LTD", "PUBLIC_LTD", "OPC", "SECTION_8"),
    "F": ("LLP", "PARTNERSHIP"),
    "P": ("PROPRIETORSHIP",),
    "H": ("HUF",),
    "T": ("TRUST",),
    "A": ("SOCIETY", "COOPERATIVE"),
    "B": ("SOCIETY",),
    "K": ("TRUST",),
    "G": (),
    "J": (),
    "L": (),
}

_PAN_HOLDER_LABEL: dict[str, str] = {
    "C": "a company",
    "F": "a firm or LLP",
    "P": "an individual or proprietor",
    "H": "a Hindu undivided family",
    "T": "a trust",
    "A": "an association of persons",
    "B": "a body of individuals",
    "K": "a trust (legacy code)",
    "G": "a government body",
    "J": "an artificial juridical person",
    "L": "a local authority",
}

#: CIN characters 13-15: the ownership class. The only place in any Indian
#: identifier that separates a private company from a public one.
CIN_OWNERSHIP: dict[str, tuple[str, Confidence, str]] = {
    "PTC": ("PVT_LTD", Confidence.CERTAIN, "a private limited company"),
    "PLC": ("PUBLIC_LTD", Confidence.CERTAIN, "a public limited company"),
    "OPC": ("OPC", Confidence.CERTAIN, "a one person company"),
    "NPL": ("SECTION_8", Confidence.CERTAIN, "a not-for-profit (Section 8) company"),
    "FTC": ("BRANCH_OFFICE", Confidence.LIKELY, "a subsidiary of a foreign company"),
    "GOI": ("PUBLIC_LTD", Confidence.LIKELY, "a company owned by the Union government"),
    "SGC": ("PUBLIC_LTD", Confidence.LIKELY, "a company owned by a state government"),
    "ULT": ("PUBLIC_LTD", Confidence.LIKELY, "an unlimited public company"),
    "ULL": ("PVT_LTD", Confidence.LIKELY, "an unlimited private company"),
}

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
CIN_RE = re.compile(r"^([LU])([0-9]{5})([A-Z]{2})([0-9]{4})([A-Z]{3})([0-9]{6})$")
LLPIN_RE = re.compile(r"^[A-Z]{3}-?[0-9]{4}$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")


#: The GSTIN character set, in the order that gives each character its value.
GST_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_check_digit(first_fourteen: str) -> str:
    """Compute the fifteenth character of a GSTIN.

    Each character's value is weighted alternately by 1 and 2; each product is
    folded (``quotient + remainder`` over 36) and summed; the check digit is the
    complement of that sum modulo 36.

    Lives here rather than in ``validators`` because ``validators`` imports this
    module, and because reading a GSTIN without checking whether it is real would
    be reading a number rather than reading an identifier.
    """
    total = 0
    for index, char in enumerate(first_fourteen.upper()):
        value = GST_ALPHABET.find(char)
        if value < 0:
            raise ValueError(f"{char!r} cannot appear in a GSTIN.")
        product = value * (2 if index % 2 else 1)
        total += product // 36 + product % 36
    return GST_ALPHABET[(36 - total % 36) % 36]


def normalise(value: str) -> str:
    return value.strip().upper().replace(" ", "")


# ---------------------------------------------------------------------------
# The decoders
# ---------------------------------------------------------------------------


def decode_pan(value: str) -> Reading:
    """Read a PAN. One ambiguous hint, and a surname initial worth cross-checking."""
    pan = normalise(value)
    if not PAN_RE.match(pan):
        return Reading(
            "PAN",
            pan,
            valid=False,
            error="A PAN is ten characters: five letters, four digits, then a letter.",
        )

    holder = pan[3]
    if holder not in PAN_HOLDER_TYPES:
        return Reading(
            "PAN",
            pan,
            valid=False,
            error=f"'{holder}' is not a recognised PAN holder-type character.",
        )

    candidates = PAN_HOLDER_TYPES[holder]
    label = _PAN_HOLDER_LABEL[holder]
    hints: list[Hint] = []
    notes: list[str] = []

    if candidates:
        hints.append(
            Hint(
                field="entity_type",
                values=candidates,
                # One candidate is as good as it gets from a PAN; several is the
                # ordinary case and the caller must not collapse it.
                confidence=Confidence.LIKELY if len(candidates) == 1 else Confidence.POSSIBLE,
                source="PAN character 4",
                explanation=f"your PAN says the holder is {label}",
            )
        )
    else:
        notes.append(f"Your PAN says the holder is {label}, which this product does not model.")

    # Character 5 is the first letter of the surname, or of the entity name.
    # Not a hint about the entity — a cross-check against the name typed in.
    hints.append(
        Hint(
            field="name_initial",
            values=(pan[4],),
            confidence=Confidence.CERTAIN,
            source="PAN character 5",
            explanation=f"your PAN expects a name beginning with '{pan[4]}'",
        )
    )

    return Reading("PAN", pan, valid=True, hints=tuple(hints), notes=tuple(notes))


def decode_cin(value: str) -> Reading:
    """Read a CIN. The richest of the four — five certain facts at once."""
    cin = normalise(value)
    match = CIN_RE.match(cin)
    if not match:
        return Reading(
            "CIN",
            cin,
            valid=False,
            error=(
                "A CIN is twenty-one characters: L or U, five digits, a two-letter "
                "state, a four-digit year, three letters and six digits."
            ),
        )

    listing, nic, state_code, year, ownership, _ = match.groups()
    hints: list[Hint] = []
    notes: list[str] = []

    hints.append(
        Hint(
            field="is_listed",
            values=(listing == "L",),
            confidence=Confidence.CERTAIN,
            source="CIN character 1",
            explanation=(
                "your CIN begins with L, which means the company is listed"
                if listing == "L"
                else "your CIN begins with U, which means the company is unlisted"
            ),
        )
    )
    hints.append(
        Hint(
            field="nic_code",
            values=(nic,),
            confidence=Confidence.CERTAIN,
            source="CIN characters 2-6",
            explanation=f"your CIN carries industry code {nic}",
        )
    )

    subdivision = by_cin_code(state_code)
    if subdivision is not None:
        hints.append(
            Hint(
                field="registered_office_state",
                values=(subdivision.superseded_by or subdivision.code,),
                confidence=Confidence.CERTAIN,
                source="CIN characters 7-8",
                explanation=f"your CIN says the company is registered in {subdivision.name}",
            )
        )
        if subdivision.superseded_by:
            notes.append(f"{subdivision.name} — mapped to its current jurisdiction.")
    else:
        notes.append(f"'{state_code}' is not a state code this product recognises.")

    hints.append(
        Hint(
            field="incorporation_year",
            values=(int(year),),
            confidence=Confidence.CERTAIN,
            source="CIN characters 9-12",
            explanation=f"your CIN says the company was incorporated in {year}",
        )
    )

    if ownership in CIN_OWNERSHIP:
        entity_type, confidence, description = CIN_OWNERSHIP[ownership]
        hints.append(
            Hint(
                field="entity_type",
                values=(entity_type,),
                confidence=confidence,
                source=f"CIN characters 13-15 ({ownership})",
                explanation=f"your CIN says the company is {description}",
            )
        )
        if listing == "L" and entity_type in {"PVT_LTD", "OPC"}:
            notes.append(
                f"Your CIN says both 'listed' and '{description}', which cannot both "
                f"be true. Check the identifier."
            )
    else:
        # The regex admits any three letters. Inventing a meaning for one we do
        # not know is precisely the confident wrongness this module exists to
        # avoid, so we say nothing about the entity type.
        notes.append(
            f"'{ownership}' is not an ownership class this product recognises, so the "
            f"legal form has not been read from the CIN."
        )

    return Reading("CIN", cin, valid=True, hints=tuple(hints), notes=tuple(notes))


def decode_llpin(value: str) -> Reading:
    """Read an LLPIN. Exactly one fact, and deliberately no more.

    The three-letter prefix is a sequential allotment block, **not** geographic.
    Deriving a state from it would be inventing information, which is the failure
    this whole module exists to prevent — please do not "improve" it.
    """
    llpin = normalise(value)
    if not LLPIN_RE.match(llpin):
        return Reading(
            "LLPIN",
            llpin,
            valid=False,
            error="An LLPIN is three letters and four digits, e.g. AAB-1234.",
        )
    return Reading(
        "LLPIN",
        llpin,
        valid=True,
        hints=(
            Hint(
                field="entity_type",
                values=("LLP",),
                confidence=Confidence.CERTAIN,
                source="LLPIN",
                explanation="an LLPIN is only ever issued to a limited liability partnership",
            ),
        ),
    )


def decode_gstin(value: str) -> Reading:
    """Read a GSTIN: a state, plus the PAN embedded in the middle of it."""
    gstin = normalise(value)
    if not GSTIN_RE.match(gstin):
        return Reading(
            "GST",
            gstin,
            valid=False,
            error=(
                "A GSTIN is fifteen characters: two state digits, a PAN, an entity "
                "digit, the letter Z and a check character."
            ),
        )

    # The check digit, not just the shape. Without this a transposed pair reads
    # cleanly here and is refused by the validator later — which used to mean the
    # user learned about it as a server error on the last screen of the wizard
    # rather than as a message beside the field they typed it into.
    if gstin[14] != gstin_check_digit(gstin[:14]):
        return Reading(
            "GST",
            gstin,
            valid=False,
            error=(
                "The GSTIN check digit does not match — usually a typo or two "
                "transposed characters."
            ),
        )

    hints: list[Hint] = []
    notes: list[str] = []

    subdivision = by_gst_code(gstin[:2])
    if subdivision is not None:
        resolved = subdivision.superseded_by or subdivision.code
        hints.append(
            Hint(
                field="jurisdiction",
                values=(resolved,),
                confidence=Confidence.CERTAIN,
                source="GSTIN characters 1-2",
                explanation=f"this registration is in {subdivision.name}",
            )
        )
        hints.append(
            Hint(
                field="states_of_operation",
                values=(resolved,),
                confidence=Confidence.CERTAIN,
                source="GSTIN characters 1-2",
                explanation=f"holding a GSTIN in {subdivision.name} means you operate there",
            )
        )
        if subdivision.superseded_by:
            notes.append(
                f"State code {gstin[:2]} ({subdivision.name}) is historic; mapped to its "
                f"current jurisdiction."
            )
    elif gstin[:2] in {"97", "99"}:
        notes.append("This registration is outside the state series (other territory or a UIN).")
    else:
        notes.append(f"'{gstin[:2]}' is not a GST state code this product recognises.")

    # The middle ten characters are a PAN, so everything a PAN says applies.
    embedded = decode_pan(gstin[2:12])
    if embedded.valid:
        hints.extend(
            Hint(
                field=hint.field,
                values=hint.values,
                confidence=hint.confidence,
                source=f"GSTIN (embedded PAN, {hint.source.lower()})",
                explanation=hint.explanation.replace("your PAN", "the PAN inside your GSTIN"),
            )
            for hint in embedded.hints
        )
    else:
        notes.append("The PAN embedded in this GSTIN does not look valid.")

    if gstin[12] != "1":
        notes.append(
            f"The entity digit is {gstin[12]}, which means this PAN holds more than one "
            f"registration in this state."
        )

    return Reading("GST", gstin, valid=True, hints=tuple(hints), notes=tuple(notes))


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

_DECODERS = {
    "PAN": decode_pan,
    "CIN": decode_cin,
    "LLPIN": decode_llpin,
    "GST": decode_gstin,
}

_CONFIDENCE_ORDER = {Confidence.CERTAIN: 0, Confidence.LIKELY: 1, Confidence.POSSIBLE: 2}


def sniff(text: str) -> list[tuple[str, str]]:
    """Classify pasted text into ``(registration_type, value)`` pairs by shape.

    So a user can paste whatever they have — a signature block, a letterhead
    footer, three identifiers on three lines — instead of being asked which box
    each one goes in. Shapes do not overlap: a GSTIN is fifteen characters
    starting with two digits, a CIN twenty-one starting with L or U.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for token in re.split(r"[^A-Za-z0-9-]+", text or ""):
        candidate = normalise(token)
        if not candidate or candidate in seen:
            continue
        for kind, pattern in (
            ("CIN", CIN_RE),
            ("GST", GSTIN_RE),
            ("LLPIN", LLPIN_RE),
            ("PAN", PAN_RE),
        ):
            if pattern.match(candidate):
                found.append((kind, candidate))
                seen.add(candidate)
                break
    return found


def read(pairs: Iterable[tuple[str, str]]) -> IdentityReport:
    """Decode several identifiers together and reconcile what they say.

    The reconciliation is the point. One identifier is a hint; two that agree are
    a fact; two that disagree mean somebody mistyped one of them, and saying so is
    more useful than picking a winner.
    """
    readings: list[Reading] = []
    for registration_type, value in pairs:
        decoder = _DECODERS.get(registration_type.upper())
        if decoder is None:
            continue
        readings.append(decoder(value))

    by_field: dict[str, list[Hint]] = {}
    for reading in readings:
        if not reading.valid:
            continue
        for hint in reading.hints:
            by_field.setdefault(hint.field, []).append(hint)

    merged: list[Hint] = []
    conflicts: list[Conflict] = []

    for field_name, hints in by_field.items():
        ranked = sorted(hints, key=lambda h: _CONFIDENCE_ORDER[h.confidence])
        best = ranked[0]
        merged.append(best)

        # Only a disagreement between two *definite* readings is a conflict. A
        # PAN saying "some company" and a CIN saying "private limited" agree
        # perfectly well; the CIN is simply more specific.
        definite = [hint for hint in ranked if hint.is_definite]
        distinct = {hint.value for hint in definite}
        if len(distinct) > 1:
            sources = " and ".join(sorted({hint.source for hint in definite}))
            conflicts.append(
                Conflict(
                    field=field_name,
                    explanation=(
                        f"{sources} disagree about {field_name.replace('_', ' ')}: "
                        f"{', '.join(str(v) for v in sorted(distinct, key=str))}. "
                        f"One of the identifiers is probably mistyped."
                    ),
                )
            )
            continue

        # A definite reading that contradicts a broader one — a CIN saying LLP
        # while the PAN says a company — is worth flagging too.
        for hint in ranked[1:]:
            if best.is_definite and best.value not in hint.values and hint.values:
                conflicts.append(
                    Conflict(
                        field=field_name,
                        explanation=(
                            f"{best.source} says {best.value}, but {hint.source} points to "
                            f"{' or '.join(str(v) for v in hint.values)}."
                        ),
                    )
                )
                break

    merged.sort(key=lambda hint: (_CONFIDENCE_ORDER[hint.confidence], hint.field))
    notes = tuple(note for reading in readings for note in reading.notes)
    return IdentityReport(
        readings=tuple(readings),
        hints=tuple(merged),
        conflicts=tuple(conflicts),
        notes=notes,
    )
