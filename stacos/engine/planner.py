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
    OCCURRENCE_SPACE,
    CalendarSnapshot,
    DefinitionSnapshot,
    Diagnostic,
    EventOccurrence,
    ExtensionSet,
    FiscalYearConvention,
    Identity,
    InstanceScope,
    Period,
    Periodicity,
    ScopeRef,
    Severity,
    occurrence_number,
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

#: Shown first on an obligation that exists only because somebody added it. The
#: reasons list is the product's core claim — "you file this because…" — and
#: letting an opt-in borrow the rule's explanation would make that claim a lie.
_OPT_IN_REASON = "you added this to your calendar"


@dataclass(frozen=True, slots=True)
class EntityProfileView:
    """Everything the engine needs to know about one entity."""

    entity_id: str
    country: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    jurisdictions: frozenset[str] = field(default_factory=frozenset)
    registrations: tuple[ScopeRef, ...] = ()
    premises: tuple[ScopeRef, ...] = ()
    #: Latest date per key. What an ``EVENT_DATE`` anchor reads, and what the
    #: six AGM-anchored definitions have always used.
    events: Mapping[str, date] = field(default_factory=dict)
    #: Every recorded event, individually. What an ``EVENT_BASED`` definition
    #: materialises one instance per. Kept alongside ``events`` rather than
    #: derived from it on each call, because ``resolve_due_date`` runs once per
    #: definition x scope x period and collapsing there would regress a path
    #: advertised at 10-25 ms.
    occurrences: tuple[EventOccurrence, ...] = ()
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
    #: Which facts were absent *and* would have changed the answer. Carried out
    #: of the engine rather than discarded with the verdict, because it is the
    #: difference between a badge that says "confirm" and one that can say what
    #: to confirm — the register had the flag and not the reason, so the badge
    #: invited an action the product could not offer.
    missing_facts: tuple[str, ...] = ()
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
    #: The prompt still displayed on the row — "tell us your AGM date". Compared
    #: so that answering the prompt clears it: an instance showing both a date
    #: and a request for the date it was computed from reads as broken.
    needs_input: str = ""
    #: Whether the applicability rule was decided. A fact arriving later turns an
    #: UNKNOWN into a definite answer, and the "confirm this" flag has to go with
    #: it or the user is asked to confirm something already settled.
    confirmed: bool = True
    #: The facts still blocking that decision. Compared like ``confirmed``, so
    #: answering one of two open questions narrows the prompt instead of leaving
    #: it asking for something already given.
    missing_facts: tuple[str, ...] = ()

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


def _trigger_occurrences(
    definition: DefinitionSnapshot, profile: EntityProfileView
) -> list[tuple[Period, EventOccurrence]]:
    """The periods an event-triggered definition produces, one per occurrence.

    This is the whole difference between a periodic obligation and a triggered
    one. A monthly return exists because a month ended; DIR-12 exists because a
    director was appointed, and there are exactly as many of them as there were
    appointments.
    """
    key = str((definition.trigger or {}).get("event_key", ""))
    if not key:
        return []

    condition = (definition.trigger or {}).get("when") or {}
    matched: list[tuple[Period, EventOccurrence]] = []

    for occurrence in profile.occurrences:
        if occurrence.key != key:
            continue
        # The same three-valued evaluator as applicability, and the same reading
        # of UNKNOWN: an appointment nobody classified as executive still
        # materialises MR-1, unconfirmed, rather than being dropped because a
        # question was never asked. Dropping it is the failure that gets a client
        # fined.
        if condition and evaluate(condition, occurrence.attributes).result is V.FALSE:
            continue
        matched.append(
            (
                Period(
                    key=f"EV-{occurrence.occurred_on.isoformat()}",
                    label=occurrence.label or occurrence.occurred_on.isoformat(),
                    start=occurrence.occurred_on,
                    end=occurrence.occurred_on,
                ),
                occurrence,
            )
        )
    return matched


