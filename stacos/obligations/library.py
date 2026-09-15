"""
The compliance library: the whole catalog, read against one entity.

The calendar shows what has already been computed, period by period. This
shows the shelf it was computed *from* — every published definition, whether
or not anything has ever been materialised for it — and lets a person move one
definition between three states: added, removed, or left for the engine to
decide.

``ObligationSuppression`` and ``ObligationInclusion`` already carry exactly
this decision (see their docstrings in ``models.py``) and the planner already
reads them on every rebuild. This module is the first place that writes them
at the *definition* level, directly, rather than as a side effect of
dismissing one instance or adopting a pack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from django.db import transaction
from django.utils import timezone

from stacos.catalog.snapshots import build_catalog, definitions_for_codes
from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.engine.types import DefinitionSnapshot, InstanceScope
from stacos.obligations.models import (
    MaterialisationRun,
    ObligationEvent,
    ObligationInclusion,
    ObligationInstance,
    ObligationSuppression,
)
from stacos.obligations.profile import build_profile_view
from stacos.obligations.queries import annotate_status, live
from stacos.obligations.services import materialise
from stacos.tenancy.models import Entity

__all__ = [
    "LibraryError",
    "LibraryRow",
    "build_library",
    "force_add_definition",
    "remove_definition",
    "restore_definition",
]


class LibraryError(Exception):
    """A library action that cannot be made, with a message fit to show a user."""


@dataclass(frozen=True, slots=True)
class LibraryRow:
    code: str
    title: str
    family: str
    category: str
    #: "added" | "removed" | "not_added"
    state: str
    #: Non-empty only for a "not_added" row a force-add would refuse.
    blocked_reason: str = ""
    #: The one representative occurrence, "added" rows only. See
    #: ``_representative`` for how it is picked.
    occurrence: ObligationInstance | None = None


def build_library(entity: Entity, *, as_of: date) -> tuple[LibraryRow, ...]:
    """Every published definition for this entity's country, each carrying its
    current state and — for "added" rows — one representative occurrence.

    A fixed number of queries regardless of how large the catalog grows: one to
    build the profile (itself a handful of queries, independent of catalog
    size), one cached call for the catalog, one for live suppressions, one for
    live instances, and one for display metadata. Nothing here scales with the
    number of definitions.
    """
    profile = build_profile_view(entity, as_of=as_of)
    catalog = build_catalog(country=entity.country, jurisdictions=profile.jurisdictions)

    removed_codes = _removed_codes(entity)
    representative = _representative_occurrences(entity, as_of=as_of)
    families = definitions_for_codes(definition.code for definition in catalog)

    registration_types = {ref.label for ref in profile.registrations}
    premises_types = {ref.label for ref in profile.premises}

    rows: list[LibraryRow] = []
    for definition in catalog:
        code = definition.code
        family = families[code].family if code in families else ""

        if code in removed_codes:
            rows.append(
                LibraryRow(
                    code=code,
                    title=definition.title,
                    family=family,
                    category=definition.category,
                    state="removed",
                )
            )
            continue

        occurrence = representative.get(code)
        if occurrence is not None:
            rows.append(
                LibraryRow(
                    code=code,
                    title=definition.title,
                    family=family,
                    category=definition.category,
                    state="added",
                    occurrence=occurrence,
                )
            )
            continue

        rows.append(
            LibraryRow(
                code=code,
                title=definition.title,
                family=family,
                category=definition.category,
                state="not_added",
                blocked_reason=_blocked_reason(
                    definition, registration_types=registration_types, premises_types=premises_types
                ),
            )
        )

    return tuple(rows)


def _removed_codes(entity: Entity) -> frozenset[str]:
    """Definition codes with a **blanket** suppression — every scope, every
    period, ruled out for this entity. Matches
    ``stacos.obligations.services._blanket_suppressions`` exactly, so a
    definition removed here is the same one the planner will not resurrect.
    """
    return frozenset(
        row.definition_code
        for row in ObligationSuppression.objects.filter(entity=entity, revoked_at__isnull=True)
        if row.is_live and not row.scope_ref and not row.period_key
    )


def _has_blanket_suppression(entity: Entity, code: str) -> bool:
    return any(
        row.is_live
        for row in ObligationSuppression.objects.filter(
            entity=entity,
            definition_code=code,
            scope_ref="",
            period_key="",
            revoked_at__isnull=True,
        )
    )


def _representative_occurrences(entity: Entity, *, as_of: date) -> dict[str, ObligationInstance]:
    """One row per definition code that currently has a live instance.

    Grouped in Python from a single query — bounded by how many instances this
    entity actually has, never by the size of the catalog.
    """
    grouped: dict[str, list[ObligationInstance]] = {}
    for instance in annotate_status(live().filter(entity=entity), as_of=as_of):
        grouped.setdefault(instance.definition_code, []).append(instance)

    return {code: _representative(rows) for code, rows in grouped.items()}


def _representative(rows: list[ObligationInstance]) -> ObligationInstance:
    """Pick the one occurrence that best represents a definition's current
    state for this entity.

    Anything still open beats anything already closed. Among open occurrences,
    the soonest due date wins. Among closed occurrences, the most recently
    completed one wins — never a stale date from far in the past, because every
    fallback below is a real recorded timestamp, not a default.
    """
    open_rows = [row for row in rows if row.is_open]
    if open_rows:
        return min(open_rows, key=lambda row: (row.due_date is None, row.due_date or date.max))

    return max(rows, key=_completed_at)


def _completed_at(row: ObligationInstance) -> datetime:
    if row.closed_at is not None:
        return row.closed_at
    if row.filed_on is not None:
        return timezone.make_aware(datetime.combine(row.filed_on, datetime.min.time()))
    return row.state_changed_at


def _blocked_reason(
    definition: DefinitionSnapshot, *, registration_types: set[str], premises_types: set[str]
) -> str:
    """Why force-adding this definition would be refused, or "" if it would not be.

    A cheap hint for the browsing screen. The authoritative check is
    :func:`force_add_definition` itself, which only ever trusts what the engine
    actually manages to materialise.
    """
    if definition.instance_scope == InstanceScope.REGISTRATION:
        needed = definition.scope_selector.get("registration_type")
        if needed and needed not in registration_types:
            return f"Needs a {needed} registration on file first."
    elif definition.instance_scope == InstanceScope.PREMISES:
        needed = definition.scope_selector.get("premises_type")
        if needed and needed not in premises_types:
            return f"Needs a {needed} premises on file first."
    return ""


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _definition_title(code: str) -> str:
    lookup = definitions_for_codes([code])
    definition = lookup.get(code)
    return code if definition is None else str(definition)


@transaction.atomic
def remove_definition(entity: Entity, code: str, *, actor, reason: str) -> None:
    """Rule a definition out for this entity, for good, until reversed.

    Blocks every future rebuild (a blanket suppression, the same shape the
    planner already reads), cancels any earlier force-add so a stale one
    cannot quietly keep taking effect once this is later reversed, and
    dismisses every open instance so nothing lingers on the calendar.
    """
    now = timezone.now()

    if _has_blanket_suppression(entity, code):
        raise LibraryError("This definition is already removed.")
    if not live().filter(entity=entity, definition_code=code).exists():
        raise LibraryError("This definition is not currently added.")

    ObligationSuppression.objects.create(
        tenant=entity.tenant,
        entity=entity,
        definition_code=code,
        scope_ref="",
        period_key="",
        kind=ObligationSuppression.Kind.NOT_APPLICABLE,
        reason=reason,
        created_by=actor,
    )

    ObligationInclusion.objects.filter(
        entity=entity, definition_code=code, revoked_at__isnull=True
    ).update(revoked_at=now)

    dismissed = 0
    for instance in live().filter(entity=entity, definition_code=code):
        if not instance.is_open:
            continue
        instance.supersede(reason=reason)
        ObligationEvent.objects.create(
            tenant=entity.tenant,
            entity=entity,
            obligation=instance,
            kind=ObligationEvent.Kind.SUPERSEDED,
            to_state=instance.state,
            actor=actor,
            actor_label=str(actor),
            note=reason,
        )
        dismissed += 1

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        entity_id=entity.pk,
        object_type="ComplianceDefinition",
        object_id=code,
        object_label=_definition_title(code),
        before={"state": "added"},
        after={"state": "removed", "dismissed_instances": dismissed},
        context={"reason": reason},
    )


@transaction.atomic
def force_add_definition(entity: Entity, code: str, *, actor, reason: str, as_of: date) -> None:
    """Add a definition the engine has not selected, with a real due date.

    The inclusion tells the planner to force the applicability verdict, but the
    planner still has to fan the definition out to a real registration or
    premises when the definition is scoped to one. If it cannot — because the
    entity has none on file — nothing is created, and this refuses rather than
    leaving a silent, empty override behind.
    """
    if _has_blanket_suppression(entity, code):
        raise LibraryError("This definition is removed for this entity — restore it first.")
    if live().filter(entity=entity, definition_code=code).exists():
        raise LibraryError("This definition already applies to this entity.")

    ObligationInclusion.objects.create(
        tenant=entity.tenant,
        entity=entity,
        definition_code=code,
        source=ObligationInclusion.Source.USER,
        reason=reason,
        added_by=actor,
    )

    materialise(entity, as_of=as_of, trigger=MaterialisationRun.Trigger.MANUAL, actor=actor)

    created = live().filter(entity=entity, definition_code=code).exists()
    if not created:
        lookup = definitions_for_codes([code])
        definition = lookup.get(code)
        version = definition.version_for(as_of) if definition is not None else None
        selector = version.scope_selector if version is not None else {}
        needed = selector.get("registration_type") or selector.get("premises_type")
        message = (
            f"Could not add — this needs a {needed} on file first."
            if needed
            else "Could not add — the engine could not compute a due date for this entity."
        )
        raise LibraryError(message)

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        entity_id=entity.pk,
        object_type="ComplianceDefinition",
        object_id=code,
        object_label=_definition_title(code),
        before={"state": "not_added"},
        after={"state": "added"},
        context={"reason": reason},
    )


@transaction.atomic
def restore_definition(entity: Entity, code: str, *, actor, as_of: date) -> None:
    """Undo an earlier removal. No fresh reason — the removal was already
    justified, and this simply reverses it and clears the block on a rebuild.
    """
    now = timezone.now()
    updated = ObligationSuppression.objects.filter(
        entity=entity,
        definition_code=code,
        scope_ref="",
        period_key="",
        revoked_at__isnull=True,
    ).update(revoked_at=now)

    if not updated:
        raise LibraryError("This definition is not currently removed.")

    materialise(entity, as_of=as_of, trigger=MaterialisationRun.Trigger.MANUAL, actor=actor)

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        entity_id=entity.pk,
        object_type="ComplianceDefinition",
        object_id=code,
        object_label=_definition_title(code),
        before={"state": "removed"},
        after={"state": "restored"},
    )
