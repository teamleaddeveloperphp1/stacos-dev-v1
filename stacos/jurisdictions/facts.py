"""
The fact registry.

Applicability rules are data, not code — ``if industry == "pharma"`` is banned
outright. What makes that workable is this registry: every fact an entity profile
can hold is declared here with a type, a unit, and its allowed values, so a rule
referencing an unknown fact or comparing a set to a scalar is caught at authoring
time rather than producing a silently wrong calendar.

It also does the work Foundation needs immediately: validating what goes into
``EntityProfile.facts``. An unvalidated JSONB blob is how a fact namespace rots
into ``has_boiler``, ``hasBoiler`` and ``boiler_present`` inside a year.

Later it becomes the schema behind the platform-admin rule editor, which is why
labels and help text live here rather than in a template.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

__all__ = ["REGISTRY", "FactDef", "FactRegistry", "FactSource", "FactType"]


class FactType(StrEnum):
    BOOL = "BOOL"
    INT = "INT"
    DECIMAL = "DECIMAL"
    DATE = "DATE"
    STRING = "STRING"
    ENUM = "ENUM"
    SET_ENUM = "SET_ENUM"
    SET_STRING = "SET_STRING"


class FactSource(StrEnum):
    #: Answered by the user in the onboarding profile wizard.
    PROFILE = "PROFILE"
    #: Derived from registrations the entity holds.
    REGISTRATION = "REGISTRATION"
    #: Derived from premises the entity operates.
    PREMISES = "PREMISES"
    #: Computed from other facts by a pure function.
    DERIVED = "DERIVED"


@dataclass(frozen=True, slots=True)
class FactDef:
    key: str
    label: str
    type: FactType
    source: FactSource = FactSource.PROFILE
    unit: str | None = None
    allowed_values: tuple[str, ...] | None = None
    nullable: bool = True
    #: True when the value changes over time and history matters — turnover for
    #: FY 2025-26 is only known once books close, and gets restated. Facts marked
    #: here are stored in ``EntityFactValue`` with validity windows, so the
    #: calendar can ask "what was true for the July 2026 period" rather than
    #: "what is true today".
    effective_dated: bool = False
    help_text: str = ""
    depends_on: tuple[str, ...] = field(default_factory=tuple)

    def validate(self, value: Any) -> None:
        """Raise ``ValueError`` if ``value`` is not acceptable for this fact."""
        if value is None:
            if not self.nullable:
                raise ValueError(f"{self.key} is required.")
            return

        match self.type:
            case FactType.BOOL:
                if not isinstance(value, bool):
                    raise ValueError(f"{self.key} must be true or false, got {value!r}.")
            case FactType.INT:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{self.key} must be a whole number, got {value!r}.")
            case FactType.DECIMAL:
                if not isinstance(value, int | float | Decimal | str):
                    raise ValueError(f"{self.key} must be a number, got {value!r}.")
                try:
                    Decimal(str(value))
                except Exception:
                    raise ValueError(f"{self.key} must be a number, got {value!r}.") from None
            case FactType.ENUM:
                if self.allowed_values and value not in self.allowed_values:
                    raise ValueError(
                        f"{self.key}={value!r} is not one of: {', '.join(self.allowed_values)}."
                    )
            case FactType.SET_ENUM | FactType.SET_STRING:
                if not isinstance(value, list | tuple | set | frozenset):
                    raise ValueError(f"{self.key} must be a list, got {value!r}.")
                if self.type is FactType.SET_ENUM and self.allowed_values:
                    unknown = [v for v in value if v not in self.allowed_values]
                    if unknown:
                        raise ValueError(f"{self.key} contains unknown values: {unknown}.")
            case FactType.STRING | FactType.DATE:
                if not isinstance(value, str):
                    raise ValueError(f"{self.key} must be a string, got {value!r}.")


class FactRegistry:
    def __init__(self, definitions: list[FactDef] | None = None) -> None:
        self._facts: dict[str, FactDef] = {}
        for definition in definitions or []:
            self.register(definition)

    def register(self, definition: FactDef) -> FactDef:
        if definition.key in self._facts and self._facts[definition.key] != definition:
            raise ValueError(f"Fact {definition.key!r} is already registered differently.")
        self._facts[definition.key] = definition
        return definition

    def get(self, key: str) -> FactDef | None:
        return self._facts.get(key)

    def __contains__(self, key: object) -> bool:
        return key in self._facts

    def __iter__(self) -> Iterator[FactDef]:
        return iter(sorted(self._facts.values(), key=lambda f: f.key))

    def __len__(self) -> int:
        return len(self._facts)

    def keys(self) -> frozenset[str]:
        return frozenset(self._facts)

    def validate_facts(self, facts: dict[str, Any], *, strict: bool = True) -> list[str]:
        """Validate a fact dictionary, returning a list of problems.

        :param strict: also reject keys that are not registered. Turn this off
            when loading a profile authored against a newer catalog than this
            deployment knows about — better to ignore a fact than to refuse the
            whole profile.
        """
        problems: list[str] = []
        for key, value in facts.items():
            definition = self._facts.get(key)
            if definition is None:
                if strict:
                    problems.append(f"Unknown fact {key!r}.")
                continue
            try:
                definition.validate(value)
            except ValueError as exc:
                problems.append(str(exc))
        return problems

    def effective_dated_keys(self) -> frozenset[str]:
        return frozenset(f.key for f in self if f.effective_dated)


# ---------------------------------------------------------------------------
# The registry itself
#
# India-shaped in its *values* (state codes, entity types) but not in its
# structure. Another jurisdiction adds its own entries; nothing here is special
# cased in code.
# ---------------------------------------------------------------------------

IN_STATE_CODES: tuple[str, ...] = (
    "IN-AN",
    "IN-AP",
    "IN-AR",
    "IN-AS",
    "IN-BR",
    "IN-CH",
    "IN-CT",
    "IN-DH",
    "IN-DL",
    "IN-GA",
    "IN-GJ",
    "IN-HP",
    "IN-HR",
    "IN-JH",
    "IN-JK",
    "IN-KA",
    "IN-KL",
    "IN-LA",
    "IN-LD",
    "IN-MH",
    "IN-ML",
    "IN-MN",
    "IN-MP",
    "IN-MZ",
    "IN-NL",
    "IN-OR",
    "IN-PB",
    "IN-PY",
    "IN-RJ",
    "IN-SK",
    "IN-TG",
    "IN-TN",
    "IN-TR",
    "IN-UP",
    "IN-UT",
    "IN-WB",
)

ENTITY_TYPES: tuple[str, ...] = (
    "PVT_LTD",
    "PUBLIC_LTD",
    "OPC",
    "LLP",
    "PARTNERSHIP",
    "PROPRIETORSHIP",
    "TRUST",
    "SOCIETY",
    "SECTION_8",
    "BRANCH_OFFICE",
    "LIAISON_OFFICE",
    "COOPERATIVE",
    "HUF",
)

REGISTRATION_TYPES: tuple[str, ...] = (
    "PAN",
    "TAN",
    "GST",
    "CIN",
    "LLPIN",
    "PF",
    "ESIC",
    "PT_EC",
    "PT_RC",
    "IEC",
    "UDYAM",
    "FACTORY_LICENCE",
    "SHOPS_ESTAB",
    "PCB_CONSENT",
    "DRUG_LICENCE",
    "FSSAI",
    "LEGAL_METROLOGY",
    "BIS",
    "TRADE_LICENCE",
    "FIRE_NOC",
    "CONTRACT_LABOUR",
    "FCRN",
    "FIRM_REGN",
    "TRUST_REGN",
    "SOCIETY_REGN",
    "COOP_REGN",
    "RBI_ROC_DETAILS",
    "RBI_APPROVAL",
    "KARTA_PAN",
    "12AB",
    "80G",
    "DARPAN",
)

PREMISES_TYPES: tuple[str, ...] = (
    "REGISTERED_OFFICE",
    "CORPORATE_OFFICE",
    "BRANCH",
    "FACTORY",
    "PLANT",
    "WAREHOUSE",
    "RETAIL_STORE",
    "SITE",
)

REGISTRY = FactRegistry(
    [
        # -- Identity -------------------------------------------------------
        FactDef(
            "country",
            "Country",
            FactType.ENUM,
            nullable=False,
            help_text="ISO-3166-1 alpha-2 code of the entity's home jurisdiction.",
        ),
        FactDef(
            "entity_type", "Entity type", FactType.ENUM, allowed_values=ENTITY_TYPES, nullable=False
        ),
        FactDef("incorporation_date", "Date of incorporation", FactType.DATE),
        FactDef(
            "registered_office_state",
            "Registered office state",
            FactType.ENUM,
            allowed_values=IN_STATE_CODES,
        ),
        FactDef(
            "states_of_operation",
            "States of operation",
            FactType.SET_ENUM,
            allowed_values=IN_STATE_CODES,
            nullable=False,
        ),
        # -- Scale (effective-dated: these change, and get restated) ---------
        FactDef(
            "aggregate_turnover",
            "Annual aggregate turnover",
            FactType.DECIMAL,
            unit="INR",
            effective_dated=True,
            help_text="Turnover for the most recently closed financial year.",
        ),
        FactDef(
            "employee_count",
            "Employees on payroll",
            FactType.INT,
            unit="COUNT",
            effective_dated=True,
        ),
        FactDef(
            "contractor_count", "Contract workers", FactType.INT, unit="COUNT", effective_dated=True
        ),
        FactDef(
            "women_employees_count",
            "Women employees",
            FactType.INT,
            unit="COUNT",
            effective_dated=True,
            help_text="Drives POSH committee and Maternity Benefit obligations.",
        ),
        FactDef(
            "paid_up_capital",
            "Paid-up share capital",
            FactType.DECIMAL,
            unit="INR",
            effective_dated=True,
        ),
        FactDef("net_worth", "Net worth", FactType.DECIMAL, unit="INR", effective_dated=True),
        FactDef("net_profit", "Net profit", FactType.DECIMAL, unit="INR", effective_dated=True),
        # -- Sector ----------------------------------------------------------
        FactDef(
            "nic_code",
            "NIC industry code",
            FactType.STRING,
            help_text="National Industrial Classification code.",
        ),
        FactDef("sector", "Sector", FactType.STRING),
        FactDef("sub_sector", "Sub-sector", FactType.STRING),
        # -- Derived from registrations and premises -------------------------
        FactDef(
            "registrations",
            "Registrations held",
            FactType.SET_ENUM,
            source=FactSource.REGISTRATION,
            allowed_values=REGISTRATION_TYPES,
            nullable=False,
        ),
        FactDef(
            "premises_types",
            "Types of premises operated",
            FactType.SET_ENUM,
            source=FactSource.PREMISES,
            allowed_values=PREMISES_TYPES,
            nullable=False,
        ),
        FactDef(
            "has_factory_premises",
            "Operates a factory",
            FactType.BOOL,
            source=FactSource.DERIVED,
            depends_on=("premises_types",),
        ),
        FactDef(
            "turnover_band",
            "Turnover band",
            FactType.ENUM,
            source=FactSource.DERIVED,
            allowed_values=("LT_40L", "LT_1_5CR", "LT_5CR", "LT_50CR", "LT_250CR", "GTE_250CR"),
            depends_on=("aggregate_turnover",),
        ),
        # -- Tax posture -----------------------------------------------------
        FactDef(
            "gst_scheme",
            "GST scheme",
            FactType.ENUM,
            allowed_values=(
                "REGULAR",
                "COMPOSITION",
                "CASUAL",
                "ISD",
                "TDS_DEDUCTOR",
                "NON_RESIDENT",
            ),
        ),
        FactDef(
            "qrmp_opted",
            "Opted for QRMP",
            FactType.BOOL,
            help_text="Quarterly Return, Monthly Payment scheme under GST.",
        ),
        FactDef(
            "fy_convention",
            "Financial year convention",
            FactType.STRING,
            help_text="Resolved from the jurisdiction pack; never hardcoded.",
        ),
        # -- Flags that drive whole families of obligations -------------------
        FactDef("is_listed", "Listed on a stock exchange", FactType.BOOL),
        FactDef("has_foreign_shareholding", "Has foreign shareholding", FactType.BOOL),
        FactDef("has_csr_obligation", "Subject to CSR", FactType.BOOL),
        FactDef("has_msme_vendors", "Buys from MSME vendors", FactType.BOOL),
        FactDef("has_related_party_txns", "Has related-party transactions", FactType.BOOL),
        FactDef("has_export_import", "Exports or imports", FactType.BOOL),
        FactDef("has_ecommerce_sales", "Sells through e-commerce", FactType.BOOL),
        FactDef("is_startup_dpiit", "DPIIT-recognised startup", FactType.BOOL),
        FactDef("has_esop", "Operates an ESOP", FactType.BOOL),
        FactDef("deals_in_hazardous_material", "Handles hazardous material", FactType.BOOL),
        FactDef("has_boiler", "Operates a boiler", FactType.BOOL),
        FactDef("has_lift", "Operates a lift", FactType.BOOL),
        FactDef("has_canteen", "Operates a canteen", FactType.BOOL),
        FactDef("is_dormant", "Dormant company", FactType.BOOL),
        FactDef("has_fcra", "Holds FCRA registration", FactType.BOOL),
    ]
)
