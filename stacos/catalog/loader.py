"""
Loading the catalog from YAML in git into the database.

**The repository is the source of truth; the database is a cache of it.** That
one decision is what makes catalog changes reviewable, testable in CI, rollable
back, and rebuildable for any historical date.

The alternatives were considered and rejected for concrete reasons:

*Django fixtures* have no versioning semantics, no partial load, and a domain
expert cannot review a fixture diff.

*Data migrations* are an append-only, non-re-runnable log designed for **schema**.
Catalog content changes weekly with every GST notification: you would accumulate
hundreds of unreviewable files, be unable to correct a past one, and couple a
"the due date moved" fix to a code deploy — which is exactly backwards.

*Admin-UI-only authoring* has no code review, no CI validation, no rollback and
no diff history. It becomes the source of the product's worst outages.

What YAML buys: a pull request that reads like the law changed, validation as a
merge gate, and a one-command rollback because published versions are immutable.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from django.db import transaction

from stacos.catalog.models import (
    ComplianceDefinition,
    DefinitionVersion,
    GovernmentExtension,
    PublicationStatus,
)
from stacos.catalog.taxonomy import unknown_sector_tags, unknown_tags
from stacos.engine.probe import possible_for
from stacos.engine.rules import RuleError, V, facts_used, validate_rule
from stacos.engine.types import InstanceScope, Periodicity, ShiftRule
from stacos.jurisdictions.events import EVENT_TYPES
from stacos.jurisdictions.facts import ENTITY_TYPES, REGISTRY
from stacos.jurisdictions.models import Authority

__all__ = [
    "CATALOG_ROOT",
    "DefinitionDocument",
    "LoadReport",
    "iter_documents",
    "load_catalog",
    "parse_document",
]

#: Where the YAML lives. One file per definition, grouped by family, so a diff
#: touching GST cannot accidentally rewrite the labour rules.
CATALOG_ROOT = Path(__file__).resolve().parents[2] / "catalog"

#: Fields that make up the rule payload. Changing any of them on a published
#: version is forbidden; that is what "append-only" means in practice.
_PAYLOAD_FIELDS = (
    "title",
    "periodicity",
    "period_anchor",
    "jurisdictions",
    "applicability_rule",
    "instance_scope",
    "scope_selector",
    "trigger_rule",
    "due_rule",
    "evidence_requirements",
    "effective_from",
    "effective_to",
)

#: Bumped whenever a column *derived* from the YAML changes shape or meaning —
#: ``possible_entity_types`` today.
#:
#: It is folded into the checksum, and that is not decoration. ``_persist``
#: short-circuits on an unchanged checksum, and the checksum hashes the raw YAML,
#: which a derived-column migration does not touch. Without this, the first
#: ``loadcatalog`` after such a migration reports "unchanged" for every
#: definition and leaves the new column empty on all of them. Bumping it changes
#: every checksum, so every row re-persists.
#:
#: It cannot trip the published-immutability guard: that guard compares only
#: ``_PAYLOAD_FIELDS``, none of which move, so the changed list is empty and
#: ``_persist`` falls through to a plain save.
_DERIVED_SCHEMA_VERSION = 1

#: Fingerprint of the entity-type vocabulary the probe index was computed
#: against, stored on each version so a system check can spot a stale index.
PROBE_SIGNATURE = hashlib.sha256(",".join(ENTITY_TYPES).encode("utf-8")).hexdigest()[:16]

#: Mirrors ``ComplianceDefinition.TriggerKind``. A literal here, like
#: ``VALID_CATEGORIES`` in validation.py, so parsing stays importable without
#: the Django app registry.
_TRIGGER_KINDS = frozenset(
    {"STATUTORY_PERIODIC", "EVENT_DRIVEN", "RENEWAL", "GOVERNANCE", "INTERNAL"}
)


class CatalogError(ValueError):
    """A YAML document that cannot be loaded, with the file that caused it."""

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        super().__init__(f"{source}: {message}")


@dataclass(slots=True)
class DefinitionDocument:
    """One parsed and validated YAML definition, ready to persist."""

    code: str
    country: str
    category: str
    family: str
    authority_code: str
    version: int
    title: str
    periodicity: str
    period_anchor: str
    jurisdictions: list[str]
    applicability_rule: dict[str, Any]
    instance_scope: str
    scope_selector: dict[str, Any]
    trigger_rule: dict[str, Any]
    due_rule: dict[str, Any]
    evidence_requirements: list[dict[str, Any]]
    effective_from: date
    effective_to: date | None
    trigger_kind: str = "STATUTORY_PERIODIC"
    tags: list[str] = field(default_factory=list)
    sector_tags: list[str] = field(default_factory=list)
    plain_language_summary: str = ""
    statutory_reference: str = ""
    filing_portal_url: str = ""
    default_owner_role: str = ""
    penalty_summary: str = ""
    reviewed_by: str = ""
    reviewed_at: date | None = None
    confidence: str = "MEDIUM"
    status: str = PublicationStatus.PUBLISHED
    source_path: str = ""
    checksum: str = ""

    @property
    def facts(self) -> list[str]:
        return sorted(facts_used(self.applicability_rule))

    @property
    def possible_entity_types(self) -> list[str]:
        """Legal forms this rule cannot be refuted for, knowing only country and
        entity type.

        Note that ``known`` carries exactly two keys. Everything else is unknown
        *by construction* rather than by remembering to leave it out — which is
        what keeps the probe honest. Hand it a profile-shaped dict and
        ``registrations: []`` would read as definite absence and turn most of the
        catalog FALSE at once.
        """
        return [
            entity_type
            for entity_type in ENTITY_TYPES
            if possible_for(
                self.applicability_rule, {"country": self.country, "entity_type": entity_type}
            )
            is not V.FALSE
        ]


@dataclass(slots=True)
class LoadReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"{len(self.created)} created, {len(self.updated)} updated, "
            f"{len(self.unchanged)} unchanged, {len(self.errors)} failed"
        )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def iter_documents(root: Path | None = None) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield every catalog YAML file and its raw contents.

    Sorted so a load is deterministic and two runs produce identical logs — which
    matters when the log is what a reviewer diffs to confirm a catalog change did
    only what it claimed.
    """
    base = root or (CATALOG_ROOT / "definitions")
    if not base.exists():
        return
    for path in sorted(base.rglob("*.yaml")):
        with path.open("r", encoding="utf-8") as handle:
            # `safe_load` rather than `load`: catalog files are reviewed, but a
            # loader that can construct arbitrary Python objects is not something
            # to leave lying around in a product that will eventually accept
            # partner-contributed jurisdiction packs.
            raw = yaml.safe_load(handle)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise CatalogError(str(path), "top level must be a mapping")
        yield path, raw


