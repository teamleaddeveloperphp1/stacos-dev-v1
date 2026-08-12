"""
The persona library: a dozen entities that stand in for the Indian SME market.

These exist to answer questions no unit test can. "Does this rule ever fire?" and
"does it fire for everyone?" are properties of a rule *against a population*, and
without a population you cannot detect a definition that is dead (a typo nobody
notices for six months) or one that is undiscriminating (a missing clause that
puts a factory return on a software consultancy's calendar).

They earn their keep three times over: as the input to the catalog's semantic
validation, as the fixtures behind golden-file scenario tests, and as sales demo
data — a prospect who sees their own shape of business already on screen is being
shown something true rather than a mock-up.

Pure data, no Django. The engine consumes these directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from stacos.engine.planner import EntityProfileView
from stacos.engine.types import InstanceScope, ScopeRef

__all__ = ["PERSONAS", "Persona", "persona_profiles"]


@dataclass(frozen=True, slots=True)
class Persona:
    """One archetypal entity, described the way the engine wants to read it."""

    key: str
    name: str
    description: str
    entity_type: str
    facts: dict[str, Any]
    jurisdictions: frozenset[str]
    registrations: tuple[tuple[str, str], ...] = ()
    premises: tuple[tuple[str, str], ...] = ()
    incorporation_date: date | None = None
    events: dict[str, date] = field(default_factory=dict)

    def to_profile(self) -> EntityProfileView:
        """Build the frozen view the planner takes.

        Note what ``ScopeRef.label`` carries: the registration *type*, because
        that is the field the planner's fan-out matches ``scope_selector`` on. The
        human-readable identifier belongs on the materialised row, not here — the
        engine has no business knowing what a GSTIN looks like.
        """
        return EntityProfileView(
            entity_id=self.key,
            country="IN",
            facts={
                "country": "IN",
                "entity_type": self.entity_type,
                "registrations": [kind for kind, _ in self.registrations],
                "premises_types": [kind for kind, _ in self.premises],
                "states_of_operation": sorted(self.jurisdictions),
                **self.facts,
            },
            jurisdictions=self.jurisdictions,
            registrations=tuple(
                ScopeRef(
                    kind=InstanceScope.REGISTRATION,
                    ref=f"{self.key}:{kind}:{where}",
                    label=kind,
                    jurisdiction=where,
                )
                for kind, where in self.registrations
            ),
            premises=tuple(
                ScopeRef(
                    kind=InstanceScope.PREMISES,
                    ref=f"{self.key}:{kind}:{where}",
                    label=kind,
                    jurisdiction=where,
                )
                for kind, where in self.premises
            ),
            events=dict(self.events),
            incorporation_date=self.incorporation_date,
        )


#: Deliberately spread across the axes that actually change a calendar: entity
#: type, turnover band, headcount, number of states, factory versus office,
#: foreign shareholding, listing status. A persona set clustered on one axis
#: would pass the discrimination check while proving nothing.
PERSONAS: tuple[Persona, ...] = (
    Persona(
        key="textile-manufacturer-gj",
        name="Shreeji Textiles Pvt Ltd",
        description="Gujarat textile manufacturer, ₹80 Cr turnover, 200 workers, one plant.",
        entity_type="PVT_LTD",
        facts={
            "aggregate_turnover": 800000000,
            "employee_count": 200,
            "contractor_count": 45,
            "women_employees_count": 60,
            "paid_up_capital": 50000000,
            "net_worth": 350000000,
            "gst_scheme": "REGULAR",
            "qrmp_opted": False,
            "sector": "Textiles",
            "is_listed": False,
            "has_factory_premises": True,
            "has_boiler": True,
            "deals_in_hazardous_material": False,
            "has_msme_vendors": True,
            "has_export_import": True,
            "registered_office_state": "IN-GJ",
        },
        jurisdictions=frozenset({"IN-GJ"}),
        registrations=(
            ("PAN", ""),
            ("TAN", ""),
            ("CIN", ""),
            ("GST", "IN-GJ"),
            ("PF", ""),
            ("ESIC", ""),
            ("PT_RC", "IN-GJ"),
            ("PT_EC", "IN-GJ"),
            ("FACTORY_LICENCE", "IN-GJ"),
            ("PCB_CONSENT", "IN-GJ"),
            ("IEC", ""),
            ("UDYAM", ""),
        ),
        premises=(("FACTORY", "IN-GJ"), ("REGISTERED_OFFICE", "IN-GJ")),
        incorporation_date=date(2011, 6, 14),
        events={"AGM_DATE": date(2026, 9, 25), "BOARD_MEETING": date(2026, 5, 20)},
    ),
    Persona(
        key="saas-startup-ka",
        name="Nimbus Software Pvt Ltd",
        description="Bengaluru SaaS startup, ₹6 Cr turnover, 40 staff, foreign shareholding.",
        entity_type="PVT_LTD",
        facts={
            "aggregate_turnover": 60000000,
            "employee_count": 40,
            "women_employees_count": 14,
            "paid_up_capital": 1000000,
            "net_worth": 90000000,
            "gst_scheme": "REGULAR",
            "qrmp_opted": False,
            "sector": "Information technology",
            "is_listed": False,
            "has_foreign_shareholding": True,
            "is_startup_dpiit": True,
            "has_esop": True,
            "has_export_import": True,
            "has_factory_premises": False,
            "registered_office_state": "IN-KA",
        },
        jurisdictions=frozenset({"IN-KA"}),
        registrations=(
            ("PAN", ""),
            ("TAN", ""),
            ("CIN", ""),
            ("GST", "IN-KA"),
            ("PF", ""),
            ("PT_RC", "IN-KA"),
            ("PT_EC", "IN-KA"),
            ("SHOPS_ESTAB", "IN-KA"),
            ("UDYAM", ""),
        ),
        premises=(("CORPORATE_OFFICE", "IN-KA"), ("REGISTERED_OFFICE", "IN-KA")),
        incorporation_date=date(2021, 2, 3),
        events={"AGM_DATE": date(2026, 9, 28)},
    ),
    Persona(
        key="trading-llp-mh",
        name="Kohli Trading LLP",
        description="Mumbai trading LLP under composition, ₹80 lakh turnover, 6 staff.",
        entity_type="LLP",
        facts={
            "aggregate_turnover": 8000000,
            "employee_count": 6,
            "women_employees_count": 2,
            "gst_scheme": "COMPOSITION",
            "qrmp_opted": False,
            "sector": "Wholesale trade",
            "is_listed": False,
            "has_factory_premises": False,
            "registered_office_state": "IN-MH",
        },
        jurisdictions=frozenset({"IN-MH"}),
        registrations=(
            ("PAN", ""),
            ("LLPIN", ""),
            ("GST", "IN-MH"),
            ("PT_EC", "IN-MH"),
            ("SHOPS_ESTAB", "IN-MH"),
            ("UDYAM", ""),
        ),
        premises=(("REGISTERED_OFFICE", "IN-MH"),),
        incorporation_date=date(2018, 8, 21),
    ),
    Persona(
        key="multistate-retailer",
        name="Bharat Retail Ltd",
        description="Listed retailer, ₹600 Cr turnover, GST in six states, 1,800 staff.",
        entity_type="PUBLIC_LTD",
        facts={
            "aggregate_turnover": 6000000000,
            "employee_count": 1800,
            "contractor_count": 400,
            "women_employees_count": 700,
            "paid_up_capital": 400000000,
            "net_worth": 2200000000,
            "net_profit": 180000000,
            "gst_scheme": "REGULAR",
            "qrmp_opted": False,
            "sector": "Retail",
            "is_listed": True,
            "has_csr_obligation": True,
            "has_related_party_txns": True,
            "has_msme_vendors": True,
            "has_ecommerce_sales": True,
            "has_lift": True,
            "has_factory_premises": False,
            "registered_office_state": "IN-MH",
        },
        jurisdictions=frozenset({"IN-MH", "IN-KA", "IN-TN", "IN-DL", "IN-GJ", "IN-WB"}),
        registrations=(
            ("PAN", ""),
            ("TAN", ""),
            ("CIN", ""),
            ("GST", "IN-MH"),
            ("GST", "IN-KA"),
            ("GST", "IN-TN"),
            ("GST", "IN-DL"),
            ("GST", "IN-GJ"),
            ("GST", "IN-WB"),
            ("PF", ""),
            ("ESIC", ""),
            ("PT_RC", "IN-MH"),
            ("PT_RC", "IN-KA"),
            ("PT_EC", "IN-MH"),
            ("SHOPS_ESTAB", "IN-MH"),
            ("LEGAL_METROLOGY", "IN-MH"),
        ),
        premises=(
            ("REGISTERED_OFFICE", "IN-MH"),
            ("RETAIL_STORE", "IN-MH"),
            ("RETAIL_STORE", "IN-KA"),
            ("WAREHOUSE", "IN-TN"),
        ),
        incorporation_date=date(2004, 3, 11),
        events={"AGM_DATE": date(2026, 8, 14)},
    ),
    Persona(
        key="pharma-manufacturer-tg",
        name="Vedant Pharma Pvt Ltd",
        description="Hyderabad pharma manufacturer, hazardous handling, ₹150 Cr turnover.",
        entity_type="PVT_LTD",
        facts={
            "aggregate_turnover": 1500000000,
            "employee_count": 320,
            "contractor_count": 120,
            "women_employees_count": 95,
            "paid_up_capital": 120000000,
            "net_worth": 800000000,
            "net_profit": 62000000,
            "gst_scheme": "REGULAR",
            "qrmp_opted": False,
            "sector": "Pharmaceuticals",
            "is_listed": False,
            "has_csr_obligation": True,
            "has_export_import": True,
            "has_factory_premises": True,
            "deals_in_hazardous_material": True,
            "has_boiler": True,
            "has_canteen": True,
            "registered_office_state": "IN-TG",
        },
        jurisdictions=frozenset({"IN-TG"}),
        registrations=(
            ("PAN", ""),
            ("TAN", ""),
            ("CIN", ""),
            ("GST", "IN-TG"),
            ("PF", ""),
            ("ESIC", ""),
            ("PT_RC", "IN-TG"),
            ("PT_EC", "IN-TG"),
            ("FACTORY_LICENCE", "IN-TG"),
            ("PCB_CONSENT", "IN-TG"),
            ("DRUG_LICENCE", "IN-TG"),
            ("FIRE_NOC", "IN-TG"),
            ("IEC", ""),
        ),
        premises=(("FACTORY", "IN-TG"), ("REGISTERED_OFFICE", "IN-TG")),
        incorporation_date=date(2009, 11, 2),
        events={"AGM_DATE": date(2026, 9, 20)},
    ),
    Persona(
        key="restaurant-chain-dl",
        name="Spice Route Hospitality Pvt Ltd",
        description="Delhi restaurant group under QRMP, FSSAI-licensed, 85 staff.",
        entity_type="PVT_LTD",
        facts={
            "aggregate_turnover": 180000000,
            "employee_count": 85,
            "women_employees_count": 22,
            "paid_up_capital": 5000000,
            "gst_scheme": "REGULAR",
            "qrmp_opted": True,
            "sector": "Food service",
            "is_listed": False,
            "has_factory_premises": False,
            "has_lift": True,
            "registered_office_state": "IN-DL",
        },
        jurisdictions=frozenset({"IN-DL"}),
        registrations=(
            ("PAN", ""),
            ("TAN", ""),
            ("CIN", ""),
            ("GST", "IN-DL"),
            ("PF", ""),
            ("ESIC", ""),
            ("FSSAI", "IN-DL"),
            ("SHOPS_ESTAB", "IN-DL"),
            ("TRADE_LICENCE", "IN-DL"),
            ("FIRE_NOC", "IN-DL"),
        ),
        premises=(("REGISTERED_OFFICE", "IN-DL"), ("RETAIL_STORE", "IN-DL")),
        incorporation_date=date(2016, 5, 30),
        events={"AGM_DATE": date(2026, 9, 26)},
    ),
    Persona(
        key="proprietor-consultant",
        name="Anand Consulting (Proprietorship)",
        description="Single-person consultancy below the GST threshold. The floor case.",
        entity_type="PROPRIETORSHIP",
        facts={
            "aggregate_turnover": 1800000,
            "employee_count": 0,
            "sector": "Professional services",
            "is_listed": False,
            "has_factory_premises": False,
            "registered_office_state": "IN-KA",
        },
        jurisdictions=frozenset({"IN-KA"}),
        registrations=(("PAN", ""),),
        premises=(),
        incorporation_date=date(2019, 4, 1),
    ),
    Persona(
        key="opc-ecommerce",
        name="Craftly OPC Pvt Ltd",
        description="One-person company selling through marketplaces. TCS credits apply.",
        entity_type="OPC",
        facts={
            "aggregate_turnover": 22000000,
            "employee_count": 3,
            "paid_up_capital": 100000,
            "gst_scheme": "REGULAR",
            "qrmp_opted": True,
            "sector": "E-commerce",
            "is_listed": False,
            "has_ecommerce_sales": True,
            "has_factory_premises": False,
            "registered_office_state": "IN-UP",
        },
        jurisdictions=frozenset({"IN-UP"}),
        registrations=(
            ("PAN", ""),
            ("TAN", ""),
            ("CIN", ""),
            ("GST", "IN-UP"),
            ("UDYAM", ""),
        ),
        premises=(("REGISTERED_OFFICE", "IN-UP"),),
        incorporation_date=date(2022, 7, 19),
        events={"AGM_DATE": date(2026, 9, 30)},
    ),
    Persona(
        key="ngo-section8",
        name="Prerna Foundation (Section 8)",
        description="Section 8 company with FCRA registration and foreign grants.",
        entity_type="SECTION_8",
        facts={
            "aggregate_turnover": 45000000,
            "employee_count": 28,
            "women_employees_count": 16,
            "gst_scheme": "REGULAR",
            "sector": "Non-profit",
            "is_listed": False,
            "has_fcra": True,
            "has_foreign_shareholding": False,
            "has_factory_premises": False,
            "registered_office_state": "IN-RJ",
        },
        jurisdictions=frozenset({"IN-RJ"}),
        registrations=(
            ("PAN", ""),
            ("TAN", ""),
            ("CIN", ""),
            ("GST", "IN-RJ"),
            ("PF", ""),
        ),
        premises=(("REGISTERED_OFFICE", "IN-RJ"),),
        incorporation_date=date(2013, 1, 22),
        events={"AGM_DATE": date(2026, 9, 29)},
    ),
    Persona(
        key="logistics-partnership",
        name="Sharma Transport (Partnership)",
        description="Interstate transport partnership. Reverse charge, no company filings.",
        entity_type="PARTNERSHIP",
        facts={
            "aggregate_turnover": 95000000,
            "employee_count": 55,
            "contractor_count": 30,
            "gst_scheme": "REGULAR",
            "qrmp_opted": False,
            "sector": "Logistics",
            "is_listed": False,
            "has_factory_premises": False,
            "registered_office_state": "IN-PB",
        },
        jurisdictions=frozenset({"IN-PB", "IN-HR"}),
        registrations=(
            ("PAN", ""),
            ("TAN", ""),
            ("GST", "IN-PB"),
            ("GST", "IN-HR"),
            ("PF", ""),
            ("ESIC", ""),
        ),
        premises=(("WAREHOUSE", "IN-PB"),),
        incorporation_date=date(2012, 10, 5),
    ),
    Persona(
        key="dormant-holdco",
        name="Meridian Holdings Pvt Ltd",
        description="Dormant holding company. Files the bare statutory minimum.",
        entity_type="PVT_LTD",
        facts={
            "aggregate_turnover": 0,
            "employee_count": 0,
            "paid_up_capital": 100000,
            "is_dormant": True,
            "is_listed": False,
            "has_factory_premises": False,
            "registered_office_state": "IN-MH",
        },
        jurisdictions=frozenset({"IN-MH"}),
        registrations=(("PAN", ""), ("CIN", "")),
        premises=(("REGISTERED_OFFICE", "IN-MH"),),
        incorporation_date=date(2017, 3, 28),
        events={"AGM_DATE": date(2026, 9, 30)},
    ),
    # The three below exist because the dead-rule check demanded them. GSTR-5,
    # GSTR-6 and GSTR-7 are real obligations that fired for nobody in the library,
    # which is the check reporting a gap in the *population* rather than in the
    # rules — and a population that cannot exercise a rule cannot prove it works.
    Persona(
        key="isd-head-office",
        name="Anantha Group — ISD registration",
        description="Head office registered as an input service distributor for six branches.",
        entity_type="PVT_LTD",
        facts={
            "aggregate_turnover": 3200000000,
            "employee_count": 140,
            "paid_up_capital": 250000000,
            "gst_scheme": "ISD",
            "sector": "Diversified",
            "is_listed": False,
            "has_factory_premises": False,
            "registered_office_state": "IN-KA",
        },
        jurisdictions=frozenset({"IN-KA"}),
        registrations=(("PAN", ""), ("TAN", ""), ("CIN", ""), ("GST", "IN-KA"), ("PF", "")),
        premises=(("CORPORATE_OFFICE", "IN-KA"),),
        incorporation_date=date(2008, 6, 17),
        events={"AGM_DATE": date(2026, 9, 22)},
    ),
    Persona(
        key="psu-tds-deductor",
        name="Statecorp Infrastructure Ltd",
        description="State PSU that deducts GST TDS from its contractors.",
        entity_type="PUBLIC_LTD",
        facts={
            "aggregate_turnover": 9000000000,
            "employee_count": 2400,
            "contractor_count": 900,
            "women_employees_count": 480,
            "paid_up_capital": 1000000000,
            "net_worth": 5000000000,
            "net_profit": 220000000,
            "gst_scheme": "TDS_DEDUCTOR",
            "sector": "Infrastructure",
            "is_listed": False,
            "has_csr_obligation": True,
            "has_msme_vendors": True,
            "has_factory_premises": False,
            "registered_office_state": "IN-UP",
        },
        jurisdictions=frozenset({"IN-UP"}),
        registrations=(
            ("PAN", ""),
            ("TAN", ""),
            ("CIN", ""),
            ("GST", "IN-UP"),
            ("PF", ""),
            ("ESIC", ""),
            ("PT_RC", "IN-UP"),
        ),
        premises=(("REGISTERED_OFFICE", "IN-UP"),),
        incorporation_date=date(1998, 4, 20),
        events={"AGM_DATE": date(2026, 9, 18)},
    ),
    Persona(
        key="oidar-nonresident",
        name="Northwind Digital Inc — OIDAR",
        description="Overseas digital-services supplier with a non-resident GST registration.",
        entity_type="BRANCH_OFFICE",
        facts={
            "aggregate_turnover": 300000000,
            "employee_count": 0,
            "gst_scheme": "NON_RESIDENT",
            "sector": "Digital services",
            "is_listed": False,
            "has_ecommerce_sales": True,
            "has_foreign_shareholding": True,
            "has_factory_premises": False,
            "registered_office_state": "IN-MH",
        },
        jurisdictions=frozenset({"IN-MH"}),
        registrations=(("GST", "IN-MH"),),
        premises=(),
        incorporation_date=date(2020, 1, 15),
    ),
    Persona(
        key="branch-office-foreign",
        name="Helvetica AG — India Branch",
        description="Branch office of a foreign company. FEMA reporting, no ROC annual return.",
        entity_type="BRANCH_OFFICE",
        facts={
            "aggregate_turnover": 240000000,
            "employee_count": 35,
            "gst_scheme": "REGULAR",
            "qrmp_opted": False,
            "sector": "Engineering services",
            "is_listed": False,
            "has_foreign_shareholding": True,
            "has_export_import": True,
            "has_factory_premises": False,
            "registered_office_state": "IN-TN",
        },
        jurisdictions=frozenset({"IN-TN"}),
        registrations=(
            ("PAN", ""),
            ("TAN", ""),
            ("GST", "IN-TN"),
            ("PF", ""),
            ("IEC", ""),
        ),
        premises=(("BRANCH", "IN-TN"),),
        incorporation_date=date(2015, 9, 9),
    ),
)


def persona_profiles() -> dict[str, EntityProfileView]:
    """Every persona as an engine profile, keyed by persona key."""
    return {persona.key: persona.to_profile() for persona in PERSONAS}
