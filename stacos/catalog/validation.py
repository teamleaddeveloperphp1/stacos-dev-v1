"""
Semantic validation of the catalog as a whole.

Schema validation says a rule is well-formed. This says it is *right* — or at
least that it is not obviously wrong in one of the five ways catalog content
actually goes wrong. It runs against the live catalog on every catalog pull
request, and it is the highest-return test suite in the project, because a bad
definition reaching production is silently wrong for every tenant at once and is
discovered by a client missing a filing.

Five checks, each earning its place:

**Dead rules.** A definition that is applicable to no persona in the library is
almost always a typo — ``registrations includes "GSTIN"`` instead of ``"GST"``
evaluates cleanly, validates cleanly, and never fires. Nothing else catches it.

**Undiscriminating rules.** A definition applicable to every persona, including
the one-person proprietorship and the dormant holding company, is missing a
clause. This is how a factory return ends up on a software consultancy's
calendar.

**Unresolvable dates.** A due rule that produces no date for any period of a
plausible horizon is broken, unless it is deliberately event-anchored — in which
case it must say so.

**Stale review.** ``plain_language_summary`` is legal guidance a client acts on.
A published definition whose statutory review is older than twelve months fails
the build.

**Structural mismatches.** A registration-scoped definition naming a
registration type that does not exist; a category outside the vocabulary
engagements are scoped along.

Deliberately runnable without a database, straight from the YAML, so it is a
merge gate rather than a deployment gate.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from stacos.catalog.loader import DefinitionDocument
from stacos.catalog.personas import PERSONAS
from stacos.engine.dates import generate_periods, resolve_due_date
from stacos.engine.rules import V, evaluate
from stacos.engine.types import (
    CalendarSnapshot,
    DefinitionSnapshot,
    FiscalYearConvention,
    InstanceScope,
    Periodicity,
)
from stacos.jurisdictions.facts import PREMISES_TYPES, REGISTRATION_TYPES

__all__ = ["Finding", "Level", "validate_catalog"]


class Level(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


@dataclass(frozen=True, slots=True)
class Finding:
    level: Level
    code: str
    check: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.code} ({self.check}): {self.message}"


#: Categories an engagement can be scoped along. Mirrors
#: ``tenancy.ComplianceCategory``; duplicated as a literal tuple so this module
#: stays importable without Django, which is what makes it a merge gate.
VALID_CATEGORIES: frozenset[str] = frozenset(
    {
        "TAX_INDIRECT",
        "TAX_DIRECT",
        "CORPORATE_SECRETARIAL",
        "LABOUR",
        "ENVIRONMENT",
        "SAFETY_FIRE",
        "LICENSING",
        "FEMA_RBI",
        "SECTORAL",
        "DATA_PRIVACY",
        "INTERNAL_GOVERNANCE",
    }
)

#: Anchors that legitimately produce no date until a human records something.
#: A definition using one of these is exempt from the resolvability check.
_EVENT_ANCHORS = frozenset({"EVENT_DATE", "LICENCE_EXPIRY", "PREVIOUS_INSTANCE_DATE"})

#: India's convention, used only to *exercise* the date rules during validation.
#: Not a hardcoded assumption in the product: the real one comes from the
#: jurisdiction pack. Validation needs some convention to generate periods
#: against, and the alternative is not validating dates at all.
_VALIDATION_FY = FiscalYearConvention(4, 1, "FY{start_year}-{end_year_short}")

MAX_REVIEW_AGE_DAYS = 365


def validate_catalog(
    documents: Sequence[DefinitionDocument],
    *,
    as_of: date,
    strict: bool = False,
    bundles: Sequence[Any] = (),
) -> list[Finding]:
    """Check every definition, and the catalog as a population.

    :param strict: promote warnings to errors. What CI uses on the ``main``
        branch; a feature branch may legitimately carry a warning while a rule is
        being drafted.
    """
    findings: list[Finding] = []

    findings.extend(_check_effective_windows(documents))

    profiles = [persona.to_profile() for persona in PERSONAS]

    for document in documents:
        findings.extend(_check_structure(document))
        findings.extend(_check_review(document, as_of=as_of))
        findings.extend(_check_population(document, profiles))
        findings.extend(_check_dates_resolve(document, as_of=as_of))

    findings.extend(_check_bundles(bundles, documents))

    if strict:
        return [
            Finding(Level.ERROR, f.code, f.check, f.message) if f.level is Level.WARNING else f
            for f in findings
        ]
    return findings


# ---------------------------------------------------------------------------


def _check_bundles(
    bundles: Sequence[Any], documents: Sequence[DefinitionDocument]
) -> list[Finding]:
    """A pack has to deliver what its card promises.

    Two ways it can fail to. A code that does not exist means the pack quietly
    adds fewer obligations than it claims — the failure is invisible, because a
    shorter list still looks like a list. And a pack whose definitions all apply
    unconditionally is a card that adds nothing anybody did not already have.
    """
    findings: list[Finding] = []
    known = {document.code for document in documents}
    conditional = {document.code for document in documents if document.applicability_rule}

    for bundle in bundles:
        missing = [code for code in bundle.definition_codes if code not in known]
        if missing:
            findings.append(
                Finding(
                    Level.ERROR,
                    bundle.code,
                    "pack-unknown-code",
                    f"references definitions that do not exist: {missing}. Pressing the "
                    f"button would add fewer obligations than the card says.",
                )
            )
            continue

        if not any(code in conditional for code in bundle.definition_codes):
            findings.append(
                Finding(
                    Level.WARNING,
                    bundle.code,
                    "pack-redundant",
                    "every definition in this pack applies unconditionally, so adopting "
                    "it adds nothing the entity did not already have.",
                )
            )

    return findings


def _check_effective_windows(documents: Sequence[DefinitionDocument]) -> list[Finding]:
    """No two published versions of one definition may claim the same instant.

    The database enforces this with an exclusion constraint, but discovering it
    at ``migrate`` time means the pull request already merged. Same rule, checked
    early.
    """
    findings: list[Finding] = []
    by_code: dict[str, list[DefinitionDocument]] = {}
    for document in documents:
        by_code.setdefault(document.code, []).append(document)

    for code, versions in by_code.items():
        published = sorted(
            (d for d in versions if d.status == "PUBLISHED"), key=lambda d: d.effective_from
        )
        for earlier, later in itertools.pairwise(published):
            earlier_end = earlier.effective_to
            if earlier_end is None or earlier_end >= later.effective_from:
                findings.append(
                    Finding(
                        Level.ERROR,
                        code,
                        "effective-windows",
                        f"v{earlier.version} (from {earlier.effective_from}, to "
                        f"{earlier_end or 'open'}) overlaps v{later.version} (from "
                        f"{later.effective_from}). Close the earlier window on the day "
                        f"before the later one opens.",
                    )
                )
    return findings


def _check_structure(document: DefinitionDocument) -> list[Finding]:
    findings: list[Finding] = []

    if document.category not in VALID_CATEGORIES:
        findings.append(
            Finding(
                Level.ERROR,
                document.code,
                "category",
                f"{document.category!r} is not a compliance category. Engagements are "
                f"scoped along these, so an unknown one is invisible to a practice.",
            )
        )

    if document.instance_scope == InstanceScope.REGISTRATION:
        wanted = str(document.scope_selector.get("registration_type", ""))
        if wanted not in REGISTRATION_TYPES:
            findings.append(
                Finding(
                    Level.ERROR,
                    document.code,
                    "scope-selector",
                    f"registration_type {wanted!r} is not a known registration type. "
                    f"This definition would fan out to nothing.",
                )
            )

    if document.instance_scope == InstanceScope.PREMISES:
        wanted = str(document.scope_selector.get("premises_type", ""))
        if wanted not in PREMISES_TYPES:
            findings.append(
                Finding(
                    Level.ERROR,
                    document.code,
                    "scope-selector",
                    f"premises_type {wanted!r} is not a known premises type.",
                )
            )

    if not document.plain_language_summary:
        findings.append(
            Finding(
                Level.WARNING,
                document.code,
                "summary",
                "no plain_language_summary. The detail page will show the statutory "
                "title to a business owner, which is not an explanation.",
            )
        )

    if not document.penalty_summary:
        findings.append(
            Finding(
                Level.WARNING,
                document.code,
                "penalty",
                "no penalty_summary. What it costs to miss this is what actually gets "
                "a client to answer an information request.",
            )
        )

    return findings


def _check_review(document: DefinitionDocument, *, as_of: date) -> list[Finding]:
    """A published definition needs a dated statutory review.

    ``plain_language_summary`` is guidance a client relies on. Shipping it with
    nobody's name against it, or with a review from two years ago, is the legal
    exposure the design called out and the reason these fields exist at all.
    """
    if document.status != "PUBLISHED":
        return []

    if document.reviewed_at is None or not document.reviewed_by:
        return [
            Finding(
                Level.ERROR,
                document.code,
                "review",
                "published without reviewed_by/reviewed_at. Someone has to have checked "
                "this against the statute, and the record has to say who and when.",
            )
        ]

    age = (as_of - document.reviewed_at).days
    if age > MAX_REVIEW_AGE_DAYS:
        return [
            Finding(
                Level.ERROR,
                document.code,
                "review",
                f"last reviewed {document.reviewed_at} ({age} days ago). Compliance "
                f"guidance older than twelve months is worse than none.",
            )
        ]
    return []


def _check_population(
    document: DefinitionDocument,
    profiles: Sequence[object],
) -> list[Finding]:
    """Dead-rule and discrimination checks, against the persona library."""
    if not document.applicability_rule:
        # An unconditional definition is legitimate — every company files an
        # annual return — and by construction applies to everyone, so the
        # discrimination check would be a false positive.
        return []

    applicable = 0
    unknown = 0
    for profile in profiles:
        facts = getattr(profile, "facts", {})
        verdict = evaluate(document.applicability_rule, facts)
        if verdict.result is V.TRUE:
            applicable += 1
        elif verdict.result is V.UNKNOWN:
            unknown += 1

    findings: list[Finding] = []
    total = len(profiles)

    if applicable == 0 and unknown == 0:
        findings.append(
            Finding(
                Level.ERROR,
                document.code,
                "dead-rule",
                f"applicable to none of the {total} personas and undecided for none "
                f"either. A rule that can never fire is almost always a typo in a fact "
                f"name or an expected value.",
            )
        )
    elif applicable == total:
        findings.append(
            Finding(
                Level.WARNING,
                document.code,
                "discrimination",
                f"applicable to all {total} personas, including the one-person "
                f"proprietorship and the dormant holding company. A clause is probably "
                f"missing.",
            )
        )

    return findings


def _check_dates_resolve(document: DefinitionDocument, *, as_of: date) -> list[Finding]:
    """A due rule must produce a date for at least one period.

    Event-anchored rules are exempt: those legitimately return ``UNRESOLVED``
    until somebody records the AGM date, and the calendar shows "tell us your AGM
    date to schedule this" rather than guessing. Everything else producing no date
    at all is broken.
    """
    anchor = str(document.due_rule.get("anchor", "PERIOD_END"))
    if anchor in _EVENT_ANCHORS:
        return []

    periodicity = Periodicity(document.periodicity)
    if periodicity is Periodicity.EVENT_BASED:
        return []

    snapshot = DefinitionSnapshot(
        code=document.code,
        version=document.version,
        title=document.title,
        country=document.country,
        periodicity=periodicity,
        due_rule=document.due_rule,
        period_anchor=document.period_anchor,
        effective_from=document.effective_from,
        effective_to=document.effective_to,
    )

    periods = generate_periods(
        periodicity=periodicity,
        fy=_VALIDATION_FY,
        window_start=as_of - timedelta(days=730),
        window_end=as_of + timedelta(days=730),
        period_anchor=document.period_anchor,
    )
    if not periods:
        return [
            Finding(
                Level.ERROR,
                document.code,
                "periods",
                f"periodicity {document.periodicity} generated no periods over a four-year window.",
            )
        ]

    resolved = 0
    for period in periods:
        resolution = resolve_due_date(
            snapshot, period, calendars=CalendarSnapshot(), fy=_VALIDATION_FY
        )
        if resolution.resolved:
            resolved += 1

    if resolved == 0:
        return [
            Finding(
                Level.ERROR,
                document.code,
                "due-date",
                f"due rule with anchor {anchor} resolved to a date for none of the "
                f"{len(periods)} periods generated. The obligation would materialise "
                f"with no date and no explanation.",
            )
        ]

    # A date more than two years after its period end is nearly always an offset
    # expressed in the wrong unit — `months: 20` where `days: 20` was meant.
    for period in periods[:4]:
        resolution = resolve_due_date(
            snapshot, period, calendars=CalendarSnapshot(), fy=_VALIDATION_FY
        )
        if resolution.effective_date and (resolution.effective_date - period.end).days > 730:
            return [
                Finding(
                    Level.WARNING,
                    document.code,
                    "due-date",
                    f"period {period.key} ends {period.end} but falls due "
                    f"{resolution.effective_date}, more than two years later. Check the "
                    f"offset units.",
                )
            ]

    return []