def _checksum(raw: Mapping[str, Any]) -> str:
    """Hash the *semantic* content, not the file bytes.

    Reformatting a YAML file, rewrapping a summary or reordering keys must not
    read as a change to the law. Hashing the parsed structure means only a real
    edit trips the immutability guard.
    """
    canonical = yaml.safe_dump(dict(raw), sort_keys=True, default_flow_style=False)
    # The derived-schema version is part of the hash so that a change to a
    # *computed* column invalidates every row. See _DERIVED_SCHEMA_VERSION.
    canonical = f"v{_DERIVED_SCHEMA_VERSION}\n{canonical}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_document(path: Path, raw: Mapping[str, Any]) -> DefinitionDocument:
    """Validate one YAML document and return it in structured form.

    Every problem that can be detected without touching the database is detected
    here, because a catalog author wants the whole list of what is wrong with
    their file, not the first thing that happened to fail.
    """
    source = str(path)

    def required(key: str) -> Any:
        if key not in raw:
            raise CatalogError(source, f"missing required key {key!r}")
        return raw[key]

    code = str(required("code")).strip()
    if not code:
        raise CatalogError(source, "code must not be empty")

    periodicity = str(required("periodicity"))
    try:
        Periodicity(periodicity)
    except ValueError:
        raise CatalogError(
            source,
            f"unknown periodicity {periodicity!r}; expected one of {sorted(Periodicity)}",
        ) from None

    instance_scope = str(raw.get("instance_scope", InstanceScope.ENTITY))
    try:
        InstanceScope(instance_scope)
    except ValueError:
        raise CatalogError(source, f"unknown instance_scope {instance_scope!r}") from None

    due_rule = dict(required("due") or {})

    # `offset_by_month` is keyed on a month number. YAML parses `3:` as the int 3;
    # JSONB round-trips it back as the string "3". Left alone, the stored rule and
    # the parsed rule never compare equal, so every reload of the five definitions
    # using it looks like the law changed and trips the immutability guard.
    # `_select_offset` already accepts both forms, so canonicalising to strings at
    # this boundary costs nothing and makes the comparison honest.
    if isinstance(due_rule.get("offset_by_month"), dict):
        due_rule["offset_by_month"] = {
            str(month): offset for month, offset in due_rule["offset_by_month"].items()
        }

    # A day_of_month of 0 — or 40 — is an authoring slip that reaches
    # `set_day_of_month` as `date(y, m, 0)` and raises ValueError from inside date
    # resolution, where the traceback says nothing about which file caused it.
    # -1 is the deliberate "last day of the month" sentinel.
    for offset in [due_rule.get("offset"), *(due_rule.get("offset_by_month") or {}).values()]:
        if not isinstance(offset, dict) or "day_of_month" not in offset:
            continue
        day_of_month = int(offset["day_of_month"])
        if day_of_month != -1 and not 1 <= day_of_month <= 31:
            raise CatalogError(
                source,
                f"day_of_month {day_of_month} is not a day. Use 1-31, or -1 for the "
                f"last day of the month.",
            )

    if "shift_if_holiday" not in due_rule:
        # Mandatory rather than defaulted. Indian statutory tax dates do not
        # shift for weekends or holidays — the portals accept filings on a
        # Sunday, and relief comes through explicit government extensions. A
        # default of NEXT_WORKING_DAY would produce dates that are wrong in law,
        # so an author has to decide consciously, per definition.
        raise CatalogError(
            source,
            "due.shift_if_holiday is required. Use NONE for statutory filing dates; "
            "use a shifting rule only for physically-dependent obligations such as "
            "counter filings, cheque payments or inspections.",
        )
    try:
        ShiftRule(str(due_rule["shift_if_holiday"]))
    except ValueError:
        raise CatalogError(
            source, f"unknown due.shift_if_holiday {due_rule['shift_if_holiday']!r}"
        ) from None

    applicability = dict(raw.get("applicability") or {})
    rule_errors: list[RuleError] = validate_rule(applicability, known_facts=REGISTRY.keys())
    if rule_errors:
        raise CatalogError(source, "; ".join(str(e) for e in rule_errors))

    scope_selector = dict(raw.get("scope_selector") or {})
    if instance_scope == InstanceScope.REGISTRATION and not scope_selector.get("registration_type"):
        raise CatalogError(
            source,
            "instance_scope REGISTRATION needs scope_selector.registration_type — "
            "otherwise the obligation fans out across every registration the "
            "entity holds, including unrelated ones.",
        )
    if instance_scope == InstanceScope.PREMISES and not scope_selector.get("premises_type"):
        raise CatalogError(source, "instance_scope PREMISES needs scope_selector.premises_type")

    # -- Triggers -----------------------------------------------------------
    # Five checks on a new shape, so none of the existing 129 files can newly
    # fail: not one of them has a `trigger:` block or EVENT_BASED periodicity.
    trigger_rule = dict(raw.get("trigger") or {})
    is_event_based = periodicity == Periodicity.EVENT_BASED
    anchor = str(due_rule.get("anchor", "PERIOD_END"))

    if is_event_based and not trigger_rule:
        raise CatalogError(
            source,
            "periodicity EVENT_BASED needs a `trigger:` block naming the event that "
            "brings the obligation into existence. Without one it loads cleanly and "
            "materialises nothing, ever, with every test still green.",
        )
    if trigger_rule and not is_event_based:
        raise CatalogError(
            source,
            "a `trigger:` block requires periodicity EVENT_BASED. On a periodic "
            "definition it would be ignored, and the rows would keep appearing every "
            "period whether or not the event ever happened.",
        )
    if trigger_rule:
        event_key = str(trigger_rule.get("event_key", ""))
        if event_key not in EVENT_TYPES:
            raise CatalogError(
                source,
                f"unknown trigger.event_key {event_key!r}. Add it to "
                f"stacos/jurisdictions/events.py first — a key nothing can record is a "
                f"definition that never fires.",
            )
        event_type = EVENT_TYPES.get(event_key)
        assert event_type is not None
        if str(event_type.scope) != instance_scope:
            raise CatalogError(
                source,
                f"{event_key} attaches to {event_type.scope} but this definition is "
                f"scoped {instance_scope}. The event could never match a scope.",
            )
        if anchor != "TRIGGER_DATE":
            raise CatalogError(
                source,
                "an event-triggered definition must use due.anchor: TRIGGER_DATE, which "
                "dates each instance from its own occurrence. EVENT_DATE reads the "
                "latest date recorded for the key, so every filing of the year would be "
                "dated from the most recent event.",
            )
        when_errors = validate_rule(
            dict(trigger_rule.get("when") or {}), known_facts=event_type.attribute_keys()
        )
        if when_errors:
            raise CatalogError(source, "; ".join(str(e) for e in when_errors))

    if anchor == "TRIGGER_DATE" and not trigger_rule:
        raise CatalogError(source, "due.anchor TRIGGER_DATE has no trigger to anchor on.")

    if anchor == "EVENT_DATE" and str(due_rule.get("event_key", "")) not in EVENT_TYPES:
        raise CatalogError(
            source,
            f"unknown due.event_key {due_rule.get('event_key')!r}. This anchor waits for "
            f"a date nobody can record, so the obligation would prompt forever.",
        )

    # -- Navigation facets ---------------------------------------------------
    tags = [str(tag) for tag in (raw.get("tags") or [])]
    bad_tags = unknown_tags(tags)
    if bad_tags:
        raise CatalogError(
            source,
            f"unknown tag(s) {bad_tags}. Add them to stacos/catalog/taxonomy.py — an "
            f"uncontrolled tag list acquires three spellings of one word and then "
            f"nothing filters reliably.",
        )
    sector_tags = [str(tag) for tag in (raw.get("sector_tags") or [])]
    bad_sectors = unknown_sector_tags(sector_tags)
    if bad_sectors:
        raise CatalogError(source, f"unknown sector_tag(s) {bad_sectors}.")

    trigger_kind = str(raw.get("trigger_kind", "STATUTORY_PERIODIC"))
    if trigger_kind not in _TRIGGER_KINDS:
        raise CatalogError(
            source, f"unknown trigger_kind {trigger_kind!r}; expected one of {sorted(_TRIGGER_KINDS)}"
        )

    effective_from = _as_date(source, "effective_from", required("effective_from"))
    effective_to = (
        _as_date(source, "effective_to", raw["effective_to"]) if raw.get("effective_to") else None
    )
    if effective_to and effective_to < effective_from:
        raise CatalogError(source, "effective_to is before effective_from")

    evidence = list(raw.get("evidence") or [])
    for item in evidence:
        if not isinstance(item, dict) or not item.get("key"):
            raise CatalogError(source, f"evidence entries need a key: {item!r}")

    return DefinitionDocument(
        code=code,
        country=str(required("country")),
        category=str(required("category")),
        family=str(raw.get("family", "")),
        authority_code=str(raw.get("authority", "")),
        version=int(raw.get("version", 1)),
        title=str(required("title")),
        periodicity=periodicity,
        period_anchor=str(raw.get("period_anchor", "FY")),
        jurisdictions=[str(j) for j in (raw.get("jurisdictions") or [])],
        applicability_rule=applicability,
        instance_scope=instance_scope,
        scope_selector=scope_selector,
        trigger_rule=trigger_rule,
        due_rule=due_rule,
        evidence_requirements=evidence,
        effective_from=effective_from,
        effective_to=effective_to,
        trigger_kind=trigger_kind,
        tags=tags,
        sector_tags=sector_tags,
        plain_language_summary=str(raw.get("plain_language_summary", "")).strip(),
        statutory_reference=str(raw.get("statutory_reference", "")),
        filing_portal_url=str(raw.get("filing_portal_url", "")),
        default_owner_role=str(raw.get("default_owner_role", "")),
        penalty_summary=str(raw.get("penalty_summary", "")),
        reviewed_by=str(raw.get("reviewed_by", "")),
        reviewed_at=_as_date(source, "reviewed_at", raw["reviewed_at"])
        if raw.get("reviewed_at")
        else None,
        confidence=str(raw.get("confidence", "MEDIUM")),
        status=str(raw.get("status", PublicationStatus.PUBLISHED)),
        source_path=str(path.relative_to(CATALOG_ROOT.parent))
        if path.is_relative_to(CATALOG_ROOT.parent)
        else source,
        checksum=_checksum(raw),
    )


