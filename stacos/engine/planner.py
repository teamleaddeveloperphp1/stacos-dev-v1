"""
Materialisation: turning a catalog and an entity profile into a calendar.

``plan()`` is a pure function. It reads the world and returns a description of
the change; nothing here writes anything. The Django side applies the plan in one
transaction, which means the hard part — deciding what should exist — is testable
without a database and reviewable as a diff before it takes effect.

Two invariants matter more than anything else here:

* **Idempotent.** Planning against the state a plan produces yields an empty
  plan. Asserted as a property test, not hoped for.
* **Nothing with history is destroyed.** An instance that carries evidence, or
  that someone has worked on, is superseded and retained — never deleted. The
  nightly job must not be able to erase a client's audit trail.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from stacos.engine.dates import generate_periods, resolve_due_date, static_max_lag_days
from stacos.engine.rules import V, evaluate
from stacos.engine.types import (
    CalendarSnapshot,
    DefinitionSnapshot,
    Diagnostic,
    ExtensionSet,
    FiscalYearConvention,
    Identity,
    InstanceScope,
    Period,
    ScopeRef,
    Severity,
)

__all__ = [
    "EntityProfileView",
    "ExistingInstance",
    "MaterialisationPlan",
    "PlannedInstance",
    "plan",
]

#: States in which an instance is finished. Their dates are never rewritten: a
#: retroactive extension must not turn a filing that was late into one that was
#: on time, or the record stops being evidence.
TERMINAL_STATES = frozenset({"FILED", "CLOSED"})

#: The only state an instance may be archived from. Anything further along has
#: history worth keeping.
ARCHIVABLE_STATES = frozenset({"NOT_STARTED"})


@dataclass(frozen=True, slots=True)
class EntityProfileView:
    """Everything the engine needs to know about one entity."""

    entity_id: str
    country: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    jurisdictions: frozenset[str] = field(default_factory=frozenset)
    registrations: tuple[ScopeRef, ...] = ()
    premises: tuple[ScopeRef, ...] = ()
    events: Mapping[str, date] = field(default_factory=dict)
    incorporation_date: date | None = None
    cessation_date: date | None = None


@dataclass(frozen=True, slots=True)
class PlannedInstance:
    identity: Identity
    definition_code: str
    definition_version: int
    title: str
    category: str
    period: Period
    scope: ScopeRef
    due_date: date | None
    original_due_date: date | None = None
    applied_extension_reference: str = ""
    owner_role: str = ""
    #: TRUE means every condition was decided; UNKNOWN means a fact is missing
    #: and the user is asked to confirm rather than being told nothing applies.
    confirmed: bool = True
    reasons: tuple[str, ...] = ()
    needs_input: str = ""


@dataclass(frozen=True, slots=True)
class ExistingInstance:
    """An instance already in the register, as the planner needs to see it."""

    identity: Identity
    state: str = "NOT_STARTED"
    due_date: date | None = None
    definition_version: int = 0
    has_evidence: bool = False
    has_history: bool = False
    archived: bool = False

    @property
    def is_protected(self) -> bool:
        """Whether this instance may never be quietly removed."""
        return (
            self.state in TERMINAL_STATES
            or self.state not in ARCHIVABLE_STATES
            or self.has_evidence
            or self.has_history
        )


@dataclass(frozen=True, slots=True)
class InstanceDelta:
    identity: Identity
    changes: Mapping[str, tuple[Any, Any]]


@dataclass(frozen=True, slots=True)
class SupersedeAction:
    identity: Identity
    reason: str


@dataclass(frozen=True, slots=True)
class ArchiveAction:
    identity: Identity
    reason: str


@dataclass(frozen=True, slots=True)
class MaterialisationPlan:
    to_create: tuple[PlannedInstance, ...] = ()
    to_update: tuple[InstanceDelta, ...] = ()
    to_supersede: tuple[SupersedeAction, ...] = ()
    to_archive: tuple[ArchiveAction, ...] = ()
    to_revive: tuple[Identity, ...] = ()
    unchanged: tuple[Identity, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    not_applicable: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (
            self.to_create
            or self.to_update
            or self.to_supersede
            or self.to_archive
            or self.to_revive
        )

    def summary(self) -> str:
        """The one-line diff a user approves before anything is written."""
        parts = []
        if self.to_create:
            parts.append(f"{len(self.to_create)} added")
        if self.to_update:
            parts.append(f"{len(self.to_update)} changed")
        if self.to_supersede:
            parts.append(f"{len(self.to_supersede)} no longer applicable")
        if self.to_archive:
            parts.append(f"{len(self.to_archive)} removed")
        if self.to_revive:
            parts.append(f"{len(self.to_revive)} restored")
        return ", ".join(parts) if parts else "no changes"


# ---------------------------------------------------------------------------


def _fan_out(definition: DefinitionSnapshot, profile: EntityProfileView) -> list[ScopeRef]:
    """The scopes one definition produces instances for.

    This is where "six GST registrations means six GSTR-3Bs a month" happens. A
    definition scoped to a registration type that the entity does not hold
    produces nothing at all.
    """
    match definition.instance_scope:
        case InstanceScope.ENTITY:
            return [ScopeRef(kind=InstanceScope.ENTITY, ref="", label="")]

        case InstanceScope.REGISTRATION:
            wanted = str(definition.scope_selector.get("registration_type", ""))
            return [
                registration
                for registration in profile.registrations
                if not wanted or registration.label == wanted
            ]

        case InstanceScope.PREMISES:
            wanted = str(definition.scope_selector.get("premises_type", ""))
            return [site for site in profile.premises if not wanted or site.label == wanted]

    return []


def plan(
    *,
    profile: EntityProfileView,
    catalog: Sequence[DefinitionSnapshot],
    horizon_start: date,
    horizon_end: date,
    fy: FiscalYearConvention,
    calendars: CalendarSnapshot | None = None,
    extensions: ExtensionSet | None = None,
    existing: Sequence[ExistingInstance] = (),
    suppressed: frozenset[Identity] = frozenset(),
    as_of: date | None = None,
) -> MaterialisationPlan:
    """Work out what this entity's obligation register should contain.

    :param suppressed: identities the user has marked not-applicable or deferred.
        Passed in rather than inferred, because a nightly job that resurrects
        obligations somebody dismissed destroys trust faster than any bug.
    :param as_of: required for determinism; the engine never reads the clock.
    """
    calendars = calendars or CalendarSnapshot()
    as_of = as_of or horizon_start

    desired: dict[Identity, PlannedInstance] = {}
    diagnostics: list[Diagnostic] = []
    not_applicable: dict[str, tuple[str, ...]] = {}

    for definition in catalog:
        if definition.country != profile.country:
            continue
        # A definition limited to particular sub-jurisdictions only applies where
        # the entity actually operates.
        if definition.jurisdictions and not (definition.jurisdictions & profile.jurisdictions):
            continue

        verdict = evaluate(definition.applicability_rule, profile.facts)
        if verdict.result is V.FALSE:
            not_applicable[definition.code] = tuple(verdict.reasons())
            continue

        scopes = _fan_out(definition, profile)
        if not scopes:
            continue

        lag = static_max_lag_days(definition.due_rule)
        periods = generate_periods(
            periodicity=definition.periodicity,
            fy=fy,
            # Widened by the maximum lag: a period that closed before the window
            # opened can still be due inside it.
            window_start=horizon_start - _days(lag),
            window_end=horizon_end,
            period_anchor=definition.period_anchor,
        )

        for scope in scopes:
            for period in periods:
                if not definition.is_effective_for(period):
                    continue
                if not scope.covers(period):
                    continue
                if profile.incorporation_date and period.end < profile.incorporation_date:
                    continue
                if profile.cessation_date and period.start > profile.cessation_date:
                    continue

                resolution = resolve_due_date(
                    definition,
                    period,
                    calendars=calendars,
                    fy=fy,
                    extensions=extensions,
                    events=profile.events,
                    scope_jurisdictions=(
                        frozenset({scope.jurisdiction})
                        if scope.jurisdiction
                        else profile.jurisdictions
                    ),
                    licence_expiry=scope.valid_to,
                )

                # The horizon filters on the DUE date, not the period. This is
                # the line that keeps annual returns in the calendar.
                if resolution.effective_date is not None and not (
                    horizon_start <= resolution.effective_date <= horizon_end
                ):
                    continue

                identity = Identity(
                    definition_code=definition.code,
                    scope_ref=scope.ref,
                    period_key=period.key,
                )
                if identity in suppressed:
                    continue

                if resolution.blocking_input:
                    diagnostics.append(
                        Diagnostic(
                            Severity.INFO,
                            "needs_input",
                            f"{definition.title} cannot be scheduled until "
                            f"{resolution.blocking_input.replace('_', ' ').lower()} is recorded.",
                            definition.code,
                        )
                    )

                desired[identity] = PlannedInstance(
                    identity=identity,
                    definition_code=definition.code,
                    definition_version=definition.version,
                    title=definition.title,
                    category=definition.category,
                    period=period,
                    scope=scope,
                    due_date=resolution.effective_date,
                    original_due_date=resolution.original_date,
                    applied_extension_reference=(
                        resolution.applied_extension.notification_reference
                        if resolution.applied_extension
                        else ""
                    ),
                    owner_role=definition.default_owner_role,
                    confirmed=verdict.result is V.TRUE,
                    reasons=tuple(verdict.reasons()),
                    needs_input=resolution.blocking_input,
                )

    return _diff(desired, existing, diagnostics, not_applicable)


def _days(count: int) -> timedelta:
    return timedelta(days=count)


def _diff(
    desired: Mapping[Identity, PlannedInstance],
    existing: Sequence[ExistingInstance],
    diagnostics: list[Diagnostic],
    not_applicable: Mapping[str, tuple[str, ...]],
) -> MaterialisationPlan:
    """Compare what should exist with what does."""
    live = {row.identity: row for row in existing if not row.archived}
    archived = {row.identity: row for row in existing if row.archived}

    to_create: list[PlannedInstance] = []
    to_revive: list[Identity] = []
    to_update: list[InstanceDelta] = []
    unchanged: list[Identity] = []

    for identity, planned in desired.items():
        current = live.get(identity)
        if current is None:
            # Reviving beats creating: if the profile flips back, the original
            # row returns rather than a duplicate appearing beside it.
            if identity in archived:
                to_revive.append(identity)
            else:
                to_create.append(planned)
            continue

        changes: dict[str, tuple[Any, Any]] = {}

        # A finished instance keeps the dates it was finished against. Rewriting
        # them would reclassify a late filing as on time.
        if current.state not in TERMINAL_STATES and current.due_date != planned.due_date:
            changes["due_date"] = (current.due_date, planned.due_date)

        if current.definition_version != planned.definition_version:
            changes["definition_version"] = (
                current.definition_version,
                planned.definition_version,
            )

        if changes:
            to_update.append(InstanceDelta(identity=identity, changes=changes))
        else:
            unchanged.append(identity)

    to_supersede: list[SupersedeAction] = []
    to_archive: list[ArchiveAction] = []

    for identity, current in live.items():
        if identity in desired:
            continue

        reasons = not_applicable.get(identity.definition_code, ())
        reason = reasons[0] if reasons else "No longer applicable after a profile change."

        if current.state in TERMINAL_STATES:
            # Already filed. It stays exactly as it is — history, not a mistake.
            continue

        if current.is_protected:
            to_supersede.append(SupersedeAction(identity=identity, reason=reason))
        else:
            to_archive.append(ArchiveAction(identity=identity, reason=reason))

    return MaterialisationPlan(
        to_create=tuple(to_create),
        to_update=tuple(to_update),
        to_supersede=tuple(to_supersede),
        to_archive=tuple(to_archive),
        to_revive=tuple(to_revive),
        unchanged=tuple(unchanged),
        diagnostics=tuple(diagnostics),
        not_applicable=dict(not_applicable),
    )
