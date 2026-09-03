"""
The boundary between the ORM and the pure engine.

Everything the engine reads is built here and handed over as frozen dataclasses.
Nothing downstream of this module touches a Django model, which is what lets a
whole calendar generation run in a unit test with no database — and what stops
the engine quietly acquiring a dependency on the ORM through a lazily-evaluated
attribute.

The snapshot is also the cache boundary. A catalog of ~140 definitions is
rebuilt from four queries and cached under a fingerprint; a materialisation plan
records the fingerprint it was generated against, so applying a stale preview
after someone published a catalog change fails loudly instead of writing dates
computed from rules that no longer exist.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import date

from django.core.cache import cache
from django.db.models import Q

from stacos.catalog.models import (
    ComplianceDefinition,
    DefinitionVersion,
    GovernmentExtension,
    PublicationStatus,
)
from stacos.engine.types import (
    CalendarSnapshot,
    DefinitionSnapshot,
    EvidenceRequirement,
    ExtensionKind,
    ExtensionRecord,
    ExtensionSet,
    FiscalYearConvention,
    InstanceScope,
    Periodicity,
)
from stacos.jurisdictions.models import Holiday, HolidayCalendar, JurisdictionPack, WeekendRule

__all__ = [
    "build_calendar_snapshot",
    "build_catalog",
    "build_extension_set",
    "catalog_fingerprint",
    "fiscal_year_for",
    "version_to_snapshot",
]

#: Long, because the catalog changes on publication rather than on a timer, and
#: publication invalidates explicitly. A short TTL here would mean rebuilding
#: reference data thousands of times a day for no reason.
CATALOG_CACHE_SECONDS = 3600
_CATALOG_CACHE_KEY = "catalog:snapshot:v1"


def version_to_snapshot(version: DefinitionVersion) -> DefinitionSnapshot:
    """Convert one stored version into the frozen type the engine reads."""
    return DefinitionSnapshot(
        code=version.definition.code,
        version=version.version,
        title=version.title,
        country=version.definition.country,
        periodicity=Periodicity(version.periodicity),
        due_rule=dict(version.due_rule or {}),
        applicability_rule=dict(version.applicability_rule or {}),
        jurisdictions=frozenset(version.jurisdictions or ()),
        category=version.definition.category,
        instance_scope=InstanceScope(version.instance_scope),
        scope_selector=dict(version.scope_selector or {}),
        trigger=dict(version.trigger_rule or {}),
        period_anchor=version.period_anchor,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
        default_owner_role=version.default_owner_role,
        evidence_requirements=tuple(
            EvidenceRequirement(
                key=str(item.get("key", "")),
                label=str(item.get("label", "")),
                kind=str(item.get("kind", "DOC")),
                mandatory_for_close=bool(item.get("mandatory_for_close", False)),
                file_types=tuple(item.get("file_types", ()) or ()),
            )
            for item in (version.evidence_requirements or [])
        ),
    )


def build_catalog(
    *,
    country: str,
    jurisdictions: Iterable[str] = (),
    as_of: date | None = None,
    use_cache: bool = True,
) -> tuple[DefinitionSnapshot, ...]:
    """Every published definition that could apply to an entity in ``country``.

    The SQL prefilter here is the first and cheapest of the three optimisations
    the design calls for: filtering by country and jurisdiction takes a catalog
    architected for two thousand definitions down to the hundred-odd that could
    conceivably match, before a single rule is evaluated.

    Jurisdiction filtering is deliberately inclusive — a definition with no
    jurisdictions is nationwide and must survive the filter. Excluding it here
    would silently drop every central-government obligation.
    """
    wanted = sorted(set(jurisdictions))
    cache_key = f"{_CATALOG_CACHE_KEY}:{country}:{','.join(wanted)}:{as_of or ''}"

    if use_cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return tuple(cached)

    queryset = (
        DefinitionVersion.objects.select_related("definition")
        .filter(
            status=PublicationStatus.PUBLISHED,
            definition__country=country,
            definition__is_active=True,
        )
        .order_by("definition__code", "-version")
    )

    if as_of is not None:
        queryset = queryset.filter(effective_from__lte=as_of).filter(
            Q(effective_to__isnull=True) | Q(effective_to__gte=as_of)
        )

    if wanted:
        # `len=0` catches the nationwide case. Postgres arrays overlap with `&&`,
        # which Django spells `__overlap`.
        queryset = queryset.filter(Q(jurisdictions__len=0) | Q(jurisdictions__overlap=wanted))

    snapshots = tuple(version_to_snapshot(version) for version in queryset)

    if use_cache:
        cache.set(cache_key, list(snapshots), CATALOG_CACHE_SECONDS)

    return snapshots


def catalog_fingerprint(catalog: Sequence[DefinitionSnapshot]) -> str:
    """A stable hash of the catalog a plan was computed against.

    Recorded on a materialisation plan and re-checked before it is applied. The
    window between previewing a diff and confirming it is small, but a catalog
    publication inside that window would otherwise write dates derived from rules
    the user never saw.
    """
    digest = hashlib.sha256()
    for definition in sorted(catalog, key=lambda d: d.code):
        digest.update(f"{definition.code}:{definition.version}\n".encode())
    return digest.hexdigest()[:32]


def invalidate_catalog_cache() -> None:
    """Drop cached catalogs. Called on publication, never on a schedule."""
    # `delete_pattern` is a redis-cache extension that the locmem backend used in
    # tests does not have, so the whole cache is cleared when it is unavailable.
    # Catalog publication is rare; the cost of a cold cache afterwards is not
    # worth a bespoke key registry.
    deleter = getattr(cache, "delete_pattern", None)
    if callable(deleter):
        deleter(f"{_CATALOG_CACHE_KEY}*")
    else:  # pragma: no cover - depends on the configured cache backend
        cache.clear()


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


def build_extension_set(
    *,
    definition_codes: Iterable[str] = (),
    as_of: date | None = None,
) -> ExtensionSet:
    """Published notifications, indexed for date resolution.

    ``WAIVER`` and ``AMNESTY`` records are included even though they do not move
    dates: the engine still needs them so the instance can display "late fee
    waived by Notification 07/2026" beside an unchanged date. Filtering them out
    here is how a client ends up believing they owe a penalty they do not.
    """
    queryset = GovernmentExtension.objects.filter(superseded_at__isnull=True)

    codes = sorted(set(definition_codes))
    if codes:
        queryset = queryset.filter(definition_code__in=codes)
    if as_of is not None:
        queryset = queryset.filter(published_at__lte=as_of)

    return ExtensionSet(
        records=tuple(
            ExtensionRecord(
                definition_code=row.definition_code,
                kind=ExtensionKind(row.kind),
                notification_reference=row.notification_reference,
                period_key=row.period_key,
                new_due_date=row.new_due_date,
                jurisdictions=frozenset(row.jurisdictions or ()),
                relief=dict(row.relief or {}),
                published_at=row.published_at,
            )
            for row in queryset
        )
    )


# ---------------------------------------------------------------------------
# Calendars and fiscal years
# ---------------------------------------------------------------------------


def fiscal_year_for(pack: JurisdictionPack) -> FiscalYearConvention:
    """The fiscal year convention, read from the pack rather than assumed.

    Nothing in the engine knows that India starts in April. This one function is
    where that fact enters, and it comes from a database row.
    """
    return FiscalYearConvention(
        start_month=pack.fy_start_month,
        start_day=pack.fy_start_day,
        label_template=pack.fy_label_template,
    )


def build_calendar_snapshot(
    *,
    country: str,
    calendar_keys: Iterable[str] = (),
    window_start: date | None = None,
    window_end: date | None = None,
    as_of: date | None = None,
) -> CalendarSnapshot:
    """Holidays and weekend days, resolved so the engine can read them synchronously.

    Weekend rules are effective-dated and resolved against ``as_of`` rather than
    read as a constant — the UAE moved its public-sector weekend in 2022, and a
    constant produces wrong dates for every date before the change. India happens
    not to have moved, which is exactly why hardcoding it would go unnoticed.
    """
    wanted = sorted(set(calendar_keys))

    holidays: dict[str, frozenset[date]] = {}
    if wanted:
        calendars = HolidayCalendar.objects.filter(key__in=wanted)
        holiday_rows = Holiday.objects.filter(calendar__in=calendars).select_related("calendar")
        if window_start is not None:
            holiday_rows = holiday_rows.filter(date__gte=window_start)
        if window_end is not None:
            holiday_rows = holiday_rows.filter(date__lte=window_end)

        collected: dict[str, set[date]] = {key: set() for key in wanted}
        for row in holiday_rows:
            collected.setdefault(row.calendar.key, set()).add(row.date)
        holidays = {key: frozenset(days) for key, days in collected.items()}

    weekend_days: dict[str, frozenset[int]] = {}
    pack = JurisdictionPack.objects.filter(country=country).first()
    if pack is not None:
        rules = WeekendRule.objects.filter(pack=pack)
        if as_of is not None:
            rules = rules.filter(valid_from__lte=as_of).filter(
                Q(valid_to__isnull=True) | Q(valid_to__gte=as_of)
            )
        rule = rules.order_by("-valid_from").first()
        if rule is not None:
            weekend_days[country] = frozenset(int(day) for day in rule.weekend_days or ())

    return CalendarSnapshot(holidays=holidays, weekend_days=weekend_days)


def calendar_keys_in(catalog: Sequence[DefinitionSnapshot]) -> frozenset[str]:
    """Every holiday calendar the catalog's due rules actually reference.

    Loading only these keeps the snapshot proportional to what is used rather
    than to how many state calendars the platform happens to hold.
    """
    keys: set[str] = set()
    for definition in catalog:
        for key in definition.due_rule.get("calendars", ()) or ():
            keys.add(str(key))
    return frozenset(keys)


def definitions_for_codes(codes: Iterable[str]) -> dict[str, ComplianceDefinition]:
    """Definitions by code, for enriching a register listing in one query."""
    return {
        definition.code: definition
        for definition in ComplianceDefinition.objects.filter(code__in=sorted(set(codes)))
    }