def _as_date(source: str, key: str, value: Any) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise CatalogError(source, f"{key} must be an ISO date, got {value!r}") from None


# ---------------------------------------------------------------------------
# Persisting
# ---------------------------------------------------------------------------


def load_catalog(
    *,
    root: Path | None = None,
    dry_run: bool = False,
    allow_republish: bool = False,
) -> LoadReport:
    """Load every YAML definition into the database.

    :param dry_run: validate and report without writing. What CI runs.
    :param allow_republish: permit overwriting a published version whose content
        has changed. Off by default, because a published version is meant to be
        immutable — the correct response to "this rule was wrong" is a new version
        with a new effective window, so that the calendar generated last month
        can still be explained.
    """
    report = LoadReport()
    documents: list[DefinitionDocument] = []

    for path, raw in iter_documents(root):
        try:
            documents.append(parse_document(path, raw))
        except CatalogError as exc:
            report.errors.append(str(exc))

    _check_duplicate_codes(documents, report)
    if not report.ok:
        return report

    authorities = {a.code: a for a in Authority.objects.all()}

    for document in documents:
        try:
            if dry_run:
                report.unchanged.append(document.code)
                continue
            with transaction.atomic():
                outcome = _persist(document, authorities, allow_republish=allow_republish)
            getattr(report, outcome).append(document.code)
        except CatalogError as exc:
            report.errors.append(str(exc))

    return report


