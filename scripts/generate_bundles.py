"""
Generate the curated compliance packs in ``catalog/bundles/``.

Like ``generate_definitions.py``, a one-off checked in for provenance. The
membership of each pack is *derived* from the catalog's own facets rather than
listed by hand, so adding a definition tagged ``csr`` puts it in the CSR pack
without anybody remembering to. Re-run it after adding definitions.

    uv run python scripts/generate_bundles.py
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFINITIONS = ROOT / "catalog" / "definitions"
BUNDLES = ROOT / "catalog" / "bundles"

Predicate = Callable[[dict[str, Any]], bool]


def load_all() -> list[dict[str, Any]]:
    return [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(DEFINITIONS.rglob("*.yaml"))
    ]


ALL = load_all()


def codes_where(predicate: Predicate) -> list[str]:
    return sorted(raw["code"] for raw in ALL if predicate(raw))


def has_tag(*tags: str) -> Predicate:
    return lambda raw: any(tag in (raw.get("tags") or []) for tag in tags)


def has_sector(*tags: str) -> Predicate:
    return lambda raw: any(tag in (raw.get("sector_tags") or []) for tag in tags)


def in_family(*names: str) -> Predicate:
    return lambda raw: raw.get("family") in names


def in_category(*names: str) -> Predicate:
    return lambda raw: raw.get("category") in names


def entity_is(*types: str) -> dict[str, Any]:
    return {
        "all": [
            {
                "fact": "entity_type",
                "op": "in",
                "value": list(types),
                "explain": "of your legal form",
            }
        ]
    }


def holds(registration: str, explain: str) -> dict[str, Any]:
    return {"fact": "registrations", "op": "includes", "value": registration, "explain": explain}


SPECS: list[dict[str, Any]] = []


def bundle(
    code: str,
    name: str,
    summary: str,
    rationale: str,
    definitions: list[str],
    *,
    suggest: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> None:
    document: dict[str, Any] = {
        "code": code,
        "country": "IN",
        "name": name,
        "summary": summary,
        "rationale": rationale,
    }
    if tags:
        document["tags"] = tags
    if suggest:
        document["suggest_for"] = suggest
    document["definitions"] = definitions
    SPECS.append(document)


# --- The packs --------------------------------------------------------------

ROC_ANNUAL_MARKERS = (
    "AOC4",
    "MGT7",
    "ADT1",
    "AGM",
    "BOARDS-REPORT",
    "BOARD-REPORT",
    "ANNUAL-RETURN",
    "FINANCIAL-STATEMENT",
    "AUDITORS-REPORT",
    "STATUTORY-REGISTERS",
    "DIR3-KYC",
    "DIRECTOR-ANNUAL",
    "MBP1",
    "SHAREHOLDING",
    "MEMBER-LIST",
    "DIRECTOR-KMP-ANNUAL",
)
EXCLUDED_FROM_PVT = ("NBFC", "CFS", "XBRL", "MGT7A")

bundle(
    "IN-PACK-PVT-ROC-ANNUAL",
    "Private limited — annual ROC kit",
    "Everything a private limited company does with the Registrar once a year, from "
    "approving the accounts to filing the annual return.",
    "The default starting point for a private company. If you file nothing else with the "
    "MCA, you file these.",
    [
        code
        for code in codes_where(in_family("ROC"))
        if any(marker in code for marker in ROC_ANNUAL_MARKERS)
        and not any(excluded in code for excluded in EXCLUDED_FROM_PVT)
    ],
    suggest=entity_is("PVT_LTD", "OPC"),
    tags=["roc", "annual-filing"],
)

bundle(
    "IN-PACK-DIRECTOR-CHANGES",
    "Director and KMP changes",
    "The filings that follow an appointment, a resignation or a change of office — each "
    "one triggered by the event rather than by the calendar.",
    "Add this if your board ever changes. Nothing appears until you record an appointment "
    "or a resignation, and then the right forms appear with the right deadlines.",
    codes_where(
        lambda raw: raw.get("trigger_kind") == "EVENT_DRIVEN" and has_tag("director", "kmp")(raw)
    ),
    suggest=entity_is("PVT_LTD", "PUBLIC_LTD", "OPC", "SECTION_8"),
    tags=["roc", "director", "kmp"],
)

bundle(
    "IN-PACK-AUDITOR-LIFECYCLE",
    "Auditor appointment and resignation",
    "Consent and eligibility before appointment, ADT-1 after it, and the two clocks that "
    "start when an auditor resigns.",
    "Add this if you are appointing, reappointing or replacing an auditor. The casual "
    "vacancy rules in particular catch people out.",
    codes_where(has_tag("auditor")),
    suggest=entity_is("PVT_LTD", "PUBLIC_LTD", "OPC", "SECTION_8"),
    tags=["roc", "auditor"],
)

bundle(
    "IN-PACK-EMPLOYER-PAYROLL",
    "Employer — PF and ESIC",
    "Monthly contributions and returns for provident fund and employees' state insurance, "
    "plus the member-level steps that follow a joiner or a leaver.",
    "Add this as soon as you have employees on a payroll. The monthly filings are the "
    "obvious part; the per-employee enrolment steps are the ones that get missed.",
    codes_where(has_tag("epf", "esic")),
    suggest={
        "any": [
            holds("PF", "you hold an EPFO establishment code"),
            holds("ESIC", "you hold an ESIC employer code"),
        ]
    },
    tags=["epf", "esic", "payroll"],
)

bundle(
    "IN-PACK-GST",
    "GST returns and registration",
    "Outward-supply and summary returns, the annual return and reconciliation, and the "
    "registration lifecycle around them.",
    "The standard GST set. Which of the monthly and quarterly returns actually apply "
    "depends on your scheme and whether you have opted into QRMP.",
    codes_where(lambda raw: has_tag("gst")(raw) or in_family("GST")(raw)),
    suggest={"all": [holds("GST", "you hold a GST registration")]},
    tags=["gst"],
)

bundle(
    "IN-PACK-CSR",
    "CSR — spending, reporting and CSR-2",
    "The CSR obligations that arrive together once a company crosses the thresholds: the "
    "spending itself, the unspent-amount rules, the Board's Report disclosures and the "
    "annual filing.",
    "Add this if your company meets the net worth, turnover or profit thresholds. The "
    "unspent-amount rules are where the money is.",
    codes_where(has_tag("csr")),
    suggest={
        "all": [
            {
                "fact": "has_csr_obligation",
                "op": "eq",
                "value": True,
                "explain": "you meet the CSR thresholds",
            }
        ]
    },
    tags=["roc", "csr"],
)

bundle(
    "IN-PACK-NGO-TRUST",
    "Trust, society or Section 8 company",
    "Registration and revalidation under 12A and 80G, the donation statement and "
    "certificates, accumulation of income, and the audit reports that go with them.",
    "Add this if you are a charitable institution. Missing the registration windows does "
    "not just cost a fee — it costs the exemption.",
    codes_where(has_sector("ngo", "trust")),
    suggest=entity_is("TRUST", "SOCIETY", "SECTION_8"),
    tags=["income-tax", "exemption", "donation"],
)

bundle(
    "IN-PACK-TRANSFER-PRICING",
    "International group — transfer pricing",
    "Contemporaneous documentation, the accountant's report, and the master file and "
    "country-by-country chain where the group is large enough.",
    "Add this if you transact with related parties abroad. The documentation has to be "
    "contemporaneous, so it cannot be caught up once an assessment arrives.",
    codes_where(has_tag("transfer-pricing")),
    suggest={
        "all": [
            {
                "fact": "has_foreign_shareholding",
                "op": "eq",
                "value": True,
                "explain": "you are part of an international group",
            }
        ]
    },
    tags=["income-tax", "transfer-pricing"],
)

bundle(
    "IN-PACK-FACTORY",
    "Factory occupier",
    "Licence renewal, the annual and half-yearly returns, and the safety obligations that "
    "come with running a plant.",
    "Add this if you operate a factory. Most of it is per premises, so a business with "
    "three plants gets three of each.",
    codes_where(
        lambda raw: (
            in_family("Factories", "Plant safety")(raw) or raw.get("instance_scope") == "PREMISES"
        )
    ),
    suggest={
        "all": [
            {
                "fact": "has_factory_premises",
                "op": "eq",
                "value": True,
                "explain": "you operate a factory or plant",
            }
        ]
    },
    tags=["factories", "plant-safety"],
)

bundle(
    "IN-PACK-ENVIRONMENT",
    "Environmental consents and waste",
    "Consent to operate, the environmental statement, and the waste streams — hazardous, "
    "e-waste and plastic — that each carry their own return.",
    "Add this if you hold a pollution control board consent or handle any regulated waste stream.",
    codes_where(in_category("ENVIRONMENT")),
    suggest={
        "any": [
            holds("PCB_CONSENT", "you hold a pollution control board consent"),
            {
                "fact": "deals_in_hazardous_material",
                "op": "eq",
                "value": True,
                "explain": "you handle hazardous material",
            },
        ]
    },
    tags=["environment", "waste", "pollution"],
)

bundle(
    "IN-PACK-EXPORTER",
    "Exporter and importer",
    "The FEMA reporting an exporting business accumulates — realisation reconciliation, "
    "software export declarations and the annual foreign assets return — plus the IEC "
    "that underpins it.",
    "Add this if you export or import. Caution-listing by the RBI is the consequence "
    "nobody expects until it happens.",
    codes_where(lambda raw: in_category("FEMA_RBI")(raw) or "IEC" in raw.get("code", "")),
    suggest={
        "all": [
            {
                "fact": "has_export_import",
                "op": "eq",
                "value": True,
                "explain": "you export or import",
            }
        ]
    },
    tags=["fema", "rbi"],
)

bundle(
    "IN-PACK-TDS-DEDUCTOR",
    "TDS and TCS deductor",
    "Monthly payments, quarterly returns, the certificates that follow them, and the "
    "reconciliation that catches a default before it becomes a demand.",
    "Add this if you hold a TAN. The quarterly returns are the visible part; the "
    "reconciliation is what stops a mismatch turning into a Section 201 demand.",
    codes_where(in_family("TDS")),
    suggest={"all": [holds("TAN", "you hold a TAN")]},
    tags=["tds", "tcs", "withholding"],
)

bundle(
    "IN-PACK-LLP",
    "LLP — annual filings",
    "Form 8 and Form 11, the two filings every LLP makes, plus the income tax return that "
    "goes with them.",
    "The whole of an LLP's annual compliance in one place. Shorter than a company's, and "
    "the penalties for missing it are not.",
    codes_where(lambda raw: "LLP" in raw.get("code", "") or raw.get("code") == "IN-IT-ITR5"),
    suggest=entity_is("LLP"),
    tags=["roc", "annual-filing"],
)

bundle(
    "IN-PACK-LISTED",
    "Listed company — additional obligations",
    "What listing adds on top of the ordinary company calendar: secretarial audit, the "
    "certified annual return, remuneration disclosures and the committee cycle.",
    "Add this if your shares are listed. Everything here is in addition to the ordinary "
    "ROC kit, not instead of it.",
    codes_where(has_sector("listed")) + codes_where(has_tag("secretarial-audit")),
    suggest={
        "all": [{"fact": "is_listed", "op": "eq", "value": True, "explain": "you are listed"}]
    },
    tags=["roc", "governance"],
)


def main() -> int:
    BUNDLES.mkdir(parents=True, exist_ok=True)
    written = 0
    for document in SPECS:
        definitions = sorted(set(document["definitions"]))
        if not definitions:
            print(f"SKIPPED (nothing matched): {document['code']}")
            continue
        document["definitions"] = definitions
        name = document["code"].removeprefix("IN-PACK-").lower()
        path = BUNDLES / f"{name}.yaml"
        path.write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100),
            encoding="utf-8",
        )
        print(f"{document['code']:30s} {len(definitions):3d} definitions")
        written += 1
    print(f"\n{written} bundles written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
