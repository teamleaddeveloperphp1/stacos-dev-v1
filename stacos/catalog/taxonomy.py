"""
The navigation vocabularies: tags and sector tags.

``category`` says which department owns a thing and scopes an engagement along
it. ``family`` says which body of law it comes from. Neither answers the question
a user actually asks while choosing what to track — "show me the director-related
ROC filings", "show me what an NBFC has to do" — and adding more enum members to
``category`` would break engagement scoping to serve a filter.

So: tags. Free-form in the YAML, controlled here. Controlled because an
uncontrolled tag list acquires ``roc``, ``ROC`` and ``registrar`` within a year
and then nothing can be filtered reliably — the same failure the fact registry
was built to prevent, and there is no reason to learn it twice.

Django-free on purpose. ``manage.py validatecatalog`` is a merge gate that runs
without a database, and it has to be able to reject an unknown tag.
"""

from __future__ import annotations

__all__ = ["CATALOG_TAGS", "SECTOR_TAGS", "unknown_sector_tags", "unknown_tags"]

#: What the obligation is *about*. Deliberately about subject matter rather than
#: about mechanism — periodicity, scope and trigger kind are already columns, and
#: a tag that duplicates a column is a second place to get it wrong.
CATALOG_TAGS: frozenset[str] = frozenset(
    {
        # Bodies of law and the regulator's own vocabulary
        "roc",
        "gst",
        "tds",
        "tcs",
        "income-tax",
        "transfer-pricing",
        "fema",
        "rbi",
        "epf",
        "esic",
        "professional-tax",
        "labour-welfare",
        "factories",
        "contract-labour",
        "posh",
        "environment",
        "waste",
        "pollution",
        "fire-safety",
        "plant-safety",
        "licensing",
        "data-privacy",
        # Subject matter
        "director",
        "kmp",
        "auditor",
        "secretarial-audit",
        "board-meeting",
        "general-meeting",
        "minutes",
        "statutory-register",
        "shareholding",
        "beneficial-ownership",
        "share-capital",
        "dividend",
        "iepf",
        "charge",
        "csr",
        "related-party",
        "deposits",
        "msme",
        "financial-statements",
        "annual-return",
        "audit-report",
        "remuneration",
        "payroll",
        "employee-onboarding",
        "employee-exit",
        "kyc",
        "registration",
        "amendment",
        "cancellation",
        "return",
        "payment",
        "reconciliation",
        "certificate",
        "declaration",
        "disclosure",
        "donation",
        "exemption",
        "treaty",
        "non-resident",
        "withholding",
        "refund",
        "correction",
        # Cadence, as a user would say it rather than as the enum spells it
        "annual-filing",
        "monthly-filing",
        "quarterly-filing",
        "half-yearly-filing",
        "renewal",
        "internal-control",
        "governance",
    }
)

#: Which kinds of business an obligation is peculiar to. Empty on a definition
#: means "not sector-specific", which is the overwhelming majority. This is what
#: makes the SECTORAL category — valid in the vocabulary and used by nothing —
#: finally mean something, and it gives onboarding one high-yield question.
SECTOR_TAGS: frozenset[str] = frozenset(
    {
        "listed",
        "nbfc",
        "banking",
        "insurance",
        "real-estate",
        "pharma",
        "food",
        "chemicals",
        "textiles",
        "manufacturing",
        "construction",
        "logistics",
        "e-commerce",
        "it-services",
        "education",
        "healthcare",
        "hospitality",
        "mining",
        "power",
        "telecom",
        "ngo",
        "trust",
        "exporter",
        "importer",
        "startup",
    }
)


def unknown_tags(tags: list[str]) -> list[str]:
    """Tags outside the controlled vocabulary, in the order they appeared."""
    return [tag for tag in tags if tag not in CATALOG_TAGS]


def unknown_sector_tags(tags: list[str]) -> list[str]:
    return [tag for tag in tags if tag not in SECTOR_TAGS]