def _check_duplicate_codes(documents: list[DefinitionDocument], report: LoadReport) -> None:
    """Two files claiming one (code, version) is a copy-paste error, always."""
    seen: dict[tuple[str, int], str] = {}
    for document in documents:
        key = (document.code, document.version)
        if key in seen:
            report.errors.append(
                f"{document.source_path}: {document.code} v{document.version} is also "
                f"defined in {seen[key]}"
            )
        seen[key] = document.source_path


def _persist(
    document: DefinitionDocument,
    authorities: Mapping[str, Authority],
    *,
    allow_republish: bool,
) -> str:
    """Write one document. Returns ``"created"``, ``"updated"`` or ``"unchanged"``."""
    authority = authorities.get(document.authority_code) if document.authority_code else None
    if document.authority_code and authority is None:
        raise CatalogError(
            document.source_path,
            f"unknown authority {document.authority_code!r} — add it to the jurisdiction pack first",
        )

    definition, _ = ComplianceDefinition.objects.update_or_create(
        code=document.code,
        defaults={
            "country": document.country,
            "category": document.category,
            "family": document.family,
            "trigger_kind": document.trigger_kind,
            "tags": document.tags,
            "sector_tags": document.sector_tags,
            "authority": authority,
            "is_active": True,
        },
    )

    existing = DefinitionVersion.objects.filter(
        definition=definition, version=document.version
    ).first()

    payload = _version_fields(document)

    if existing is None:
        DefinitionVersion.objects.create(definition=definition, version=document.version, **payload)
        return "created"

    if existing.source_checksum == document.checksum:
        return "unchanged"

    if existing.is_published and not allow_republish:
        changed = [
            name
            for name in _PAYLOAD_FIELDS
            if getattr(existing, name) != payload.get(name, getattr(existing, name))
        ]
        if changed:
            raise CatalogError(
                document.source_path,
                f"{document.code} v{document.version} is published and its rule payload "
                f"changed ({', '.join(changed)}). Publish a new version with a new "
                f"effective window instead — a published version has to stay explainable. "
                f"Pass --allow-republish only to correct an editorial field.",
            )

    for name, value in payload.items():
        setattr(existing, name, value)
    existing.save()
    return "updated"


