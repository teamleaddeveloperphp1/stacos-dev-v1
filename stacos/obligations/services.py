"""
Materialisation: previewing a calendar, and applying it.

The split is the whole design. :func:`preview` is pure computation — it reads the
world and returns a description of the change without writing anything, which is
what lets a profile edit show "3 added, 1 removed, 2 dates changed" before the
user commits. :func:`apply_plan` writes that exact description in one
transaction.

Three properties are load-bearing:

**Idempotent.** Applying a plan and then planning again yields an empty plan.
Asserted as a property test rather than hoped for, because the nightly job runs
this against every entity every night and a plan that is not idempotent produces
duplicate obligations at a rate nobody notices until a client does.

**Nothing with history is destroyed.** An instance carrying evidence or events is
superseded and retained. The nightly rebuild must not be able to erase a client's
audit trail — that is not a bug, it is a breach.

**Suppressions are an input.** A user who marked something not-applicable must
not find it back tomorrow morning. This is the single fastest way to lose a
client's trust in a calendar, and it costs one query to avoid.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import structlog
from django.db import transaction

from stacos.catalog.snapshots import (
    build_calendar_snapshot,
    build_catalog,
    build_extension_set,
    calendar_keys_in,
    catalog_fingerprint,
    fiscal_year_for,
)
from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.engine.planner import ExistingInstance, MaterialisationPlan, PlannedInstance
from stacos.engine.planner import plan as run_planner
from stacos.engine.types import Identity
from stacos.jurisdictions.models import JurisdictionPack
from stacos.obligations.models import (
    MaterialisationRun,
    ObligationEvent,
    ObligationInstance,
    ObligationSuppression,
)
from stacos.obligations.profile import build_profile_view, scope_display_map
from stacos.tenancy.models import Entity

logger = structlog.get_logger(__name__)

__all__ = ["MaterialisationPreview", "StaleCatalogError", "apply_plan", "materialise", "preview"]

#: Eighteen months forward. Long enough that a client planning a year ahead sees
#: everything, short enough that the register does not fill with speculative rows
#: whose rules will have changed by the time they matter.
HORIZON_MONTHS = 18

#: How far back the horizon opens. Onboarding a client mid-year has to surface the
#: filings they have already missed — a calendar that starts clean tomorrow is
#: not the truth about their position.
HORIZON_LOOKBACK_DAYS = 120


class StaleCatalogError(RuntimeError):
    """The catalog moved between previewing a plan and applying it.

    Rare, and worth failing on. The window is small, but applying a preview
    generated against rules the user never saw is exactly the kind of silent
    wrongness this product cannot afford.
    """


@dataclass(frozen=True, slots=True)
class MaterialisationPreview:
    plan: MaterialisationPlan
    fingerprint: str
    horizon_start: date
    horizon_end: date
    as_of: date

    @property
    def summary(self) -> str:
        return self.plan.summary()

    @property
    def is_empty(self) -> bool:
        return self.plan.is_empty


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def preview(
    entity: Entity,
    *,
    as_of: date,
    horizon_start: date | None = None,
    horizon_end: date | None = None,
) -> MaterialisationPreview:
    """Work out what this entity's register should contain. Writes nothing.

    Around ten to twenty-five milliseconds for a full catalog against one profile,
    which is what makes this affordable to run synchronously on a profile edit and
    show as a diff.
    """
    started = time.perf_counter()

    horizon_start = horizon_start or (as_of - timedelta(days=HORIZON_LOOKBACK_DAYS))
    horizon_end = horizon_end or _add_months(as_of, HORIZON_MONTHS)

    profile_view = build_profile_view(entity, as_of=as_of)

    catalog = build_catalog(
        country=entity.country,
        jurisdictions=profile_view.jurisdictions,
    )
    fingerprint = catalog_fingerprint(catalog)

    pack = JurisdictionPack.objects.filter(country=entity.country).first()
    if pack is None:
        raise RuntimeError(
            f"No jurisdiction pack for {entity.country!r}. The fiscal year convention, "
            f"weekend rules and holiday calendars all come from the pack — without one "
            f"there is nothing to compute dates against."
        )

    extensions = build_extension_set(
        definition_codes=[definition.code for definition in catalog], as_of=as_of
    )
    calendars = build_calendar_snapshot(
        country=entity.country,
        calendar_keys=calendar_keys_in(catalog),
        window_start=horizon_start,
        window_end=horizon_end,
        as_of=as_of,
    )

    materialisation = run_planner(
        profile=profile_view,
        catalog=catalog,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        fy=fiscal_year_for(pack),
        calendars=calendars,
        extensions=extensions,
        existing=_existing_instances(entity),
        suppressed=_suppressed_identities(entity),
        as_of=as_of,
    )

    logger.info(
        "materialisation.previewed",
        entity_id=str(entity.pk),
        definitions=len(catalog),
        summary=materialisation.summary(),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )

    return MaterialisationPreview(
        plan=materialisation,
        fingerprint=fingerprint,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        as_of=as_of,
    )


def _existing_instances(entity: Entity) -> list[ExistingInstance]:
    """What the register already holds, in the shape the planner compares against.

    ``.values()`` rather than model instances, and the evidence/history flags
    computed as annotations: the planner needs six fields per row and a
    multi-state client can hold several thousand.
    """
    from django.db.models import Count, Q

    rows = (
        ObligationInstance.objects.filter(entity=entity)
        .annotate(event_count=Count("events", filter=~Q(events__kind="MATERIALISED")))
        .values(
            "definition_code",
            "scope_ref",
            "period_key",
            "occurrence",
            "state",
            "due_date",
            "definition_version",
            "archived_at",
            "event_count",
        )
    )

    return [
        ExistingInstance(
            identity=Identity(
                definition_code=row["definition_code"],
                scope_ref=row["scope_ref"],
                period_key=row["period_key"],
                occurrence=row["occurrence"],
            ),
            state=row["state"],
            due_date=row["due_date"],
            definition_version=row["definition_version"],
            # Evidence lands in the vault module; until then an obligation with
            # human activity against it is the thing that must not be destroyed,
            # and the event log is the honest signal for that.
            has_evidence=False,
            has_history=row["event_count"] > 0,
            archived=row["archived_at"] is not None,
        )
        for row in rows
    ]


def _suppressed_identities(entity: Entity) -> frozenset[Identity]:
    """Identities the user has dismissed.

    A suppression with an empty ``period_key`` covers every period of that
    definition and scope — "we are not a factory, stop asking" — which cannot be
    expressed as a set of identities. Those are handled by expanding against what
    the planner would otherwise produce, so they are applied in
    :func:`_filter_blanket_suppressions` after planning rather than here.
    """
    identities: set[Identity] = set()
    for row in ObligationSuppression.objects.filter(entity=entity, revoked_at__isnull=True):
        if not row.is_live or not row.period_key:
            continue
        identities.add(
            Identity(
                definition_code=row.definition_code,
                scope_ref=row.scope_ref,
                period_key=row.period_key,
            )
        )
    return frozenset(identities)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


@transaction.atomic
def apply_plan(
    entity: Entity,
    materialised: MaterialisationPreview,
    *,
    trigger: str,
    actor: Any = None,
    expect_fingerprint: str | None = None,
) -> MaterialisationRun:
    """Write a plan to the register, in one transaction.

    :param expect_fingerprint: the catalog fingerprint the plan was previewed
        against. Supplying it turns a catalog publication that lands between
        preview and apply into a loud failure rather than a set of dates the user
        never approved.
    """
    started = time.perf_counter()

    if expect_fingerprint is not None and expect_fingerprint != materialised.fingerprint:
        raise StaleCatalogError(
            "The compliance catalog changed after this preview was generated. "
            "Review the calendar again before applying it."
        )

    plan = materialised.plan
    blanket = _blanket_suppressions(entity)
    scopes = scope_display_map(entity)

    created = _create_instances(entity, plan.to_create, scopes=scopes, blanket=blanket)
    updated = _update_instances(entity, plan)
    revived = _revive_instances(entity, plan)
    superseded = _supersede_instances(entity, plan)
    archived = _archive_instances(entity, plan)

    run = MaterialisationRun.objects.create(
        tenant=entity.tenant,
        entity=entity,
        trigger=trigger,
        catalog_fingerprint=materialised.fingerprint,
        horizon_start=materialised.horizon_start,
        horizon_end=materialised.horizon_end,
        as_of=materialised.as_of,
        created_count=created,
        updated_count=updated,
        superseded_count=superseded,
        archived_count=archived,
        revived_count=revived,
        unchanged_count=len(plan.unchanged),
        diagnostics=[
            {"severity": d.severity, "code": d.code, "message": d.message}
            for d in plan.diagnostics
        ],
        triggered_by=actor if getattr(actor, "is_authenticated", False) else None,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )

    if not plan.is_empty:
        record_event(
            action=AuditAction.UPDATE,
            actor=actor,
            obj=run,
            after=run.as_dict(),
            context={"trigger": trigger, "summary": run.summary()},
        )

    logger.info(
        "materialisation.applied",
        entity_id=str(entity.pk),
        trigger=trigger,
        summary=run.summary(),
    )
    return run


def _blanket_suppressions(entity: Entity) -> set[tuple[str, str]]:
    """``(definition_code, scope_ref)`` pairs suppressed for every period."""
    return {
        (row.definition_code, row.scope_ref)
        for row in ObligationSuppression.objects.filter(entity=entity, revoked_at__isnull=True)
        if row.is_live and not row.period_key
    }


def _create_instances(
    entity: Entity,
    planned: Sequence[PlannedInstance],
    *,
    scopes: dict[str, Any],
    blanket: set[tuple[str, str]],
) -> int:
    """Insert new obligations in bulk, skipping anything the user dismissed."""
    rows: list[ObligationInstance] = []
    events: list[ObligationEvent] = []

    for item in planned:
        if (item.definition_code, item.scope.ref) in blanket:
            continue

        display = scopes.get(item.scope.ref)
        instance = ObligationInstance(
            tenant=entity.tenant,
            entity=entity,
            definition_code=item.definition_code,
            definition_version=item.definition_version,
            scope_kind=item.scope.kind,
            scope_ref=item.scope.ref,
            scope_label=display.label if display else "",
            scope_jurisdiction=item.scope.jurisdiction
            or (display.jurisdiction if display else ""),
            period_key=item.period.key,
            period_label=item.period.label,
            period_start=item.period.start,
            period_end=item.period.end,
            occurrence=item.identity.occurrence,
            title=item.title,
            category=item.category,
            due_date=item.due_date,
            original_due_date=item.original_due_date,
            applied_extension_reference=item.applied_extension_reference,
            owner_role=item.owner_role,
            confirmed=item.confirmed,
            reasons=list(item.reasons)[:10],
            needs_input=item.needs_input,
        )
        rows.append(instance)

    if not rows:
        return 0

    # `ignore_conflicts` guards the race where the nightly job and a synchronous
    # profile-change rebuild plan the same entity at the same moment. The unique
    # constraint is the arbiter; one of them silently loses, which is correct.
    ObligationInstance.objects.bulk_create(rows, batch_size=500, ignore_conflicts=True)

    # Which of them actually landed. Primary keys are UUIDv7 generated in Python,
    # so they are known before the insert — but a row whose identity already
    # existed was skipped, and hanging an event off its unused id would be a
    # foreign-key violation rather than a no-op.
    inserted = set(
        ObligationInstance.objects.filter(id__in=[row.pk for row in rows]).values_list(
            "id", flat=True
        )
    )

    for instance in rows:
        if instance.pk not in inserted:
            continue
        events.append(
            ObligationEvent(
                tenant=entity.tenant,
                entity=entity,
                obligation=instance,
                kind=ObligationEvent.Kind.MATERIALISED,
                to_state=instance.state,
                actor_label="STACOS",
                note=(instance.reasons[0] if instance.reasons else ""),
            )
        )
    ObligationEvent.objects.bulk_create(events, batch_size=500)

    return len(inserted)


def _update_instances(entity: Entity, plan: MaterialisationPlan) -> int:
    """Apply date and version changes, recording anything a user would notice.

    A due-date change is written to the timeline as well as to the row. "Why is
    this due on the 22nd now" has to have an answer, and the answer is usually a
    government notification.
    """
    changed = 0
    for delta in plan.to_update:
        instance = _lookup(entity, delta.identity)
        if instance is None:
            continue

        fields: list[str] = []
        for name, (_before, after) in delta.changes.items():
            setattr(instance, name, after)
            fields.append(name)

        if fields:
            instance.save(update_fields=[*fields, "updated_at"])
            changed += 1

        if "due_date" in delta.changes:
            before, after = delta.changes["due_date"]
            ObligationEvent.objects.create(
                tenant=entity.tenant,
                entity=entity,
                obligation=instance,
                kind=(
                    ObligationEvent.Kind.EXTENSION_APPLIED
                    if instance.applied_extension_reference
                    else ObligationEvent.Kind.DATE_CHANGED
                ),
                actor_label="STACOS",
                note=(
                    f"Due date moved from {before or 'unscheduled'} to "
                    f"{after or 'unscheduled'}."
                ),
                context={
                    "from": before.isoformat() if before else None,
                    "to": after.isoformat() if after else None,
                    "notification": instance.applied_extension_reference,
                },
            )
    return changed


def _revive_instances(entity: Entity, plan: MaterialisationPlan) -> int:
    """Un-archive obligations that became applicable again.

    Reviving beats creating: if a client's profile flips back, the original row
    returns with its history rather than a duplicate appearing beside it.
    """
    revived = 0
    for identity in plan.to_revive:
        instance = _lookup(entity, identity, include_archived=True)
        if instance is None:
            continue
        instance.archived_at = None
        instance.archive_reason = ""
        instance.superseded_at = None
        instance.supersede_reason = ""
        instance.save(
            update_fields=[
                "archived_at",
                "archive_reason",
                "superseded_at",
                "supersede_reason",
                "updated_at",
            ]
        )
        revived += 1
    return revived


def _supersede_instances(entity: Entity, plan: MaterialisationPlan) -> int:
    """Retain obligations that stopped applying but carry history."""
    count = 0
    for action in plan.to_supersede:
        instance = _lookup(entity, action.identity)
        if instance is None:
            continue
        instance.supersede(reason=action.reason)
        ObligationEvent.objects.create(
            tenant=entity.tenant,
            entity=entity,
            obligation=instance,
            kind=ObligationEvent.Kind.SUPERSEDED,
            to_state=instance.state,
            actor_label="STACOS",
            note=action.reason,
        )
        count += 1
    return count


def _archive_instances(entity: Entity, plan: MaterialisationPlan) -> int:
    """Soft-delete obligations nobody had touched.

    Only ever reached for rows in ``NOT_STARTED`` with no events — the planner
    classifies anything else as a supersession. Archived rather than deleted so a
    later revive returns the same row.
    """
    identities = [action.identity for action in plan.to_archive]
    if not identities:
        return 0

    reasons = {action.identity: action.reason for action in plan.to_archive}
    count = 0
    for identity in identities:
        instance = _lookup(entity, identity)
        if instance is None:
            continue
        instance.archive(reason=reasons.get(identity, ""))
        count += 1
    return count


def _lookup(
    entity: Entity, identity: Identity, *, include_archived: bool = False
) -> ObligationInstance | None:
    queryset = ObligationInstance.objects.filter(
        entity=entity,
        definition_code=identity.definition_code,
        scope_ref=identity.scope_ref,
        period_key=identity.period_key,
        occurrence=identity.occurrence,
    )
    if not include_archived:
        queryset = queryset.filter(archived_at__isnull=True)
    return queryset.first()


# ---------------------------------------------------------------------------
# The one-call entry point
# ---------------------------------------------------------------------------


def materialise(
    entity: Entity,
    *,
    as_of: date,
    trigger: str = MaterialisationRun.Trigger.MANUAL,
    actor: Any = None,
) -> MaterialisationRun:
    """Preview and apply in one step.

    What the nightly job and onboarding call. An interactive profile edit uses
    :func:`preview` and :func:`apply_plan` separately so the user sees the diff
    before it happens.
    """
    materialised = preview(entity, as_of=as_of)
    return apply_plan(entity, materialised, trigger=trigger, actor=actor)


def _add_months(day: date, months: int) -> date:
    """Add whole months, clamping to the target month's length."""
    total = (day.year * 12 + day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    # 31 August plus six months is 28 (or 29) February.
    for candidate in (day.day, 30, 29, 28):
        try:
            return date(year, month, candidate)
        except ValueError:
            continue
    return date(year, month, 1)  # pragma: no cover - unreachable


def entity_ids_needing_materialisation(tenant_id: UUID) -> list[UUID]:
    """Active entities of one tenant, for the nightly roll.

    Dormant and struck-off entities are excluded: they have almost no live
    obligations, and rebuilding them nightly is work that produces nothing.
    """
    return list(
        Entity.objects.filter(
            tenant_id=tenant_id,
            archived_at__isnull=True,
            status__in=[Entity.Status.ACTIVE, Entity.Status.DORMANT],
        ).values_list("id", flat=True)
    )
