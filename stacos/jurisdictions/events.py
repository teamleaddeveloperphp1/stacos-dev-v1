"""
The event type registry.

An event-driven obligation exists because something happened: a director was
appointed, an auditor resigned, a declaration was received. DIR-12 is not due
every quarter — it is due thirty days after each appointment, however many of
those there are in a year, and it does not exist at all until one occurs.

This registry is what makes that expressible as data. A definition names a
``trigger.event_key``; the loader checks the key against this module and refuses
the file otherwise, which is the only thing standing between a typo and sixty
obligations that silently never fire.

**Why this is a separate namespace from the fact registry.** ``REGISTRY.keys()``
is handed to ``validate_rule`` when an applicability rule is checked. Merging
event types into it would make ``fact: DIRECTOR_APPOINTED`` a legal applicability
clause — permanently UNKNOWN for every entity, so an unconfirmed obligation for
everyone, and the dead-rule check would not catch it because UNKNOWN counts as
alive. Same shape, same directory, different namespace.

**Why code rather than the jurisdiction pack.** ``manage.py validatecatalog`` is
a merge gate that runs with no database and no Django. It already resolves
``REGISTRATION_TYPES`` from code even though ``IN.yaml`` also carries them, and
that is the right way round: the pack is contributed data, and a gate whose
vocabulary a contributor can widen is not a gate. The pack mirrors this for the
benefit of the admin UI, and a test fails the build if the two disagree.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from stacos.engine.types import InstanceScope

__all__ = [
    "EVENT_TYPES",
    "EventAttributeDef",
    "EventTypeDef",
    "EventTypeRegistry",
]


@dataclass(frozen=True, slots=True)
class EventAttributeDef:
    """One structured field an event carries, beyond its date.

    These are what ``trigger.when`` filters on. MR-1 and DIR-12 both fire on an
    appointment, but MR-1 only for a managing or whole-time director — expressed
    as a rule over ``role`` rather than as two event types, because "a director
    was appointed" is one thing that happened and modelling it as two invites a
    user to record it twice.
    """

    key: str
    label: str
    type: str = "STRING"
    allowed_values: tuple[str, ...] | None = None
    required: bool = False
    help_text: str = ""

    def validate(self, value: Any) -> str | None:
        """Return a problem, or ``None``."""
        if value is None or value == "":
            return f"{self.label} is required." if self.required else None
        if self.type == "ENUM" and self.allowed_values and value not in self.allowed_values:
            return f"{self.label} must be one of: {', '.join(self.allowed_values)}."
        if self.type == "BOOL" and not isinstance(value, bool):
            return f"{self.label} must be true or false."
        return None


@dataclass(frozen=True, slots=True)
class EventTypeDef:
    """One kind of thing that can happen and put a filing on the calendar."""

    code: str
    label: str
    help_text: str = ""
    #: What the event — and every obligation derived from it — attaches to. A
    #: factory licence is issued for one plant, not for the company.
    scope: InstanceScope = InstanceScope.ENTITY
    #: Whether this can legitimately happen more than once. False makes the UI
    #: offer "correct the date" rather than "record another one".
    recurs: bool = True
    #: Empty means every country. Restricts both the picker and the catalog
    #: check, so an Indian ROC event cannot become the trigger of a UAE rule.
    countries: tuple[str, ...] = ()
    #: What the event is *about*. Non-empty makes the form demand a subject, and
    #: the subject is what distinguishes two events recorded on the same day.
    subject_label: str = ""
    attributes: tuple[EventAttributeDef, ...] = field(default_factory=tuple)

    @property
    def requires_subject(self) -> bool:
        return bool(self.subject_label)

    def attribute_keys(self) -> frozenset[str]:
        return frozenset(a.key for a in self.attributes)

    def applies_to_country(self, country: str) -> bool:
        return not self.countries or country in self.countries


class EventTypeRegistry:
    """Lookup over the declared event types. Deliberately the same shape as
    ``FactRegistry``, so neither has to be learned twice."""

    def __init__(self, definitions: tuple[EventTypeDef, ...] = ()) -> None:
        self._types: dict[str, EventTypeDef] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: EventTypeDef) -> EventTypeDef:
        if definition.code in self._types and self._types[definition.code] != definition:
            raise ValueError(f"Event type {definition.code!r} is already registered differently.")
        self._types[definition.code] = definition
        return definition

    def get(self, code: str) -> EventTypeDef | None:
        return self._types.get(code)

    def __contains__(self, code: object) -> bool:
        return code in self._types

    def __iter__(self) -> Iterator[EventTypeDef]:
        return iter(sorted(self._types.values(), key=lambda e: e.code))

    def __len__(self) -> int:
        return len(self._types)

    def keys(self) -> frozenset[str]:
        return frozenset(self._types)

    def for_country(self, country: str) -> tuple[EventTypeDef, ...]:
        return tuple(e for e in self if e.applies_to_country(country))

    def validate_attributes(self, code: str, values: Mapping[str, Any]) -> list[str]:
        """Check a recorded event's payload. Returns every problem, not the first.

        Unknown keys are rejected: an unvalidated JSONB blob is how one concept
        rots into ``din``, ``DIN`` and ``director_din`` inside a year, and the
        fact registry exists because that already happened once.
        """
        definition = self._types.get(code)
        if definition is None:
            return [f"Unknown event type {code!r}."]

        problems: list[str] = []
        known = {a.key: a for a in definition.attributes}
        for key, value in values.items():
            attribute = known.get(key)
            if attribute is None:
                problems.append(f"{code} has no attribute {key!r}.")
                continue
            problem = attribute.validate(value)
            if problem:
                problems.append(problem)
        for attribute in definition.attributes:
            if attribute.required and attribute.key not in values:
                problems.append(f"{attribute.label} is required.")
        return problems


# ---------------------------------------------------------------------------
# The registry itself
#
# India-shaped in its values, not in its structure — another jurisdiction adds
# its own entries and nothing here is special-cased in code. Mirrored into
# ``catalog/packs/IN.yaml`` for the admin UI; ``tests/unit/test_event_types.py``
# fails the build if the two drift.
# ---------------------------------------------------------------------------

#: Offices a person can be appointed to. One event type carrying a role rather
#: than ten event types: "a director was appointed" is one thing that happened,
#: and splitting it by office invites a user to record it twice.
OFFICER_ROLES: tuple[str, ...] = (
    "DIRECTOR",
    "INDEPENDENT_DIRECTOR",
    "SMALL_SHAREHOLDER_DIRECTOR",
    "NOMINEE_DIRECTOR",
    "MD",
    "WTD",
    "MANAGER",
    "CEO",
    "CFO",
    "COMPANY_SECRETARY",
)

_DIN = EventAttributeDef("din", "DIN", help_text="Director Identification Number, if allotted.")
_ROLE = EventAttributeDef(
    "role",
    "Office held",
    type="ENUM",
    allowed_values=OFFICER_ROLES,
    required=True,
    help_text="Decides what follows. MR-1 is due only for an MD, WTD or manager.",
)
_IS_REAPPOINTMENT = EventAttributeDef(
    "is_reappointment",
    "Reappointment rather than a fresh appointment",
    type="BOOL",
)

EVENT_TYPES = EventTypeRegistry(
    (
        # -- Governance dates the existing catalog already anchors on ---------
        # Registered so the picker can label them and the loader can check them.
        # Their behaviour is unchanged: they stay EVENT_DATE anchors on ANNUAL
        # definitions, where the obligation exists whether or not the date has
        # been recorded. That is the opposite of a trigger, and §2 of the loader
        # keeps the two apart.
        EventTypeDef(
            code="AGM_DATE",
            label="Annual general meeting held",
            help_text=(
                "Unblocks AOC-4, MGT-7 and ADT-1, which are each due a fixed "
                "number of days after the meeting actually took place."
            ),
        ),
        EventTypeDef(
            code="BOARD_MEETING",
            label="Board meeting held",
            help_text="The gap between consecutive meetings is what Section 173 constrains.",
        ),
        EventTypeDef(
            code="INCORPORATION",
            label="Company incorporated",
            recurs=False,
            help_text="Starts the clock on the first board meeting and on INC-20A.",
        ),
        # -- Officers ---------------------------------------------------------
        EventTypeDef(
            code="DIRECTOR_APPOINTED",
            label="Director or KMP appointed",
            help_text=(
                "Any appointment to the board or to a key managerial post. The "
                "office held decides what follows: DIR-12 for all of them, MR-1 "
                "only for a managing or whole-time director or a manager."
            ),
            subject_label="Director or officer",
            attributes=(_DIN, _ROLE, _IS_REAPPOINTMENT),
        ),
        EventTypeDef(
            code="DIRECTOR_RESIGNED",
            label="Director or KMP ceased to hold office",
            help_text=(
                "Resignation, retirement without reappointment, removal or death. "
                "The company files DIR-12; the director may separately file DIR-11."
            ),
            subject_label="Director or officer",
            attributes=(_DIN, _ROLE),
        ),
        # -- Auditors ---------------------------------------------------------
        EventTypeDef(
            code="AUDITOR_APPOINTED",
            label="Auditor appointed",
            subject_label="Audit firm",
            attributes=(
                EventAttributeDef(
                    "appointment_kind",
                    "Kind of appointment",
                    type="ENUM",
                    allowed_values=("FIRST", "AGM", "CASUAL_VACANCY", "REAPPOINTMENT"),
                    required=True,
                ),
                EventAttributeDef("frn", "Firm registration number"),
            ),
        ),
        EventTypeDef(
            code="AUDITOR_RESIGNED",
            label="Auditor resigned",
            help_text=(
                "Starts two clocks at once: the auditor files ADT-3 within thirty "
                "days, and the company must fill the casual vacancy."
            ),
            subject_label="Audit firm",
        ),
        EventTypeDef(
            code="SECRETARIAL_AUDITOR_APPOINTED",
            label="Secretarial auditor appointed",
            subject_label="Practising company secretary",
        ),
        # -- Shareholding and capital -----------------------------------------
        EventTypeDef(
            code="BEN1_RECEIVED",
            label="BEN-1 declaration received",
            help_text="The company then has thirty days to file BEN-2.",
            subject_label="Significant beneficial owner",
        ),
        EventTypeDef(
            code="SHARES_ALLOTTED",
            label="Shares allotted",
            attributes=(
                EventAttributeDef(
                    "has_foreign_allottee",
                    "Allotted to a person resident outside India",
                    type="BOOL",
                    help_text="Drives FC-GPR alongside the ROC filing.",
                ),
            ),
        ),
        EventTypeDef(
            code="SPECIAL_RESOLUTION_PASSED",
            label="Special or specified resolution passed",
            subject_label="Resolution",
        ),
        EventTypeDef(
            code="DIVIDEND_DECLARED",
            label="Dividend declared",
            help_text=(
                "Payment is due within thirty days, and anything unclaimed after "
                "that moves to the Unpaid Dividend Account within seven more."
            ),
        ),
        EventTypeDef(
            code="IEPF_TRANSFER",
            label="Shares transferred to the IEPF",
            help_text="IEPF-4 follows within thirty days of the corporate action.",
        ),
        EventTypeDef(
            code="IEPF5_CLAIM_RECEIVED",
            label="IEPF-5 claim received from a claimant",
            subject_label="Claimant",
        ),
        # -- Transactions needing approval before they happen -------------------
        EventTypeDef(
            code="RELATED_PARTY_TRANSACTION",
            label="Related-party transaction entered into",
            subject_label="Counterparty",
        ),
        EventTypeDef(
            code="LOAN_TO_DIRECTOR",
            label="Loan, guarantee or security provided to a director",
            subject_label="Recipient",
        ),
        EventTypeDef(
            code="SECTION186_TRANSACTION",
            label="Loan, guarantee, security or investment under Section 186",
            subject_label="Counterparty",
        ),
        EventTypeDef(
            code="CSR_AGENCY_ENGAGED",
            label="CSR implementing agency engaged",
            subject_label="Implementing agency",
        ),
        # -- Payroll ------------------------------------------------------------
        EventTypeDef(
            code="EMPLOYEE_JOINED",
            label="Employee joined",
            help_text=(
                "Drives PF enrolment, UAN allotment, Form 11 and KYC — each of "
                "which is a separate step somebody has to actually carry out."
            ),
            subject_label="Employee",
        ),
        EventTypeDef(
            code="EMPLOYEE_EXITED",
            label="Employee left",
            subject_label="Employee",
        ),
        EventTypeDef(
            code="EPF_ESTABLISHMENT_CHANGED",
            label="EPF establishment or ownership particulars changed",
        ),
        # -- Indirect tax -------------------------------------------------------
        EventTypeDef(
            code="GST_LIABILITY_AROSE",
            label="Became liable to register under GST",
            recurs=False,
        ),
        EventTypeDef(
            code="GST_PARTICULARS_CHANGED",
            label="GST registration particulars changed",
            # Per GSTIN, not per company: an address change in Karnataka is
            # nothing to do with the Maharashtra registration.
            scope=InstanceScope.REGISTRATION,
        ),
        EventTypeDef(
            code="GST_CANCELLATION_EFFECTIVE",
            label="GST registration cancelled",
            help_text="GSTR-10, the final return, is due three months later.",
            # One GSTIN surrendered while five others carry on is the ordinary
            # case, so this attaches to the registration.
            scope=InstanceScope.REGISTRATION,
        ),
        # -- Direct tax ---------------------------------------------------------
        EventTypeDef(
            code="PROPERTY_PURCHASED",
            label="Immovable property purchased",
            help_text="Form 26QB is due thirty days after the end of the month of deduction.",
            subject_label="Seller",
        ),
        EventTypeDef(
            code="RENT_TDS_DEDUCTED",
            label="TDS deducted on rent by an individual or HUF",
            subject_label="Landlord",
        ),
        EventTypeDef(
            code="SPECIFIED_PAYMENT_TDS_DEDUCTED",
            label="TDS deducted on a specified payment by an individual or HUF",
            subject_label="Payee",
        ),
        EventTypeDef(
            code="VDA_TRANSFER",
            label="Virtual digital asset transferred",
            subject_label="Counterparty",
        ),
        EventTypeDef(
            code="TDS_DEFAULT_NOTICE",
            label="TDS default or demand received",
            subject_label="Notice reference",
        ),
        EventTypeDef(
            code="TREATY_BENEFIT_CLAIMED",
            label="Tax treaty benefit claimed",
            subject_label="Non-resident payee",
        ),
        EventTypeDef(
            code="TRUST_REGISTRATION_DUE",
            label="Trust or institution registration falls due",
            help_text="Provisional registration, renewal, conversion or a change in objects.",
        ),
    )
)