def _version_fields(document: DefinitionDocument) -> dict[str, Any]:
    from django.utils import timezone

    return {
        "status": document.status,
        "title": document.title,
        "plain_language_summary": document.plain_language_summary,
        "statutory_reference": document.statutory_reference,
        "filing_portal_url": document.filing_portal_url,
        "periodicity": document.periodicity,
        "period_anchor": document.period_anchor,
        "jurisdictions": document.jurisdictions,
        "applicability_rule": document.applicability_rule,
        "facts_used": document.facts,
        "possible_entity_types": document.possible_entity_types,
        "probe_signature": PROBE_SIGNATURE,
        "instance_scope": document.instance_scope,
        "scope_selector": document.scope_selector,
        "trigger_rule": document.trigger_rule,
        "due_rule": document.due_rule,
        "evidence_requirements": document.evidence_requirements,
        "default_owner_role": document.default_owner_role,
        "penalty_summary": document.penalty_summary,
        "effective_from": document.effective_from,
        "effective_to": document.effective_to,
        "published_at": timezone.now() if document.status == PublicationStatus.PUBLISHED else None,
        "reviewed_by": document.reviewed_by,
        "reviewed_at": document.reviewed_at,
        "confidence": document.confidence,
        "source_path": document.source_path,
        "source_checksum": document.checksum,
    }


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


