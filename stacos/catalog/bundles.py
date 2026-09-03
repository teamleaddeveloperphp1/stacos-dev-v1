"""
Loading compliance packs from ``catalog/bundles/``.

Deliberately a sibling of :mod:`stacos.catalog.loader` rather than part of it:
packs and definitions are authored by different people at different times, and a
broken pack must not be able to stop the definitions loading.

The directory is ``bundles/`` and not ``packs/`` because ``manage.py loadpack``
already globs ``catalog/packs/*.yaml`` looking for *countries*, and would try to
read a compliance pack as a jurisdiction.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from django.db import transaction

from stacos.catalog.loader import CATALOG_ROOT, CatalogError, LoadReport
from stacos.catalog.models import CompliancePack
from stacos.catalog.taxonomy import unknown_tags
from stacos.engine.rules import RuleError, facts_used, validate_rule
from stacos.jurisdictions.facts import REGISTRY

__all__ = ["BundleDocument", "iter_bundles", "load_bundles", "parse_bundle"]

BUNDLE_ROOT = CATALOG_ROOT / "bundles"


@dataclass(slots=True)
class BundleDocument:
    code: str
    country: str
    name: str
    definition_codes: list[str]
    summary: str = ""
    rationale: str = ""
    suggestion_rule: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    jurisdictions: list[str] = field(default_factory=list)
    source_path: str = ""
    checksum: str = ""

    @property
    def facts(self) -> list[str]:
        return sorted(facts_used(self.suggestion_rule))


def iter_bundles(root: Path | None = None) -> Iterator[tuple[Path, dict[str, Any]]]:
    base = root or BUNDLE_ROOT
    if not base.exists():
        return
    for path in sorted(base.rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise CatalogError(str(path), "top level must be a mapping")
        yield path, raw


def parse_bundle(path: Path, raw: Mapping[str, Any]) -> BundleDocument:
    source = str(path)

    def required(key: str) -> Any:
        if key not in raw:
            raise CatalogError(source, f"missing required key {key!r}")
        return raw[key]

    codes = [str(code) for code in (required("definitions") or [])]
    if not codes:
        raise CatalogError(
            source,
            "a pack with no definitions is a card that does nothing when pressed.",
        )
    if len(codes) != len(set(codes)):
        duplicates = sorted({code for code in codes if codes.count(code) > 1})
        raise CatalogError(source, f"the same definition is listed twice: {duplicates}")

    suggestion = dict(raw.get("suggest_for") or {})
    errors: list[RuleError] = validate_rule(suggestion, known_facts=REGISTRY.keys())
    if errors:
        raise CatalogError(source, "; ".join(str(error) for error in errors))

    tags = [str(tag) for tag in (raw.get("tags") or [])]
    bad = unknown_tags(tags)
    if bad:
        raise CatalogError(source, f"unknown tag(s) {bad}")

    canonical = yaml.safe_dump(dict(raw), sort_keys=True, default_flow_style=False)

    return BundleDocument(
        code=str(required("code")).strip(),
        country=str(required("country")),
        name=str(required("name")),
        definition_codes=codes,
        summary=str(raw.get("summary", "")).strip(),
        rationale=str(raw.get("rationale", "")).strip(),
        suggestion_rule=suggestion,
        tags=tags,
        jurisdictions=[str(j) for j in (raw.get("jurisdictions") or [])],
        source_path=str(path.relative_to(CATALOG_ROOT.parent))
        if path.is_relative_to(CATALOG_ROOT.parent)
        else source,
        checksum=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def check_bundles(documents: list[BundleDocument], known_definition_codes: set[str]) -> list[str]:
    """Problems that need the whole catalog to see. No database.

    The important one is a reference to a definition that does not exist. A pack
    is a promise that pressing one button adds a coherent set of obligations; a
    dangling code silently makes the set smaller than the card claims.
    """
    problems: list[str] = []
    seen: dict[str, str] = {}

    for document in documents:
        if document.code in seen:
            problems.append(
                f"{document.source_path}: {document.code} is also defined in {seen[document.code]}"
            )
        seen[document.code] = document.source_path

        unknown = [code for code in document.definition_codes if code not in known_definition_codes]
        if unknown:
            problems.append(
                f"{document.source_path}: {document.code} references definitions that do not "
                f"exist: {unknown}. The pack would quietly add fewer obligations than it "
                f"claims to."
            )
    return problems


def load_bundles(root: Path | None = None, *, dry_run: bool = False) -> LoadReport:
    report = LoadReport()
    documents: list[BundleDocument] = []

    for path, raw in iter_bundles(root):
        try:
            documents.append(parse_bundle(path, raw))
        except CatalogError as exc:
            report.errors.append(str(exc))

    if not report.ok:
        return report

    for document in documents:
        if dry_run:
            report.unchanged.append(document.code)
            continue
        with transaction.atomic():
            pack, created = CompliancePack.objects.update_or_create(
                code=document.code,
                defaults={
                    "country": document.country,
                    "name": document.name,
                    "summary": document.summary,
                    "rationale": document.rationale,
                    "definition_codes": document.definition_codes,
                    "suggestion_rule": document.suggestion_rule,
                    "facts_used": document.facts,
                    "tags": document.tags,
                    "jurisdictions": document.jurisdictions,
                    "is_active": True,
                    "source_path": document.source_path,
                    "source_checksum": document.checksum,
                },
            )
        if created:
            report.created.append(pack.code)
        else:
            report.updated.append(pack.code)

    return report