def _assign_occurrences(
    pairs: Sequence[tuple[Period, EventOccurrence | None]],
) -> list[tuple[Period, EventOccurrence | None, int]]:
    """Occurrence numbers within one (definition, scope) fan-out.

    Derived from each event's own stable reference, never from its position.
    Position-derived ordinals fail the case this feature exists for: plan over
    two same-day appointments, delete the first, and the second renumbers — which
    under ``(entity, definition_code, scope_ref, period_key, occurrence)`` is a
    *different obligation*, so the row somebody had begun preparing is superseded
    and an empty duplicate appears beside it.

    Collisions can only happen between events sharing one definition, one scope
    and one day — about one in ten thousand for five same-day events. They are
    resolved by linear probing in ``ref`` order, so the winner is the same on
    every replan and only the loser depends on ordering.
    """
    taken: set[int] = set()
    numbered: list[tuple[Period, EventOccurrence | None, int]] = []

    for period, occurrence in sorted(
        pairs, key=lambda pair: pair[1].ref if pair[1] is not None else ""
    ):
        if occurrence is None:
            numbered.append((period, None, 0))
            continue
        number = occurrence_number(occurrence.ref)
        while number in taken:
            number = (number + 1) % OCCURRENCE_SPACE
        taken.add(number)
        numbered.append((period, occurrence, number))

    return numbered


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
    opted_in: frozenset[str] = frozenset(),
    as_of: date | None = None,
) -> MaterialisationPlan:
    """Work out what this entity's obligation register should contain.

    :param suppressed: identities the user has marked not-applicable or deferred.
        Passed in rather than inferred, because a nightly job that resurrects
        obligations somebody dismissed destroys trust faster than any bug.
    :param opted_in: definition codes the user has deliberately added, whether
        through a compliance pack or one at a time. The symmetric counterpart of
        ``suppressed``, and it has to apply at the *top* of the loop rather than
        at the diff: an opt-in must make the rule produce something it otherwise
        would not, where a suppression removes something the rule already
        produced. Revoking one then needs no new machinery at all — the next plan
        simply finds the rule FALSE again.
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
        # An opt-in overrides a definite NO. Somebody chose this deliberately, and
        # a nightly job that deletes what a user asked for loses trust exactly as
        # fast as one that resurrects what they dismissed.
        forced = definition.code in opted_in
        if verdict.result is V.FALSE and not forced:
            not_applicable[definition.code] = tuple(verdict.reasons())
            continue

        scopes = _fan_out(definition, profile)
        if not scopes:
            continue

        if definition.periodicity is Periodicity.EVENT_BASED:
            # No window is applied: an event series is exactly as large as the
            # set of things somebody recorded, so there is nothing infinite to
            # bound.
            pairs: list[tuple[Period, EventOccurrence | None]] = list(
                _trigger_occurrences(definition, profile)
            )
        else:
            lag = static_max_lag_days(definition.due_rule)
            pairs = [
                (period, None)
                for period in generate_periods(
                    periodicity=definition.periodicity,
                    fy=fy,
                    # Widened by the maximum lag: a period that closed before the
                    # window opened can still be due inside it.
                    window_start=horizon_start - _days(lag),
                    window_end=horizon_end,
                    period_anchor=definition.period_anchor,
                )
            ]

        for scope in scopes:
            # An event naming one plant or one registration produces an instance
            # for that scope alone; an entity-wide event produces one for every
            # scope the definition fans out to.
            in_scope = [
                pair for pair in pairs if pair[1] is None or pair[1].scope_ref in ("", scope.ref)
            ]
            for period, occurrence, number in _assign_occurrences(in_scope):
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
                    trigger_date=occurrence.occurred_on if occurrence else None,
                    scope_jurisdictions=(
                        frozenset({scope.jurisdiction})
                        if scope.jurisdiction
                        else profile.jurisdictions
                    ),
                    licence_expiry=scope.valid_to,
                )

                # The horizon filters on the DUE date, not the period. This is
                # the line that keeps annual returns in the calendar.
                #
                # Triggered instances are deliberately exempt, at both ends. The
                # horizon exists to bound an *infinite* series; an event series is
                # finite, and every member of it was typed in by a person on
                # purpose. Apply the window and, on the first nightly run after
                # the 120-day lookback rolls past it, a DIR-12 triggered eight
                # months ago and never filed stops being desired — and `_diff`
                # quietly archives the obligation carrying the largest live
                # penalty in the register, on a night when nothing happened.
                if (
                    occurrence is None
                    and resolution.effective_date is not None
                    and not (horizon_start <= resolution.effective_date <= horizon_end)
                ):
                    continue

                identity = Identity(
                    definition_code=definition.code,
                    scope_ref=scope.ref,
                    period_key=period.key,
                    occurrence=number,
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
                    # An opt-in is never "confirmed": the rule did not decide
                    # this, a person did, and the row has to say which.
                    confirmed=verdict.result is V.TRUE and not forced,
                    # Only when the rule is genuinely undecided. An opt-in is
                    # unconfirmed because a person chose it, and there is nothing
                    # to ask about that.
                    missing_facts=(
                        tuple(sorted(verdict.missing_facts)) if verdict.result is V.UNKNOWN else ()
                    ),
                    reasons=(
                        (_OPT_IN_REASON, *verdict.reasons())
                        if forced and verdict.result is not V.TRUE
                        else tuple(verdict.reasons())
                    ),
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

        # These two are what the row *says about itself*, and both go stale the
        # moment the input they describe arrives. Recording an AGM date resolves
        # the due date; without this the row would keep asking for the date it
        # has just been given. Compared for every instance including terminal
        # ones, because a prompt on a filed obligation is equally wrong.
        if current.needs_input != planned.needs_input:
            changes["needs_input"] = (current.needs_input, planned.needs_input)

        if current.confirmed != planned.confirmed:
            changes["confirmed"] = (current.confirmed, planned.confirmed)

        if tuple(current.missing_facts) != tuple(planned.missing_facts):
            changes["missing_facts"] = (
                tuple(current.missing_facts),
                tuple(planned.missing_facts),
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