def load_extensions(root: Path | None = None) -> LoadReport:
    """Load government notifications from ``catalog/extensions/*.yaml``.

    Kept in the same repository as the definitions and reviewed the same way. An
    extension is a factual claim about a notification — it needs a reference, a
    date, and somebody's name on the pull request.
    """
    report = LoadReport()
    base = root or (CATALOG_ROOT / "extensions")
    if not base.exists():
        return report

    for path in sorted(base.rglob("*.yaml")):
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        for entry in raw or []:
            try:
                reference = str(entry["notification_reference"])
                _, created = GovernmentExtension.objects.update_or_create(
                    definition_code=str(entry["definition_code"]),
                    period_key=str(entry.get("period_key", "")),
                    notification_reference=reference,
                    defaults={
                        "kind": str(entry["kind"]),
                        "notification_url": str(entry.get("notification_url", "")),
                        "new_due_date": _as_date(str(path), "new_due_date", entry["new_due_date"])
                        if entry.get("new_due_date")
                        else None,
                        "jurisdictions": [str(j) for j in (entry.get("jurisdictions") or [])],
                        "scope_rule": dict(entry.get("scope_rule") or {}),
                        "relief": dict(entry.get("relief") or {}),
                        "published_at": _as_date(str(path), "published_at", entry["published_at"]),
                    },
                )
                (report.created if created else report.updated).append(reference)
            except (KeyError, CatalogError) as exc:
                report.errors.append(f"{path}: {exc}")

    return report
