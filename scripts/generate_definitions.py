"""
Generate catalog YAML from ``catalog/import/in-compliance-master.csv``.

A one-off, checked in for provenance rather than run at deploy time. It reads the
compliance sheet and emits one file per row under ``catalog/definitions/``; the
output is then reviewed and committed like any other catalog change.

**Why there is a spec table below instead of inference.** The sheet's "Due Date"
column is prose written for a human — "Event-based — within 30 days of
appointment", "First AGM: within 9 months from close of first financial year.
Thereafter: within 6 months…". Parsing that into an anchor and an offset would be
a guessing machine whose failures are silent and wrong by law. So the sheet
supplies what a sheet is good at — the code, the title, the statutory reference,
the penalty, verbatim — and every rule is stated explicitly here, once, by
somebody who read the provision.

Run with::

    uv run python scripts/generate_definitions.py [--check]

``--check`` writes nothing and reports which rows have no spec, which is how you
find out the sheet grew.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "catalog" / "import" / "in-compliance-master.csv"
DEFINITIONS = ROOT / "catalog" / "definitions"

REVIEWED_BY = "Skorydov compliance desk"
REVIEWED_AT = "2026-09-03"


# ---------------------------------------------------------------------------
# Rule fragments, named once
# ---------------------------------------------------------------------------

COMPANY = {
    "all": [
        {
            "fact": "entity_type",
            "op": "in",
            "value": ["PVT_LTD", "PUBLIC_LTD", "OPC", "SECTION_8"],
            "explain": "you are a company",
        }
    ]
}

#: OPC has no general meeting and a single member, so anything about members,
#: quorum or a general meeting excludes it.
COMPANY_WITH_MEMBERS = {
    "all": [
        {
            "fact": "entity_type",
            "op": "in",
            "value": ["PVT_LTD", "PUBLIC_LTD", "SECTION_8"],
            "explain": "you are a company that holds general meetings",
        }
    ]
}

LISTED_OR_LARGE = {
    "all": [
        {
            "fact": "entity_type",
            "op": "in",
            "value": ["PVT_LTD", "PUBLIC_LTD", "SECTION_8"],
            "explain": "you are a company",
        },
        {
            "any": [
                {"fact": "is_listed", "op": "eq", "value": True, "explain": "you are listed"},
                {
                    "fact": "paid_up_capital",
                    "op": "gte",
                    "value": 100000000,
                    "explain": "your paid-up capital is ₹10 crore or more",
                },
                {
                    "fact": "aggregate_turnover",
                    "op": "gte",
                    "value": 2500000000,
                    "explain": "your turnover is ₹250 crore or more",
                },
            ]
        },
    ]
}

CSR_LIABLE = {
    "all": [
        {
            "fact": "entity_type",
            "op": "in",
            "value": ["PVT_LTD", "PUBLIC_LTD", "SECTION_8"],
            "explain": "you are a company",
        },
        {
            "fact": "has_csr_obligation",
            "op": "eq",
            "value": True,
            "explain": "you meet the CSR thresholds",
        },
    ]
}


def holds(registration: str, explain: str) -> dict[str, Any]:
    return {
        "all": [
            {"fact": "registrations", "op": "includes", "value": registration, "explain": explain}
        ]
    }


def company_and(*clauses: dict[str, Any]) -> dict[str, Any]:
    return {
        "all": [
            {
                "fact": "entity_type",
                "op": "in",
                "value": ["PVT_LTD", "PUBLIC_LTD", "OPC", "SECTION_8"],
                "explain": "you are a company",
            },
            *clauses,
        ]
    }


def trig(days: int) -> dict[str, Any]:
    """Due a fixed number of days after the event that triggered it."""
    return {"anchor": "TRIGGER_DATE", "offset": {"days": days}, "shift_if_holiday": "NONE"}


def after_agm(days: int) -> dict[str, Any]:
    return {
        "anchor": "EVENT_DATE",
        "event_key": "AGM_DATE",
        "offset": {"days": days},
        "shift_if_holiday": "NONE",
    }


def after_fy_end(months: int, day_of_month: int) -> dict[str, Any]:
    return {
        "anchor": "PERIOD_END",
        "offset": {"months": months, "day_of_month": day_of_month},
        "shift_if_holiday": "NONE",
    }


def after_fy_start(months: int, day_of_month: int) -> dict[str, Any]:
    return {
        "anchor": "FY_START",
        "offset": {"months": months, "day_of_month": day_of_month},
        "shift_if_holiday": "NONE",
    }


@dataclass(slots=True)
class Spec:
    """Everything about a definition that the sheet cannot supply."""

    summary: str
    periodicity: str = "ANNUAL"
    due: dict[str, Any] = field(default_factory=dict)
    applicability: dict[str, Any] = field(default_factory=dict)
    trigger: dict[str, Any] = field(default_factory=dict)
    trigger_kind: str = "STATUTORY_PERIODIC"
    tags: list[str] = field(default_factory=list)
    sector_tags: list[str] = field(default_factory=list)
    instance_scope: str = "ENTITY"
    scope_selector: dict[str, Any] = field(default_factory=dict)
    period_anchor: str = "FY"
    jurisdictions: list[str] = field(default_factory=list)
    owner: str = "org-secretarial"
    authority: str = "MCA"
    evidence: list[dict[str, Any]] = field(default_factory=list)
    effective_from: str = "2014-04-01"
    confidence: str = "MEDIUM"
    folder: str = "mca"
    portal: str = ""


def doc(evidence_key: str, label: str, mandatory: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {"key": evidence_key, "label": label, "kind": "DOC"}
    if mandatory:
        entry["mandatory_for_close"] = True
    return entry


EXECUTIVE_APPOINTMENT = {
    "any": [
        {
            "fact": "role",
            "op": "in",
            "value": ["MD", "WTD", "MANAGER"],
            "explain": "the appointment was of a managing or whole-time director, or a manager",
        }
    ]
}


# ---------------------------------------------------------------------------
# The spec table
#
# Grouped the way a company secretary thinks about the work rather than the way
# the sheet happens to be sorted, because that is how the gaps become visible.
# ---------------------------------------------------------------------------

SPECS: dict[str, Spec] = {}


# -- Directors and officers, event-driven -----------------------------------

SPECS["IN-MCA-DIR12-APPOINTMENT"] = Spec(
    summary=(
        "Every appointment to the board is reported to the Registrar within thirty "
        "days. Two directors appointed on the same day are two filings, not one, "
        "which is why this appears once per person."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "DIRECTOR_APPOINTED"},
    due=trig(30),
    applicability=COMPANY,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "director"],
    evidence=[doc("srn", "DIR-12 SRN and challan"), doc("consent", "Form DIR-2 consent", False)],
)

SPECS["IN-MCA-DIR12-RESIGNATION"] = Spec(
    summary=(
        "The company's side of a resignation. The director may also file DIR-11 "
        "themselves, and the two are independent — a director who files DIR-11 does "
        "not discharge the company's obligation here."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "DIRECTOR_RESIGNED"},
    due=trig(30),
    applicability=COMPANY,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "director"],
    evidence=[doc("srn", "DIR-12 SRN and challan"), doc("letter", "Resignation letter", False)],
)

SPECS["IN-MCA-DIR11-RESIGNATION"] = Spec(
    summary=(
        "The resigning director's own filing, made in their name and at their cost. "
        "Optional in the sense that the statute permits rather than compels it — but "
        "a director who does not file it stays on the record as a director."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "DIRECTOR_RESIGNED"},
    due=trig(30),
    applicability=COMPANY,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "director"],
    evidence=[doc("srn", "DIR-11 SRN", False)],
)

SPECS["IN-MCA-DIR12-APPOINT-REAPPOINT"] = Spec(
    summary=(
        "A reappointment at the annual general meeting still reaches the Registrar "
        "through DIR-12. Distinct from a fresh appointment because the underlying "
        "resolution and the board process differ."
    ),
    periodicity="EVENT_BASED",
    trigger={
        "event_key": "DIRECTOR_APPOINTED",
        "when": {
            "any": [
                {
                    "fact": "is_reappointment",
                    "op": "eq",
                    "value": True,
                    "explain": "this was a reappointment rather than a fresh appointment",
                }
            ]
        },
    },
    due=trig(30),
    applicability=COMPANY,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "director"],
    evidence=[doc("srn", "DIR-12 SRN and challan"), doc("resolution", "Resolution", False)],
)

SPECS["IN-MCA-MR1"] = Spec(
    summary=(
        "A managing director, whole-time director or manager is reported separately "
        "from an ordinary director, on a longer clock — sixty days rather than "
        "thirty — and with the terms of appointment attached."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "DIRECTOR_APPOINTED", "when": EXECUTIVE_APPOINTMENT},
    due=trig(60),
    applicability=COMPANY_WITH_MEMBERS,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "kmp", "remuneration"],
    evidence=[doc("srn", "MR-1 SRN"), doc("terms", "Terms of appointment", False)],
)

SPECS["IN-MCA-KMP-APPOINTMENT"] = Spec(
    summary=(
        "Prescribed companies must have a whole-time key managerial person, and a "
        "vacancy has to be filled within six months. The board resolution is the "
        "appointment; DIR-12 is the report of it."
    ),
    periodicity="EVENT_BASED",
    trigger={
        "event_key": "DIRECTOR_APPOINTED",
        "when": {
            "any": [
                {
                    "fact": "role",
                    "op": "in",
                    "value": ["MD", "WTD", "MANAGER", "CEO", "CFO", "COMPANY_SECRETARY"],
                    "explain": "the appointment was of a key managerial person",
                }
            ]
        },
    },
    due=trig(30),
    applicability=LISTED_OR_LARGE,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "kmp"],
    evidence=[doc("srn", "DIR-12 SRN"), doc("resolution", "Board resolution", False)],
)

SPECS["IN-MCA-SMALL-SHAREHOLDERS-DIRECTOR"] = Spec(
    summary=(
        "A listed company with enough small shareholders may be required to seat a "
        "director elected by them. The election process runs on its own notice "
        "periods; the ROC filing follows the appointment."
    ),
    periodicity="EVENT_BASED",
    trigger={
        "event_key": "DIRECTOR_APPOINTED",
        "when": {
            "any": [
                {
                    "fact": "role",
                    "op": "eq",
                    "value": "SMALL_SHAREHOLDER_DIRECTOR",
                    "explain": "the appointment was of a small shareholders' director",
                }
            ]
        },
    },
    due=trig(30),
    applicability={
        "all": [
            {
                "fact": "entity_type",
                "op": "eq",
                "value": "PUBLIC_LTD",
                "explain": "you are a public company",
            },
            {"fact": "is_listed", "op": "eq", "value": True, "explain": "you are listed"},
        ]
    },
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "director", "shareholding"],
    sector_tags=["listed"],
    evidence=[doc("srn", "DIR-12 SRN"), doc("election", "Election record", False)],
)


# -- Director declarations, annual ------------------------------------------

SPECS["IN-MCA-INDEPENDENT-DIRECTOR-DECLARATION"] = Spec(
    summary=(
        "Each independent director confirms in writing that they still meet the "
        "independence tests, at the first board meeting of the year. Nothing is "
        "filed; the declaration lives in the minutes, and its absence is what an "
        "inspection finds."
    ),
    due=after_fy_start(2, 30),
    applicability={
        "all": [
            {
                "fact": "entity_type",
                "op": "eq",
                "value": "PUBLIC_LTD",
                "explain": "you are a public company",
            }
        ]
    },
    trigger_kind="GOVERNANCE",
    tags=["roc", "director", "declaration", "governance"],
    evidence=[doc("declarations", "Signed declarations")],
)

SPECS["IN-MCA-OTHER-DIRECTORSHIPS-DISCLOSURE"] = Spec(
    summary=(
        "Every director confirms annually that they are not disqualified and lists "
        "their other directorships. A director who has quietly become disqualified "
        "invalidates the acts of the board, so this is collected before the first "
        "meeting rather than after it."
    ),
    due=after_fy_start(2, 30),
    applicability=COMPANY,
    trigger_kind="GOVERNANCE",
    tags=["roc", "director", "declaration", "disclosure"],
    evidence=[doc("dir8", "DIR-8 declarations")],
)

SPECS["IN-MCA-RPT-DIRECTOR-DISCLOSURE"] = Spec(
    summary=(
        "Directors disclose their interests at the first board meeting of the year "
        "and whenever they change. What makes this bite is not the form but the "
        "consequence: an undisclosed interest can make the transaction voidable."
    ),
    due=after_fy_start(2, 30),
    applicability=COMPANY,
    trigger_kind="GOVERNANCE",
    tags=["roc", "director", "related-party", "disclosure"],
    evidence=[doc("mbp1", "MBP-1 disclosures")],
)

SPECS["IN-MCA-REGISTER-DIRECTORS-KMP"] = Spec(
    summary=(
        "The register of directors and key managerial personnel is kept current as "
        "particulars change. Nobody is fined for it being untidy on an ordinary "
        "Tuesday, but a due-diligence exercise asks for it complete and "
        "reconstructing four years of entries is a fortnight's work."
    ),
    due=after_fy_start(2, 30),
    applicability=COMPANY,
    trigger_kind="GOVERNANCE",
    tags=["roc", "statutory-register", "director", "kmp"],
    evidence=[doc("register", "Updated register")],
)

# -- Auditors ----------------------------------------------------------------

SPECS["IN-MCA-AUDITOR-CONSENT-ELIGIBILITY"] = Spec(
    summary=(
        "Written consent and an eligibility certificate are obtained *before* the "
        "auditor is appointed, not after. An appointment made without them is open "
        "to challenge and the remedy is to start again."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "AUDITOR_APPOINTED"},
    due=trig(0),
    applicability=COMPANY,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "auditor", "certificate"],
    evidence=[doc("consent", "Auditor consent and eligibility certificate")],
)

SPECS["IN-MCA-ADT1-REAPPOINTMENT"] = Spec(
    summary=(
        "Reappointing the existing auditor at the annual general meeting still "
        "requires ADT-1 within fifteen days. The commonest omission in the whole "
        "ROC calendar, precisely because nothing appears to have changed."
    ),
    due=after_agm(15),
    applicability=COMPANY,
    tags=["roc", "auditor", "annual-filing"],
    evidence=[doc("srn", "ADT-1 SRN"), doc("resolution", "AGM resolution", False)],
)

SPECS["IN-MCA-AUDITOR-CASUAL-VACANCY"] = Spec(
    summary=(
        "A casual vacancy is filled by the board within thirty days. Where it arose "
        "from a resignation the members must also approve, within three months — two "
        "clocks from one event, and the second is the one that gets missed."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "AUDITOR_RESIGNED"},
    due=trig(30),
    applicability=COMPANY,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "auditor"],
    evidence=[doc("resolution", "Board resolution"), doc("srn", "ADT-1 SRN", False)],
)

SPECS["IN-MCA-ADT3"] = Spec(
    summary=(
        "The resigning auditor's own filing, made in their name within thirty days, "
        "stating why they resigned. The company cannot make it for them, and a "
        "missing ADT-3 is a question the next auditor will ask."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "AUDITOR_RESIGNED"},
    due=trig(30),
    applicability=COMPANY,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "auditor"],
    evidence=[doc("srn", "ADT-3 SRN", False)],
)

SPECS["IN-MCA-AUDITOR-INDEPENDENCE"] = Spec(
    summary=(
        "Independence is not a condition checked once at appointment — a prohibited "
        "relationship acquired mid-year makes the auditor ineligible from that "
        "moment. Reviewed annually and monitored through the engagement."
    ),
    due=after_fy_start(2, 30),
    applicability=COMPANY,
    trigger_kind="GOVERNANCE",
    tags=["roc", "auditor", "governance"],
    evidence=[doc("checklist", "Independence checklist")],
)

SPECS["IN-MCA-AUDITORS-REPORT"] = Spec(
    summary=(
        "The audit report is signed with the financial statements and circulated "
        "before the general meeting. Everything downstream — AOC-4, the members' "
        "approval, the tax return — waits on it."
    ),
    due=after_fy_end(6, 30),
    applicability=COMPANY,
    tags=["roc", "audit-report", "financial-statements"],
    owner="org-finance",
    evidence=[doc("report", "Signed independent auditor's report")],
)

SPECS["IN-MCA-SECRETARIAL-AUDIT-MR3"] = Spec(
    summary=(
        "Prescribed companies annex a secretarial audit report to the Board's "
        "Report. It is an audit of compliance itself, carried out by a practising "
        "company secretary, and its qualifications are read closely by lenders."
    ),
    due=after_fy_end(6, 30),
    applicability=LISTED_OR_LARGE,
    tags=["roc", "secretarial-audit", "audit-report"],
    evidence=[doc("mr3", "MR-3 secretarial audit report")],
)

SPECS["IN-MCA-SECRETARIAL-AUDITOR-APPOINTMENT"] = Spec(
    summary=(
        "Appointed early enough that the audit can actually be completed before the "
        "Board's Report is signed. Appointing in September for a report due in "
        "October is how a qualification ends up in the report."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "SECRETARIAL_AUDITOR_APPOINTED"},
    due=trig(0),
    applicability=LISTED_OR_LARGE,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "secretarial-audit"],
    evidence=[doc("engagement", "Board resolution or engagement letter")],
)

SPECS["IN-MCA-MGT8"] = Spec(
    summary=(
        "Larger companies must have the annual return certified by a practising "
        "company secretary before it is filed. Obtained ahead of MGT-7, because "
        "MGT-7 cannot be filed without it."
    ),
    due=after_agm(45),
    applicability=LISTED_OR_LARGE,
    tags=["roc", "annual-return", "certificate"],
    evidence=[doc("mgt8", "MGT-8 certificate")],
)


# -- Meetings and minutes ----------------------------------------------------

SPECS["IN-MCA-AGM-NOTICE"] = Spec(
    summary=(
        "Twenty-one clear days, and 'clear' excludes both the day of despatch and "
        "the day of the meeting. A notice one day short invalidates the proceedings "
        "unless every member consents to the shorter period."
    ),
    due=after_agm(-21),
    applicability=COMPANY_WITH_MEMBERS,
    trigger_kind="GOVERNANCE",
    tags=["roc", "general-meeting", "governance"],
    evidence=[doc("notice", "Notice with proof of despatch")],
)

SPECS["IN-MCA-AGM-MINUTES"] = Spec(
    summary=(
        "Minutes are entered in the minutes book within thirty days of the meeting "
        "and signed. After that the entry is not supposed to be altered, which is "
        "why the thirty days matter more than they look."
    ),
    due=after_agm(30),
    applicability=COMPANY_WITH_MEMBERS,
    trigger_kind="GOVERNANCE",
    tags=["roc", "general-meeting", "minutes", "governance"],
    evidence=[doc("minutes", "Signed minutes")],
)

SPECS["IN-MCA-BOARD-MEETING-ATTENDANCE"] = Spec(
    summary=(
        "The attendance register for each board meeting. No filing and no deadline "
        "of its own — it exists because quorum and participation are questioned "
        "years later, and a register nobody kept cannot answer."
    ),
    periodicity="QUARTERLY",
    due={
        "anchor": "PERIOD_END",
        "offset": {"months": 0, "day_of_month": -1},
        "shift_if_holiday": "PREVIOUS_WORKING_DAY",
        "calendars": ["IN-NATIONAL"],
    },
    applicability=COMPANY_WITH_MEMBERS,
    trigger_kind="GOVERNANCE",
    tags=["roc", "board-meeting", "governance"],
    evidence=[doc("attendance", "Attendance register")],
)

SPECS["IN-MCA-BOARD-MEETING-PARTICIPATION"] = Spec(
    summary=(
        "Participation by video conference is permitted for most business but not "
        "all, and the minutes have to record which directors attended how. The "
        "restriction is on the *subject matter*, not on the director."
    ),
    periodicity="QUARTERLY",
    due={
        "anchor": "PERIOD_END",
        "offset": {"months": 0, "day_of_month": -1},
        "shift_if_holiday": "PREVIOUS_WORKING_DAY",
        "calendars": ["IN-NATIONAL"],
    },
    applicability=COMPANY_WITH_MEMBERS,
    trigger_kind="GOVERNANCE",
    tags=["roc", "board-meeting", "governance"],
    evidence=[doc("record", "Participation record")],
)

SPECS["IN-MCA-COMMITTEE-MEETINGS"] = Spec(
    summary=(
        "Audit, nomination and stakeholder committees each have their own frequency, "
        "and for a listed company the listing regulations impose tighter ones than "
        "the Companies Act. Scheduled per quarter and reconciled against what the "
        "company's own constitution requires."
    ),
    periodicity="QUARTERLY",
    due={
        "anchor": "PERIOD_END",
        "offset": {"months": 0, "day_of_month": -1},
        "shift_if_holiday": "PREVIOUS_WORKING_DAY",
        "calendars": ["IN-NATIONAL"],
    },
    applicability=LISTED_OR_LARGE,
    trigger_kind="GOVERNANCE",
    tags=["roc", "board-meeting", "minutes", "governance"],
    evidence=[doc("minutes", "Committee minutes")],
)


# -- Annual accounts and the annual return -----------------------------------

SPECS["IN-MCA-BOARDS-REPORT"] = Spec(
    summary=(
        "The Board's Report carries a long list of prescribed disclosures and "
        "several annexures, and it is approved by the board before the accounts are "
        "circulated. Assembling it is the work; approving it is the deadline."
    ),
    due=after_fy_end(6, 30),
    applicability=COMPANY,
    tags=["roc", "annual-filing", "disclosure"],
    evidence=[doc("report", "Approved Board's Report with annexures")],
)

SPECS["IN-MCA-BOARD-REPORT-DISCLOSURES"] = Spec(
    summary=(
        "The checklist behind the Board's Report: conservation of energy, related "
        "party contracts, risk management, the annual return web-link and a dozen "
        "more. Each omission is a separate default."
    ),
    due=after_fy_end(6, 30),
    applicability=COMPANY,
    tags=["roc", "disclosure", "annual-filing"],
    evidence=[doc("checklist", "Disclosure checklist")],
)

SPECS["IN-MCA-FINANCIAL-STATEMENTS-PREPARATION"] = Spec(
    summary=(
        "Balance sheet, profit and loss, cash flow where required, and the notes. "
        "Prepared for board approval, which has to happen before circulation and "
        "well before the meeting."
    ),
    due=after_fy_end(5, 31),
    applicability=COMPANY,
    owner="org-finance",
    tags=["roc", "financial-statements"],
    evidence=[doc("financials", "Draft financial statements")],
)

SPECS["IN-MCA-FINANCIAL-STATEMENT-APPROVAL"] = Spec(
    summary=(
        "Board approval and the prescribed signatures. Unsigned accounts cannot be "
        "circulated, cannot be laid before the members and cannot be filed."
    ),
    due=after_fy_end(6, 15),
    applicability=COMPANY,
    owner="org-finance",
    tags=["roc", "financial-statements", "governance"],
    evidence=[doc("resolution", "Board resolution approving the accounts")],
)

SPECS["IN-MCA-AOC4-NBFC"] = Spec(
    summary=(
        "An NBFC files its financial statements on the NBFC variant of AOC-4, not "
        "the ordinary one. Same thirty days after the meeting; a different form, and "
        "filing the wrong one is a rejection rather than a late filing."
    ),
    due=after_agm(30),
    applicability=company_and(
        {
            "fact": "sector",
            "op": "matches",
            "value": "(?i)nbfc|non.banking",
            "explain": "you are a non-banking financial company",
        }
    ),
    tags=["roc", "financial-statements", "annual-filing"],
    sector_tags=["nbfc"],
    owner="org-finance",
    evidence=[doc("srn", "AOC-4 NBFC SRN"), doc("financials", "Audited financials", False)],
)

SPECS["IN-MCA-ANNUAL-RETURN-PREPARATION"] = Spec(
    summary=(
        "The working paper behind MGT-7: shareholding as at the year end, changes "
        "during the year, meetings held, penalties suffered. Reconciling it is what "
        "takes the time, and it has to be done before the sixty days run out."
    ),
    due=after_agm(45),
    applicability=COMPANY,
    tags=["roc", "annual-return"],
    evidence=[doc("working", "Annual return working paper")],
)

SPECS["IN-MCA-ANNUAL-RETURN-WEBSITE"] = Spec(
    summary=(
        "If the company has a website, the annual return goes on it and the "
        "web-link goes in the Board's Report. Two obligations that both fail "
        "quietly, because nobody looks at either until an inspection does."
    ),
    due=after_agm(60),
    applicability=COMPANY,
    trigger_kind="GOVERNANCE",
    tags=["roc", "annual-return", "disclosure"],
    evidence=[doc("link", "Web-link and screenshot", False)],
)

SPECS["IN-MCA-SHAREHOLDING-DISCLOSURE"] = Spec(
    summary=(
        "Shareholding particulars as they will appear in the annual return, "
        "reconciled against the register of members before filing rather than after "
        "the Registrar queries them."
    ),
    due=after_agm(45),
    applicability=COMPANY,
    tags=["roc", "shareholding", "annual-return"],
    evidence=[doc("statement", "Shareholding statement")],
)

SPECS["IN-MCA-PROMOTER-SHAREHOLDING"] = Spec(
    summary=(
        "Promoter holdings, reconciled for the annual return and the financial "
        "statements. Divergence between the two is a common audit query and an "
        "avoidable one."
    ),
    due=after_agm(45),
    applicability=COMPANY,
    tags=["roc", "shareholding", "disclosure"],
    evidence=[doc("statement", "Promoter holding statement")],
)

SPECS["IN-MCA-MEMBER-LIST"] = Spec(
    summary=(
        "The register of members is maintained continuously, not assembled once a "
        "year. The annual reconciliation is the point at which gaps become visible "
        "while they can still be traced."
    ),
    due=after_agm(45),
    applicability=COMPANY,
    trigger_kind="GOVERNANCE",
    tags=["roc", "statutory-register", "shareholding"],
    evidence=[doc("register", "Register of members")],
)

SPECS["IN-MCA-DIRECTOR-KMP-ANNUAL-DISCLOSURE"] = Spec(
    summary=(
        "Director and KMP particulars as they will appear in the annual return. "
        "Where they disagree with the Registrar's record, the cause is almost always "
        "a DIR-12 that was never filed."
    ),
    due=after_agm(45),
    applicability=COMPANY,
    tags=["roc", "director", "kmp", "annual-return"],
    evidence=[doc("statement", "Director and KMP statement")],
)

SPECS["IN-MCA-ANNUAL-FILING-RECONCILIATION"] = Spec(
    summary=(
        "An internal check that AOC-4 and MGT-7 tell the same story as each other "
        "and as the accounts. Not a filing — a control, run before the filings so "
        "that a contradiction is caught by us rather than by the Registrar."
    ),
    due=after_agm(50),
    applicability=COMPANY,
    trigger_kind="INTERNAL",
    tags=["roc", "reconciliation", "internal-control"],
    evidence=[doc("recon", "Filing reconciliation")],
)

SPECS["IN-MCA-ANNUAL-COMPLIANCE-CERTIFICATE"] = Spec(
    summary=(
        "The engagement deliverable that says what was done and what was found. No "
        "statutory deadline of its own; it exists because a client, a lender or a "
        "buyer asks for one and the answer has to be written down somewhere."
    ),
    due=after_agm(75),
    applicability=COMPANY,
    trigger_kind="INTERNAL",
    tags=["roc", "certificate", "internal-control"],
    evidence=[doc("certificate", "Annual compliance certificate")],
)

SPECS["IN-MCA-ANNUAL-COMPLIANCE-SIGNOFF"] = Spec(
    summary=(
        "The final sign-off after the annual filings are complete. Closes the year "
        "so that next year's review starts from a known position rather than from "
        "an archaeology exercise."
    ),
    due=after_agm(90),
    applicability=COMPANY,
    trigger_kind="INTERNAL",
    tags=["roc", "internal-control", "governance"],
    evidence=[doc("signoff", "Sign-off checklist")],
)

SPECS["IN-MCA-STATUTORY-RECORDS-REVIEW"] = Spec(
    summary=(
        "A completeness sweep of the statutory records before the annual filings and "
        "the general meeting. Cheap to run now; a fortnight of reconstruction if it "
        "is left until due diligence."
    ),
    due=after_fy_end(5, 31),
    applicability=COMPANY,
    trigger_kind="INTERNAL",
    tags=["roc", "statutory-register", "internal-control"],
    evidence=[doc("checklist", "Records checklist")],
)


# -- KYC, remuneration, rotation ---------------------------------------------

SPECS["IN-MCA-DIR3-KYC-WEB"] = Spec(
    summary=(
        "A director whose KYC is already on record and unchanged uses the web "
        "service rather than the full form. Same 30 September deadline, and the same "
        "consequence for missing it: the DIN is deactivated and every filing that "
        "needs it stops."
    ),
    due=after_fy_start(5, 30),
    applicability=COMPANY,
    tags=["roc", "director", "kyc"],
    evidence=[doc("ack", "KYC acknowledgement")],
    effective_from="2019-07-25",
)

SPECS["IN-MCA-MBP1-DISCLOSURE"] = Spec(
    summary=(
        "Form MBP-1 is how a director tells the board what they are interested in. "
        "Collected at the first board meeting of the year and whenever it changes; "
        "kept in the records rather than filed."
    ),
    due=after_fy_start(2, 30),
    applicability=COMPANY,
    trigger_kind="GOVERNANCE",
    tags=["roc", "director", "disclosure", "related-party"],
    evidence=[doc("mbp1", "MBP-1 forms")],
)

SPECS["IN-MCA-DIRECTOR-ANNUAL-DECLARATIONS"] = Spec(
    summary=(
        "The annual bundle: interest under Section 184 and non-disqualification "
        "under Section 164, from every director, before the first board meeting of "
        "the year."
    ),
    due=after_fy_start(2, 30),
    applicability=COMPANY,
    trigger_kind="GOVERNANCE",
    tags=["roc", "director", "declaration"],
    evidence=[doc("declarations", "Signed declarations")],
)

SPECS["IN-MCA-DIRECTOR-RETIREMENT-ROTATION"] = Spec(
    summary=(
        "A third of the rotational directors retire at each annual general meeting "
        "and usually offer themselves for reappointment. Overlooked entirely in "
        "companies where the same people have been on the board for years — and the "
        "board is then improperly constituted."
    ),
    due=after_agm(0),
    applicability={
        "all": [
            {
                "fact": "entity_type",
                "op": "eq",
                "value": "PUBLIC_LTD",
                "explain": "you are a public company, whose directors retire by rotation",
            }
        ]
    },
    trigger_kind="GOVERNANCE",
    tags=["roc", "director", "general-meeting"],
    evidence=[doc("resolution", "AGM resolution")],
)

SPECS["IN-MCA-MANAGERIAL-REMUNERATION"] = Spec(
    summary=(
        "Managerial remuneration above the Section 197 limits needs approval before "
        "it is paid, and in a loss year Schedule V applies instead. Excess paid "
        "without approval is recoverable from the recipient."
    ),
    due=after_fy_start(2, 30),
    applicability=COMPANY_WITH_MEMBERS,
    trigger_kind="GOVERNANCE",
    tags=["roc", "remuneration", "kmp"],
    evidence=[doc("approval", "Board or shareholder approval")],
)

SPECS["IN-MCA-MANAGERIAL-REMUNERATION-DISCLOSURE"] = Spec(
    summary=(
        "The prescribed remuneration disclosures in the Board's Report — ratios to "
        "median employee remuneration, percentage increases, and the named "
        "particulars for the highest-paid."
    ),
    due=after_fy_end(6, 30),
    applicability=LISTED_OR_LARGE,
    tags=["roc", "remuneration", "disclosure"],
    evidence=[doc("disclosure", "Remuneration disclosure")],
)

SPECS["IN-MCA-DIRECTOR-REMUNERATION-DISCLOSURE"] = Spec(
    summary=(
        "Director remuneration as disclosed in the financial statements and the "
        "Board's Report. The two have to agree with each other and with what was "
        "actually approved."
    ),
    due=after_fy_end(6, 30),
    applicability=COMPANY,
    owner="org-finance",
    tags=["roc", "remuneration", "disclosure", "financial-statements"],
    evidence=[doc("disclosure", "Remuneration note")],
)

SPECS["IN-MCA-DIRECTOR-REMUNERATION-ANNUAL"] = Spec(
    summary=(
        "The annual remuneration statement for directors and key managerial "
        "personnel, reconciled to payroll and to the approvals on record."
    ),
    due=after_fy_end(6, 30),
    applicability=COMPANY,
    owner="org-finance",
    tags=["roc", "remuneration", "kmp", "reconciliation"],
    evidence=[doc("statement", "Remuneration statement")],
)


# -- Transactions that need approval before they happen ----------------------

SPECS["IN-MCA-LOANS-TO-DIRECTORS"] = Spec(
    summary=(
        "Section 185 restricts lending to directors and to entities they are "
        "interested in. The conditions have to be satisfied *before* the money "
        "moves — this is not a filing that can be caught up afterwards."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "LOAN_TO_DIRECTOR"},
    due=trig(0),
    applicability=COMPANY,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "director", "related-party"],
    evidence=[doc("approval", "Board or shareholder approval")],
)

SPECS["IN-MCA-SECTION186"] = Spec(
    summary=(
        "Loans, guarantees, securities and investments beyond the prescribed limits "
        "need a special resolution, and every one of them goes in the register "
        "whether or not it crossed a limit."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "SECTION186_TRANSACTION"},
    due=trig(7),
    applicability=COMPANY,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "statutory-register", "related-party"],
    evidence=[doc("register", "Register entry"), doc("approval", "Approvals", False)],
)

SPECS["IN-MCA-AOC2-RPT"] = Spec(
    summary=(
        "Related-party contracts that were not at arm's length, or not in the "
        "ordinary course, are set out in AOC-2 and annexed to the Board's Report. An "
        "empty AOC-2 is a positive statement, not an omission."
    ),
    due=after_fy_end(6, 30),
    applicability=company_and(
        {
            "fact": "has_related_party_txns",
            "op": "eq",
            "value": True,
            "explain": "you have related-party transactions",
        }
    ),
    tags=["roc", "related-party", "disclosure"],
    evidence=[doc("aoc2", "AOC-2")],
)


# -- Significant beneficial ownership ----------------------------------------

SPECS["IN-MCA-BEN2"] = Spec(
    summary=(
        "Once a BEN-1 declaration arrives, the company has thirty days to file "
        "BEN-2. The clock runs from receipt of the declaration, not from the year "
        "end, which is why this appears when the declaration is recorded."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "BEN1_RECEIVED"},
    due=trig(30),
    applicability=COMPANY,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "beneficial-ownership", "shareholding"],
    evidence=[doc("srn", "BEN-2 SRN"), doc("ben1", "BEN-1 declaration", False)],
    effective_from="2019-02-08",
)


# -- Dividend and the IEPF ---------------------------------------------------

SPECS["IN-MCA-DIVIDEND-DECLARATION"] = Spec(
    summary=(
        "A declared dividend must reach shareholders within thirty days. Interest "
        "runs from day thirty-one and the offence is on the officers personally, "
        "which is what makes this one of the sharper deadlines in the Act."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "DIVIDEND_DECLARED"},
    due=trig(30),
    applicability=COMPANY_WITH_MEMBERS,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "dividend", "payment"],
    owner="org-finance",
    evidence=[doc("records", "Payment records")],
)

SPECS["IN-MCA-UNPAID-DIVIDEND"] = Spec(
    summary=(
        "Whatever is still unclaimed seven days after the thirty are up moves to a "
        "separate Unpaid Dividend Account. Thirty-seven days from declaration, and "
        "the seven-day tail is the part that gets missed."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "DIVIDEND_DECLARED"},
    due=trig(37),
    applicability=COMPANY_WITH_MEMBERS,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "dividend", "iepf"],
    owner="org-finance",
    evidence=[doc("transfer", "Transfer confirmation")],
)

SPECS["IN-MCA-IEPF4"] = Spec(
    summary=(
        "Shares whose dividend has been unclaimed for seven consecutive years are "
        "transferred to the Investor Education and Protection Fund, and IEPF-4 "
        "reports the transfer within thirty days of the corporate action."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "IEPF_TRANSFER"},
    due=trig(30),
    applicability=COMPANY_WITH_MEMBERS,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "iepf", "shareholding"],
    evidence=[doc("srn", "IEPF-4 SRN")],
    effective_from="2016-09-07",
)

SPECS["IN-MCA-IEPF5-PROCESS"] = Spec(
    summary=(
        "When a claimant applies to the IEPF for their shares back, the company has "
        "thirty days to verify and report. A daily fee runs after that, capped — "
        "small money, but the claimant cannot be paid until it is done."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "IEPF5_CLAIM_RECEIVED"},
    due=trig(30),
    applicability=COMPANY_WITH_MEMBERS,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "iepf"],
    evidence=[doc("report", "e-verification report")],
    effective_from="2016-09-07",
)


# -- CSR ---------------------------------------------------------------------

SPECS["IN-MCA-CSR-ANNUAL-COMPLIANCE"] = Spec(
    summary=(
        "The spending itself, and what happens to anything unspent. An ongoing "
        "project keeps its unspent amount in a separate account; anything else goes "
        "to a Schedule VII fund. Getting that distinction wrong is the expensive "
        "part, not the reporting."
    ),
    due=after_fy_end(6, 30),
    applicability=CSR_LIABLE,
    tags=["roc", "csr"],
    evidence=[doc("statement", "CSR spending statement")],
    effective_from="2014-04-01",
)

SPECS["IN-MCA-CSR-REPORT"] = Spec(
    summary="The annual report on CSR activities, annexed to the Board's Report.",
    due=after_fy_end(6, 30),
    applicability=CSR_LIABLE,
    tags=["roc", "csr", "disclosure"],
    evidence=[doc("report", "Annual CSR report")],
)

SPECS["IN-MCA-CSR-ANNUAL-REPORT"] = Spec(
    summary=(
        "The prescribed format for the CSR annexure: projects, amounts, "
        "implementing agencies and the impact assessment where one is required."
    ),
    due=after_fy_end(6, 30),
    applicability=CSR_LIABLE,
    tags=["roc", "csr", "disclosure"],
    evidence=[doc("annexure", "CSR annexure")],
)

SPECS["IN-MCA-CSR-BOARD-REPORT"] = Spec(
    summary=(
        "The CSR paragraphs inside the Board's Report itself, including the reason "
        "for any shortfall. 'We did not get round to it' is not one of the "
        "permitted reasons."
    ),
    due=after_fy_end(6, 30),
    applicability=CSR_LIABLE,
    tags=["roc", "csr", "disclosure"],
    evidence=[doc("disclosure", "CSR disclosure")],
)

SPECS["IN-MCA-CSR-UTILISATION-CERTIFICATE"] = Spec(
    summary=(
        "The chief financial officer certifies that the money was actually spent on "
        "what it was released for. Internal to the board's monitoring; nothing is "
        "filed, and its absence surfaces in the secretarial audit."
    ),
    due=after_fy_end(6, 30),
    applicability=CSR_LIABLE,
    trigger_kind="INTERNAL",
    tags=["roc", "csr", "certificate", "internal-control"],
    owner="org-finance",
    evidence=[doc("certificate", "Utilisation certificate")],
)

SPECS["IN-MCA-CSR1"] = Spec(
    summary=(
        "An implementing agency has to be registered on CSR-1 *before* it takes CSR "
        "money. A company that funds an unregistered agency does not get to count "
        "the spend, which makes this the funder's problem as much as the agency's."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "CSR_AGENCY_ENGAGED"},
    due=trig(0),
    applicability=CSR_LIABLE,
    trigger_kind="EVENT_DRIVEN",
    tags=["roc", "csr", "registration"],
    evidence=[doc("csr1", "CSR-1 registration number")],
    effective_from="2021-04-01",
)


# -- Provident fund and ESIC -------------------------------------------------

HAS_PF = holds("PF", "you hold an EPFO establishment code")
HAS_ESIC = holds("ESIC", "you hold an ESIC employer code")


def _epf(code: str, summary: str, **kw: Any) -> None:
    kw.setdefault("effective_from", "1952-11-15")
    SPECS[code] = Spec(
        summary=summary,
        applicability=HAS_PF,
        folder="pf-esic",
        authority="EPFO",
        owner="org-hr",
        portal="https://unifiedportal-emp.epfindia.gov.in",
        **kw,
    )


_epf(
    "IN-EPF-CONTRIBUTION-PAYMENT",
    "The money, as distinct from the return. Both are due on the fifteenth, and a "
    "return filed without the remittance still leaves interest running under "
    "Section 7Q and damages under Section 14B.",
    periodicity="MONTHLY",
    due={
        "anchor": "PERIOD_END",
        "offset": {"months": 1, "day_of_month": 15},
        "shift_if_holiday": "NONE",
    },
    tags=["epf", "payroll", "payment", "monthly-filing"],
    evidence=[doc("challan", "Payment challan")],
)

_epf(
    "IN-EPF-ECR-RECON",
    "Tie the ECR to the challan for the month. A short remittance found now costs "
    "interest for one month; found at an inspection it costs interest and damages "
    "for however many years it went unnoticed.",
    periodicity="MONTHLY",
    due={
        "anchor": "PERIOD_END",
        "offset": {"months": 1, "day_of_month": 20},
        "shift_if_holiday": "NONE",
    },
    trigger_kind="INTERNAL",
    tags=["epf", "reconciliation", "internal-control"],
    evidence=[doc("recon", "ECR and challan reconciliation")],
)

_epf(
    "IN-EPF-FORM5A",
    "Ownership and management particulars of the establishment, updated whenever "
    "they change. An out-of-date Form 5A slows down every other EPFO service the "
    "establishment needs.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "EPF_ESTABLISHMENT_CHANGED"},
    due=trig(15),
    trigger_kind="EVENT_DRIVEN",
    tags=["epf", "amendment"],
    evidence=[doc("ack", "Form 5A acknowledgement")],
)

_epf(
    "IN-EPF-EMPLOYER-PROFILE-CORRECTION",
    "Corrections to establishment particulars on the employer portal. Left wrong, "
    "they block transfers and claims for every member of the establishment.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "EPF_ESTABLISHMENT_CHANGED"},
    due=trig(30),
    trigger_kind="EVENT_DRIVEN",
    tags=["epf", "correction"],
    evidence=[doc("ack", "Correction acknowledgement", False)],
)

_epf(
    "IN-EPF-EMPLOYEE-ENROLMENT",
    "A new joiner is enrolled before they appear in their first ECR. Enrolling late "
    "means contribution arrears with interest and damages, and a member whose "
    "service history has a hole in it.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "EMPLOYEE_JOINED"},
    due=trig(15),
    trigger_kind="EVENT_DRIVEN",
    tags=["epf", "employee-onboarding", "payroll"],
    evidence=[doc("registration", "Member registration", False)],
)

_epf(
    "IN-EPF-UAN-GENERATION",
    "A member with no existing Universal Account Number needs one before the first "
    "ECR. A second UAN issued to someone who already had one is far harder to undo "
    "than to avoid.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "EMPLOYEE_JOINED"},
    due=trig(7),
    trigger_kind="EVENT_DRIVEN",
    tags=["epf", "employee-onboarding"],
    evidence=[doc("uan", "UAN", False)],
)

_epf(
    "IN-EPF-FORM11",
    "The joiner declares whether they were ever a member before, and with which "
    "UAN. This is the form that prevents the duplicate UAN the previous obligation "
    "warns about, which is why it is collected on day one.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "EMPLOYEE_JOINED"},
    due=trig(7),
    trigger_kind="EVENT_DRIVEN",
    tags=["epf", "employee-onboarding", "declaration"],
    evidence=[doc("form11", "Signed Form 11", False)],
)

_epf(
    "IN-EPF-EMPLOYEE-KYC",
    "Aadhaar, PAN and bank details verified against the UAN. Incomplete KYC does "
    "not stop contributions going in; it stops the member ever getting them out "
    "online.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "EMPLOYEE_JOINED"},
    due=trig(30),
    trigger_kind="EVENT_DRIVEN",
    tags=["epf", "kyc", "employee-onboarding"],
    evidence=[doc("kyc", "KYC confirmation", False)],
)

_epf(
    "IN-EPF-EMPLOYEE-EXIT",
    "The date of exit is marked on the portal after someone leaves. Until it is, "
    "the member cannot transfer or withdraw, and the ex-employer is who they will "
    "ring about it.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "EMPLOYEE_EXITED"},
    due=trig(30),
    trigger_kind="EVENT_DRIVEN",
    tags=["epf", "employee-exit"],
    evidence=[doc("exit", "Date of exit confirmation", False)],
)

_epf(
    "IN-EPF-UAN-PROFILE-CORRECTION",
    "A wrong name, date of birth or father's name on the UAN blocks KYC and every "
    "claim behind it. Corrected through a joint declaration, which needs the "
    "employer as well as the member.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "EMPLOYEE_JOINED"},
    due=trig(60),
    trigger_kind="EVENT_DRIVEN",
    tags=["epf", "correction", "kyc"],
    evidence=[doc("declaration", "Joint declaration", False)],
)

SPECS["IN-ESIC-CONTRIBUTION-PAYMENT"] = Spec(
    summary=(
        "The remittance behind the monthly ESIC contribution. Interest runs on "
        "arrears and damages are assessed on how long the default lasted, so a "
        "payment made late by a week and one late by a year are not the same "
        "problem."
    ),
    periodicity="MONTHLY",
    due={
        "anchor": "PERIOD_END",
        "offset": {"months": 1, "day_of_month": 15},
        "shift_if_holiday": "NONE",
    },
    applicability=HAS_ESIC,
    folder="pf-esic",
    authority="ESIC",
    owner="org-hr",
    tags=["esic", "payroll", "payment", "monthly-filing"],
    evidence=[doc("challan", "Payment challan")],
    effective_from="1952-09-01",
    portal="https://www.esic.gov.in",
)


# -- Professional tax, for states with no specific rule ----------------------
#
# Deliberately limited to states the catalog does not already cover by name. A
# generic rule firing alongside IN-PT-MH-RC-MONTHLY would put the same tax on the
# calendar twice, and the client would pay it twice or ignore both.

PT_UNCOVERED = [
    "IN-AS",
    "IN-BR",
    "IN-CT",
    "IN-GA",
    "IN-JH",
    "IN-MN",
    "IN-ML",
    "IN-MZ",
    "IN-NL",
    "IN-OR",
    "IN-PY",
    "IN-PB",
    "IN-SK",
    "IN-TR",
]


def _pt(code: str, summary: str, periodicity: str, day: int, registration: str, **kw: Any) -> None:
    SPECS[code] = Spec(
        summary=summary,
        periodicity=periodicity,
        due={
            "anchor": "PERIOD_END",
            "offset": {"months": 1, "day_of_month": day},
            "shift_if_holiday": "NONE",
        },
        applicability=holds(
            registration, f"you hold a professional tax {registration} certificate"
        ),
        instance_scope="REGISTRATION",
        scope_selector={"registration_type": registration},
        jurisdictions=PT_UNCOVERED,
        folder="professional-tax",
        authority="STATE_TAX",
        owner="org-hr",
        effective_from="1976-04-01",
        confidence="LOW",
        **kw,
    )


_pt(
    "IN-PT-GENERIC-MONTHLY-PAYMENT",
    "Professional tax deducted from salaries, paid monthly. The due date is set by "
    "each state and this is the general shape rather than any one state's rule — "
    "confirm the day against the local Act before relying on it.",
    "MONTHLY",
    15,
    "PT_RC",
    tags=["professional-tax", "payroll", "payment", "monthly-filing"],
    evidence=[doc("challan", "Payment challan")],
)

_pt(
    "IN-PT-GENERIC-MONTHLY-RETURN",
    "The monthly employer return that accompanies the payment, in states that "
    "require the two separately.",
    "MONTHLY",
    20,
    "PT_RC",
    tags=["professional-tax", "payroll", "return", "monthly-filing"],
    evidence=[doc("return", "Filed return")],
)

_pt(
    "IN-PT-GENERIC-QUARTERLY-PAYMENT",
    "Smaller employers pay quarterly rather than monthly in several states. Which "
    "of the two applies depends on the annual liability, so this and the monthly "
    "rule are alternatives rather than both.",
    "QUARTERLY",
    15,
    "PT_RC",
    period_anchor="CALENDAR",
    tags=["professional-tax", "payroll", "payment", "quarterly-filing"],
    evidence=[doc("challan", "Payment challan")],
)

_pt(
    "IN-PT-GENERIC-QUARTERLY-RETURN",
    "The quarterly employer return, where the state asks for one separately from the payment.",
    "QUARTERLY",
    20,
    "PT_RC",
    period_anchor="CALENDAR",
    tags=["professional-tax", "payroll", "return", "quarterly-filing"],
    evidence=[doc("return", "Filed return")],
)

_pt(
    "IN-PT-GENERIC-ANNUAL-RETURN",
    "The annual employer return reconciling the year's deductions. Several states "
    "ask for this on top of the periodic returns rather than instead of them.",
    "ANNUAL",
    30,
    "PT_RC",
    tags=["professional-tax", "payroll", "return", "annual-filing"],
    evidence=[doc("return", "Filed annual return")],
)


# -- Income tax --------------------------------------------------------------

HAS_PAN = holds("PAN", "you hold a PAN")
NON_RESIDENT_DEALINGS = {
    "all": [
        {
            "any": [
                {
                    "fact": "has_foreign_shareholding",
                    "op": "eq",
                    "value": True,
                    "explain": "you have foreign shareholding",
                },
                {
                    "fact": "has_export_import",
                    "op": "eq",
                    "value": True,
                    "explain": "you deal across borders",
                },
            ]
        }
    ]
}
INTERNATIONAL_GROUP = {
    "all": [
        {
            "fact": "has_foreign_shareholding",
            "op": "eq",
            "value": True,
            "explain": "you are part of an international group",
        },
        {
            "fact": "aggregate_turnover",
            "op": "gte",
            "value": 5000000000,
            "explain": "your turnover is at or above the master-file threshold",
        },
    ]
}
CHARITABLE = {
    "all": [
        {
            "fact": "entity_type",
            "op": "in",
            "value": ["TRUST", "SOCIETY", "SECTION_8"],
            "explain": "you are a trust, society or Section 8 company",
        }
    ]
}


def _it(code: str, summary: str, **kw: Any) -> None:
    kw.setdefault("applicability", HAS_PAN)
    kw.setdefault("tags", ["income-tax"])
    kw.setdefault("effective_from", "1962-04-01")
    kw.setdefault("portal", "https://www.incometax.gov.in")
    SPECS[code] = Spec(
        summary=summary, folder="income-tax", authority="CBDT", owner="org-finance", **kw
    )


_it(
    "IN-IT-ITR5",
    "The return for a firm or LLP. Which due date applies turns on whether a tax "
    "audit is required, so the date is not a fixed offset from the year end — it is "
    "a consequence of the audit question.",
    due=after_fy_end(7, 31),
    applicability={
        "all": [
            {
                "fact": "entity_type",
                "op": "in",
                "value": ["LLP", "PARTNERSHIP"],
                "explain": "you are a firm or LLP",
            }
        ]
    },
    tags=["income-tax", "return", "annual-filing"],
    evidence=[doc("ack", "ITR-V acknowledgement")],
)

_it(
    "IN-IT-ITR3-ITR4",
    "The return for an individual or HUF with business income. ITR-4 for the "
    "presumptive schemes, ITR-3 otherwise, and audit cases have a later date than "
    "non-audit ones.",
    due=after_fy_end(4, 31),
    applicability={
        "all": [
            {
                "fact": "entity_type",
                "op": "in",
                "value": ["PROPRIETORSHIP", "HUF"],
                "explain": "you file as an individual or HUF",
            }
        ]
    },
    tags=["income-tax", "return", "annual-filing"],
    evidence=[doc("ack", "ITR-V acknowledgement")],
)

_it(
    "IN-IT-ITR7",
    "The return for a trust, institution or political party claiming exemption. "
    "Filing it late does not merely attract a fee — it can cost the exemption for "
    "the year, which is a different order of consequence.",
    due=after_fy_end(6, 30),
    applicability=CHARITABLE,
    tags=["income-tax", "return", "annual-filing", "exemption"],
    sector_tags=["ngo", "trust"],
    evidence=[doc("ack", "ITR-V acknowledgement")],
)

_it(
    "IN-IT-TAX-AUDIT-3CA",
    "Form 3CA is the audit report used where the accounts are already audited under "
    "another law — a company, for instance. 3CB is the alternative, and using the "
    "wrong one is a defect in the report.",
    due=after_fy_end(6, 30),
    applicability=company_and(
        {
            "fact": "aggregate_turnover",
            "op": "gte",
            "value": 10000000,
            "explain": "your turnover is above the tax audit threshold",
        }
    ),
    tags=["income-tax", "audit-report"],
    evidence=[doc("3ca", "Form 3CA")],
)

_it(
    "IN-IT-TAX-AUDIT-3CB",
    "Form 3CB is used where nothing else already requires an audit — a firm or "
    "proprietorship over the threshold. Paired with 3CD, never filed alone.",
    due=after_fy_end(6, 30),
    applicability={
        "all": [
            {
                "fact": "entity_type",
                "op": "in",
                "value": ["LLP", "PARTNERSHIP", "PROPRIETORSHIP", "HUF"],
                "explain": "your accounts are not audited under any other law",
            },
            {
                "fact": "aggregate_turnover",
                "op": "gte",
                "value": 10000000,
                "explain": "your turnover is above the tax audit threshold",
            },
        ]
    },
    tags=["income-tax", "audit-report"],
    evidence=[doc("3cb", "Form 3CB")],
)

_it(
    "IN-IT-BELATED-RETURN",
    "A safety net rather than a plan: if the return was not filed on time, there is "
    "still a window. Losses other than house-property losses cannot be carried "
    "forward from a belated return, which is usually the expensive part.",
    due=after_fy_end(9, 31),
    applicability=HAS_PAN,
    trigger_kind="INTERNAL",
    tags=["income-tax", "return", "correction"],
    evidence=[doc("ack", "ITR-V acknowledgement", False)],
)

_it(
    "IN-IT-REVISED-RETURN",
    "Correcting a return already filed. The deadline moves with the Finance Act, so "
    "it is deliberately not expressed here as a fixed December date.",
    due=after_fy_end(9, 31),
    applicability=HAS_PAN,
    trigger_kind="INTERNAL",
    tags=["income-tax", "return", "correction"],
    evidence=[doc("ack", "Revised ITR-V", False)],
)

_it(
    "IN-IT-ITRU",
    "The updated return, available for years after the ordinary window has closed, "
    "at the price of additional tax. Useful, and not a substitute for filing on "
    "time.",
    due=after_fy_end(23, 31),
    applicability=HAS_PAN,
    trigger_kind="INTERNAL",
    tags=["income-tax", "return", "correction"],
    evidence=[doc("ack", "ITR-U acknowledgement", False)],
    effective_from="2022-04-01",
)

_it(
    "IN-IT-SELF-ASSESSMENT-TAX",
    "Tax, interest and fee are paid before the return is furnished, not with it. A "
    "return uploaded without the self-assessment tax paid is treated as defective.",
    due=after_fy_end(6, 25),
    applicability=HAS_PAN,
    tags=["income-tax", "payment"],
    evidence=[doc("challan", "Challan")],
)

_it(
    "IN-IT-TAX-PAYMENT",
    "Any other income-tax payment — a demand, regular tax, an appeal deposit. The "
    "date comes from the notice rather than from the calendar.",
    due=after_fy_end(6, 30),
    applicability=HAS_PAN,
    trigger_kind="INTERNAL",
    tags=["income-tax", "payment"],
    evidence=[doc("challan", "Challan", False)],
)

_it(
    "IN-IT-AIS-REVIEW",
    "The Annual Information Statement is what the department already believes about "
    "your income. Reconciling it before filing is how a mismatch becomes a "
    "correction rather than a notice.",
    due=after_fy_end(5, 31),
    applicability=HAS_PAN,
    trigger_kind="INTERNAL",
    tags=["income-tax", "reconciliation", "internal-control"],
    evidence=[doc("review", "AIS review notes", False)],
    effective_from="2021-11-01",
)

_it(
    "IN-IT-TIS-REVIEW",
    "The Taxpayer Information Summary is the aggregated view of the AIS. Quicker to "
    "scan, and the place a large discrepancy shows up first.",
    due=after_fy_end(5, 31),
    applicability=HAS_PAN,
    trigger_kind="INTERNAL",
    tags=["income-tax", "reconciliation", "internal-control"],
    evidence=[doc("review", "TIS review notes", False)],
    effective_from="2021-11-01",
)

_it(
    "IN-IT-PAN-AADHAAR",
    "An inoperative PAN is not a filing problem, it is an everything problem: "
    "higher withholding on every payment received, and refunds withheld.",
    due=after_fy_start(2, 30),
    applicability=HAS_PAN,
    trigger_kind="INTERNAL",
    tags=["income-tax", "kyc", "internal-control"],
    evidence=[doc("status", "Linking status", False)],
    effective_from="2017-07-01",
)

_it(
    "IN-IT-TP-DOCUMENTATION",
    "Transfer-pricing documentation has to be *contemporaneous* — assembled by the "
    "report due date, not reconstructed when the assessment arrives. The penalty is "
    "for not having kept it, and it cannot be cured later.",
    due=after_fy_end(7, 31),
    applicability=NON_RESIDENT_DEALINGS,
    tags=["income-tax", "transfer-pricing"],
    evidence=[doc("documentation", "Rule 10D documentation")],
    effective_from="2001-04-01",
)

_it(
    "IN-IT-FORM3CEAA",
    "The master file: a group-level description of the business, its intangibles "
    "and its financing. Required only above the prescribed thresholds, and the "
    "thresholds are on the group rather than on the Indian entity.",
    due=after_fy_end(11, 30),
    applicability=INTERNATIONAL_GROUP,
    tags=["income-tax", "transfer-pricing", "disclosure"],
    evidence=[doc("3ceaa", "Form 3CEAA")],
    effective_from="2017-04-01",
)

_it(
    "IN-IT-FORM3CEAB",
    "Where several Indian entities of one group exist, they nominate one to file "
    "the master file. This is the intimation of that choice, and it is due before "
    "the master file itself.",
    due=after_fy_end(10, 31),
    applicability=INTERNATIONAL_GROUP,
    tags=["income-tax", "transfer-pricing"],
    evidence=[doc("3ceab", "Form 3CEAB")],
    effective_from="2017-04-01",
)

_it(
    "IN-IT-FORM3CEAC",
    "Notification of which group entity will file the country-by-country report, "
    "and where. Due two months before the report, and easily forgotten because the "
    "report itself is somebody else's job.",
    due=after_fy_end(10, 31),
    applicability=INTERNATIONAL_GROUP,
    tags=["income-tax", "transfer-pricing"],
    evidence=[doc("3ceac", "Form 3CEAC")],
    effective_from="2016-04-01",
)

_it(
    "IN-IT-FORM3CEAD",
    "The country-by-country report itself, filed by the Indian entity where the "
    "parent's jurisdiction has no exchange arrangement with India. Penalties here "
    "run per day and then step up.",
    due=after_fy_end(12, 31),
    applicability=INTERNATIONAL_GROUP,
    tags=["income-tax", "transfer-pricing", "disclosure"],
    evidence=[doc("3cead", "Form 3CEAD")],
    effective_from="2016-04-01",
)

_it(
    "IN-IT-NONRESIDENT-PE",
    "Where a non-resident has a taxable presence in India, several obligations "
    "arrive together — a return, withholding, transfer pricing and treaty "
    "positions. Tracked as one review because they are decided together.",
    due=after_fy_end(7, 31),
    applicability={
        "all": [
            {
                "fact": "entity_type",
                "op": "in",
                "value": ["BRANCH_OFFICE", "LIAISON_OFFICE"],
                "explain": "you are the Indian presence of a foreign company",
            }
        ]
    },
    tags=["income-tax", "non-resident", "treaty"],
    evidence=[doc("review", "Position paper")],
)

_it(
    "IN-IT-TRC",
    "A treaty benefit needs a residency certificate from the other country, held "
    "before the benefit is claimed. Obtained after the fact, it is often refused "
    "for the year in question.",
    due=after_fy_start(3, 30),
    applicability=NON_RESIDENT_DEALINGS,
    tags=["income-tax", "treaty", "non-resident", "certificate"],
    evidence=[doc("trc", "Tax residency certificate")],
)

_it(
    "IN-IT-FORM10F",
    "Form 10F supplies the treaty particulars a residency certificate leaves out. "
    "Now filed electronically, which caught out a lot of non-residents who had been "
    "handing over a signed paper form.",
    due=after_fy_start(3, 30),
    applicability=NON_RESIDENT_DEALINGS,
    tags=["income-tax", "treaty", "non-resident"],
    evidence=[doc("10f", "Filed Form 10F")],
    effective_from="2013-04-01",
)

_it(
    "IN-IT-EQUALISATION-LEVY-LEGACY",
    "Kept for historical periods only — the levy has been withdrawn prospectively. "
    "Past defaults still carry interest and penalty, which is the only reason this "
    "is still in the catalog.",
    due=after_fy_end(2, 30),
    applicability={
        "all": [
            {
                "fact": "has_ecommerce_sales",
                "op": "eq",
                "value": True,
                "explain": "you had e-commerce supply or services in a covered period",
            }
        ]
    },
    trigger_kind="INTERNAL",
    tags=["income-tax", "withholding", "internal-control"],
    evidence=[doc("statement", "Historical statement", False)],
    effective_from="2016-06-01",
)

_it(
    "IN-IT-FORM10A",
    "First registration or revalidation under 12A/12AB and 80G. Miss the window and "
    "the institution is taxed as an ordinary entity until it re-registers.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "TRUST_REGISTRATION_DUE"},
    due=trig(0),
    applicability=CHARITABLE,
    trigger_kind="EVENT_DRIVEN",
    tags=["income-tax", "registration", "exemption"],
    sector_tags=["ngo", "trust"],
    evidence=[doc("10a", "Form 10A acknowledgement")],
    effective_from="2021-04-01",
)

_it(
    "IN-IT-FORM10AB",
    "Renewal, conversion from provisional to regular registration, or a change in "
    "objects. Different triggers, one form, and each has its own window.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "TRUST_REGISTRATION_DUE"},
    due=trig(0),
    applicability=CHARITABLE,
    trigger_kind="EVENT_DRIVEN",
    tags=["income-tax", "registration", "exemption"],
    sector_tags=["ngo", "trust"],
    evidence=[doc("10ab", "Form 10AB acknowledgement")],
    effective_from="2021-04-01",
)

_it(
    "IN-IT-FORM10",
    "Income set aside rather than applied has to be declared, with the purpose, at "
    "least two months before the return is due. Filed late, the accumulation is "
    "simply taxable.",
    due=after_fy_end(4, 30),
    applicability=CHARITABLE,
    tags=["income-tax", "exemption", "declaration"],
    sector_tags=["ngo", "trust"],
    evidence=[doc("form10", "Form 10")],
)

_it(
    "IN-IT-FORM10BD",
    "The statement of donations received. Donors cannot claim their deduction until "
    "this is filed, so a late filing is felt by everybody who gave money.",
    due=after_fy_end(1, 31),
    applicability=CHARITABLE,
    tags=["income-tax", "donation", "return"],
    sector_tags=["ngo", "trust"],
    evidence=[doc("10bd", "Filed Form 10BD")],
    effective_from="2021-04-01",
)

_it(
    "IN-IT-FORM10BE",
    "The certificate issued to each donor once Form 10BD is filed. Generated from "
    "the statement, so it cannot be issued before it and must be by the same date.",
    due=after_fy_end(1, 31),
    applicability=CHARITABLE,
    tags=["income-tax", "donation", "certificate"],
    sector_tags=["ngo", "trust"],
    evidence=[doc("10be", "Issued certificates")],
    effective_from="2021-04-01",
)

_it(
    "IN-IT-EXEMPTION-APPLICATIONS",
    "The long tail of prescribed forms behind particular exemptions and deductions. "
    "No common deadline; tracked so that the ones an entity actually relies on are "
    "not discovered missing during an assessment.",
    due=after_fy_end(5, 31),
    applicability=CHARITABLE,
    trigger_kind="INTERNAL",
    tags=["income-tax", "exemption", "internal-control"],
    sector_tags=["ngo", "trust"],
    evidence=[doc("forms", "Filed forms", False)],
)

_it(
    "IN-IT-FORM29C",
    "The alternate minimum tax report, for non-corporate assessees claiming certain "
    "deductions. The corporate equivalent is Form 29B.",
    due=after_fy_end(6, 30),
    applicability={
        "all": [
            {
                "fact": "entity_type",
                "op": "in",
                "value": ["LLP", "PARTNERSHIP", "PROPRIETORSHIP", "HUF"],
                "explain": "you are a non-corporate assessee",
            },
            {
                "fact": "aggregate_turnover",
                "op": "gte",
                "value": 10000000,
                "explain": "you are large enough for the report to be required",
            },
        ]
    },
    tags=["income-tax", "audit-report"],
    evidence=[doc("29c", "Form 29C")],
)


# -- TDS on one-off transactions --------------------------------------------


def _tds_challan(code: str, summary: str, event: str, tag: str) -> None:
    SPECS[code] = Spec(
        summary=summary,
        periodicity="EVENT_BASED",
        trigger={"event_key": event},
        due={
            "anchor": "TRIGGER_DATE",
            "offset": {"months": 1, "day_of_month": 30},
            "shift_if_holiday": "NONE",
        },
        applicability=HAS_PAN,
        trigger_kind="EVENT_DRIVEN",
        tags=["tds", "withholding", tag],
        folder="tds",
        authority="CBDT",
        owner="org-finance",
        effective_from="2013-06-01",
        portal="https://www.incometax.gov.in",
        evidence=[doc("challan", "Challan-cum-statement")],
    )


_tds_challan(
    "IN-TDS-26QB",
    "Buying property above the threshold makes the *buyer* a tax deductor, usually "
    "for the only time in their life. There is no TAN and no quarterly return — one "
    "challan-cum-statement, thirty days after the month of deduction.",
    "PROPERTY_PURCHASED",
    "payment",
)
_tds_challan(
    "IN-TDS-26QC",
    "An individual or HUF paying rent above the monthly threshold deducts tax and "
    "files this. No TAN required, and one filing rather than a return every "
    "quarter.",
    "RENT_TDS_DEDUCTED",
    "payment",
)
_tds_challan(
    "IN-TDS-26QD",
    "Payments to a contractor or professional by an individual or HUF not otherwise "
    "required to deduct. Same shape as the rent and property cases.",
    "SPECIFIED_PAYMENT_TDS_DEDUCTED",
    "payment",
)
_tds_challan(
    "IN-TDS-26QE",
    "Tax on the transfer of a virtual digital asset by a specified person. The "
    "newest member of the challan-cum-statement family and the least familiar.",
    "VDA_TRANSFER",
    "payment",
)

SPECS["IN-TDS-CORRECTION"] = Spec(
    summary=(
        "A correction statement fixing a wrong PAN, a wrong section or a challan "
        "mismatch. Until it is filed the deductee cannot see the credit, and they "
        "will chase the deductor for it."
    ),
    periodicity="EVENT_BASED",
    trigger={"event_key": "TDS_DEFAULT_NOTICE"},
    due=trig(30),
    applicability=holds("TAN", "you hold a TAN and deduct tax at source"),
    trigger_kind="EVENT_DRIVEN",
    tags=["tds", "correction"],
    folder="tds",
    authority="CBDT",
    owner="org-finance",
    effective_from="2013-06-01",
    evidence=[doc("ack", "Correction acknowledgement")],
)

SPECS["IN-TDS-TRACES-DEFAULT"] = Spec(
    summary=(
        "Defaults raised on TRACES do not expire on their own — they accrue "
        "interest and become recoverable demands. Reviewed as a control rather than "
        "waited for as a deadline."
    ),
    periodicity="QUARTERLY",
    due={
        "anchor": "PERIOD_END",
        "offset": {"months": 2, "day_of_month": 28},
        "shift_if_holiday": "NONE",
    },
    applicability=holds("TAN", "you hold a TAN and deduct tax at source"),
    trigger_kind="INTERNAL",
    tags=["tds", "reconciliation", "internal-control"],
    folder="tds",
    authority="CBDT",
    owner="org-finance",
    effective_from="2013-06-01",
    evidence=[doc("review", "Default review", False)],
)


# -- GST registration lifecycle ----------------------------------------------

HAS_GST = holds("GST", "you hold a GST registration")


def _gst(code: str, summary: str, **kw: Any) -> None:
    kw.setdefault("effective_from", "2017-07-01")
    SPECS[code] = Spec(
        summary=summary,
        folder="gst",
        authority="CBIC",
        owner="org-finance",
        portal="https://www.gst.gov.in",
        **kw,
    )


_gst(
    "IN-GST-REG01",
    "Registration is due within thirty days of becoming liable, and liability is "
    "backdated to the day the threshold was crossed — so a late registration owes "
    "tax for a period in which it could not issue a tax invoice.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "GST_LIABILITY_AROSE"},
    due=trig(30),
    applicability={
        "all": [
            {
                "fact": "aggregate_turnover",
                "op": "gte",
                "value": 2000000,
                "explain": "your turnover is at or above the registration threshold",
            }
        ]
    },
    trigger_kind="EVENT_DRIVEN",
    tags=["gst", "registration"],
    evidence=[doc("rc", "Registration certificate")],
)

_gst(
    "IN-GST-REG14",
    "Changes to registration particulars — an address, a director, a bank account — "
    "are notified within fifteen days. Core-field changes need approval; the rest "
    "take effect on submission.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "GST_PARTICULARS_CHANGED"},
    due=trig(15),
    applicability=HAS_GST,
    instance_scope="REGISTRATION",
    scope_selector={"registration_type": "GST"},
    trigger_kind="EVENT_DRIVEN",
    tags=["gst", "amendment"],
    evidence=[doc("ack", "Amendment acknowledgement")],
)

_gst(
    "IN-GST-REG16",
    "Applying to cancel a registration that is no longer needed. Leaving it open "
    "instead means the returns keep falling due, and the late fees accrue on nil "
    "returns just the same.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "GST_CANCELLATION_EFFECTIVE"},
    due=trig(30),
    applicability=HAS_GST,
    instance_scope="REGISTRATION",
    scope_selector={"registration_type": "GST"},
    trigger_kind="EVENT_DRIVEN",
    tags=["gst", "cancellation"],
    evidence=[doc("application", "REG-16 application")],
)

_gst(
    "IN-GST-GSTR10",
    "The final return, three months after cancellation. Nothing else in GST is "
    "waiting on it, which is exactly why it is forgotten — and the late fee runs "
    "until it is filed, on a registration nobody is watching any more.",
    periodicity="EVENT_BASED",
    trigger={"event_key": "GST_CANCELLATION_EFFECTIVE"},
    due={"anchor": "TRIGGER_DATE", "offset": {"months": 3}, "shift_if_holiday": "NONE"},
    applicability=HAS_GST,
    instance_scope="REGISTRATION",
    scope_selector={"registration_type": "GST"},
    trigger_kind="EVENT_DRIVEN",
    tags=["gst", "return", "cancellation"],
    evidence=[doc("ack", "Filed GSTR-10")],
)

_gst(
    "IN-GST-GSTR1A",
    "An optional amendment window between GSTR-1 and GSTR-3B for the same period. "
    "Using it fixes the liability before it is paid; not using it means an "
    "amendment in a later period and an interest exposure in between.",
    periodicity="MONTHLY",
    due={
        "anchor": "PERIOD_END",
        "offset": {"months": 1, "day_of_month": 19},
        "shift_if_holiday": "NONE",
    },
    applicability={
        "all": [
            {
                "fact": "registrations",
                "op": "includes",
                "value": "GST",
                "explain": "you hold a GST registration",
            },
            {
                "fact": "gst_scheme",
                "op": "eq",
                "value": "REGULAR",
                "explain": "you file under the regular scheme",
            },
        ]
    },
    instance_scope="REGISTRATION",
    scope_selector={"registration_type": "GST"},
    trigger_kind="INTERNAL",
    tags=["gst", "correction", "monthly-filing"],
    evidence=[doc("ack", "Filed GSTR-1A", False)],
    effective_from="2024-08-01",
)

_gst(
    "IN-GST-GSTR11",
    "A UIN holder — an embassy or a notified body — files this to get its refund. "
    "Not a tax return; a claim, and it lapses if it is not made.",
    periodicity="MONTHLY",
    due={
        "anchor": "PERIOD_END",
        "offset": {"months": 1, "day_of_month": 28},
        "shift_if_holiday": "NONE",
    },
    applicability={
        "all": [
            {
                "fact": "registrations",
                "op": "includes",
                "value": "GST",
                "explain": "you hold a GST registration",
            },
            {
                "fact": "gst_scheme",
                "op": "in",
                "value": ["TDS_DEDUCTOR", "ISD"],
                "explain": "you are registered in a special category",
            },
        ]
    },
    instance_scope="REGISTRATION",
    scope_selector={"registration_type": "GST"},
    tags=["gst", "refund", "monthly-filing"],
    evidence=[doc("ack", "Filed GSTR-11", False)],
)


# ---------------------------------------------------------------------------
# Emitting
# ---------------------------------------------------------------------------

#: Order matters only for readability of the generated file. Keys the loader
#: requires come first, then what it applies to, then when, then the provenance.
KEY_ORDER = [
    "code",
    "country",
    "category",
    "family",
    "authority",
    "version",
    "title",
    "plain_language_summary",
    "statutory_reference",
    "filing_portal_url",
    "trigger_kind",
    "tags",
    "sector_tags",
    "periodicity",
    "period_anchor",
    "jurisdictions",
    "instance_scope",
    "scope_selector",
    "applicability",
    "trigger",
    "due",
    "evidence",
    "default_owner_role",
    "penalty_summary",
    "effective_from",
    "reviewed_by",
    "reviewed_at",
    "confidence",
]


def slugify(code: str) -> str:
    return code.removeprefix("IN-").lower().replace("_", "-")


def build(row: dict[str, str], spec: Spec) -> dict[str, Any]:
    document: dict[str, Any] = {
        "code": row["Code"],
        "country": "IN",
        "category": row["Category"],
        "family": row["Family"],
        "authority": spec.authority,
        "version": 1,
        "title": row["Compliance"],
        "plain_language_summary": spec.summary,
        "statutory_reference": row["Statutory Reference"],
        "periodicity": spec.periodicity,
        "instance_scope": spec.instance_scope,
        "applicability": spec.applicability,
        "due": spec.due,
        "evidence": spec.evidence,
        "default_owner_role": spec.owner,
        "penalty_summary": row["Penalty"],
        "effective_from": spec.effective_from,
        "reviewed_by": REVIEWED_BY,
        "reviewed_at": REVIEWED_AT,
        "confidence": spec.confidence,
    }
    if spec.portal:
        document["filing_portal_url"] = spec.portal
    if spec.trigger_kind != "STATUTORY_PERIODIC":
        document["trigger_kind"] = spec.trigger_kind
    if spec.tags:
        document["tags"] = spec.tags
    if spec.sector_tags:
        document["sector_tags"] = spec.sector_tags
    if spec.periodicity != "EVENT_BASED":
        document["period_anchor"] = spec.period_anchor
    if spec.jurisdictions:
        document["jurisdictions"] = spec.jurisdictions
    if spec.scope_selector:
        document["scope_selector"] = spec.scope_selector
    if spec.trigger:
        document["trigger"] = spec.trigger
    return {key: document[key] for key in KEY_ORDER if key in document}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report gaps, write nothing")
    args = parser.parse_args()

    import yaml  # imported here so --help works without the dependency

    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))

    # Where each code currently lives. Compared against the path *this* script
    # would write to, so a re-run overwrites its own output and still refuses to
    # touch a hand-authored file that happens to claim the same code. Keying on
    # the code alone would make the script a no-op the second time it ran.
    code_paths = {
        yaml.safe_load(path.read_text(encoding="utf-8"))["code"]: path
        for path in DEFINITIONS.rglob("*.yaml")
    }

    written = 0
    skipped: list[str] = []
    missing: list[str] = []

    for row in rows:
        code = row["Code"]
        spec = SPECS.get(code)
        if spec is None:
            if code not in code_paths:
                missing.append(code)
            else:
                skipped.append(code)
            continue

        path = DEFINITIONS / spec.folder / f"{slugify(code)}.yaml"
        current = code_paths.get(code)
        if current is not None and current.resolve() != path.resolve():
            # Somebody hand-wrote this one. Leave it alone and say so.
            skipped.append(code)
            continue

        if args.check:
            written += 1
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                build(row, spec),
                sort_keys=False,
                allow_unicode=True,
                default_flow_style=False,
                width=100,
            ),
            encoding="utf-8",
        )
        written += 1

    print(f"{written} written, {len(skipped)} already in the catalog, {len(missing)} with no spec")
    if missing:
        print("\nNo spec — these rows will not be generated:")
        for code in missing:
            print(f"  {code}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
