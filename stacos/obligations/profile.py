"""
Turning an entity into something the engine can evaluate.

Everything the planner needs about one entity is assembled here and handed over
as a frozen :class:`~stacos.engine.planner.EntityProfileView`. This is the only
place in the materialisation path that touches the ORM.

Two things are less obvious than they look.

**Facts are resolved *as of* a date, not as of today.** Turnover for FY 2025-26
is not known until books close in mid-2026 and is frequently restated afterwards.
Asking "what is this entity's turnover" gives the wrong answer for a period that
closed a year ago; asking "what was true for the period ending 31 March 2026"
gives the right one. :class:`~stacos.tenancy.models.EntityFactValue` stores the
history and this module reads it.

**Registrations and premises become scope references.** A definition scoped to
``REGISTRATION`` with a selector of ``GST`` fans out to one instance per GSTIN.
The planner matches the selector against ``ScopeRef.label``, so the label carries
the registration *type* — the human-readable GSTIN is resolved separately when
the instance is written, because the engine has no business knowing what a GSTIN
looks like.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import Q

from stacos.engine.planner import EntityProfileView
from stacos.engine.types import InstanceScope, ScopeRef
from stacos.obligations.models import EntityEvent
from stacos.tenancy.models import (
    Entity,
    EntityFactValue,
    EntityPremises,
    EntityProfile,
    EntityRegistration,
)

__all__ = ["ScopeDisplay", "build_profile_view", "scope_display_map"]

#: Turnover band boundaries, in rupees, low to high. India-shaped in its
#: *values* and declared as data, exactly like the state codes and entity types
#: beside it in the fact registry. Another jurisdiction supplies its own; nothing
#: in the engine or in a view knows these numbers.
TURNOVER_BANDS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("4000000"), "LT_40L"),
    (Decimal("15000000"), "LT_1_5CR"),
    (Decimal("50000000"), "LT_5CR"),
    (Decimal("500000000"), "LT_50CR"),
    (Decimal("2500000000"), "LT_250CR"),
)
_TOP_BAND = "GTE_250CR"


def build_profile_view(
    entity: Entity,
    *,
    as_of: date,
    profile: EntityProfile | None = None,
) -> EntityProfileView:
    """Assemble everything the planner needs about one entity.

    ``as_of`` governs which historical fact values apply and which registrations
    and premises count as live. Passing today's date is correct for a nightly
    roll; passing a period end is what makes a retrospective explanation honest.
    """
    profile = profile or EntityProfile.objects.filter(entity=entity).first()

    registrations = list(EntityRegistration.objects.filter(entity=entity, archived_at__isnull=True))
    premises = list(EntityPremises.objects.filter(entity=entity, archived_at__isnull=True))

    facts = _base_facts(entity, profile)
    facts.update(_derived_facts(registrations, premises, facts))
    facts.update(_historical_facts(entity, as_of=as_of))
    # Recomputed after the historical overlay, because a restated turnover has to
    # move the band with it — otherwise a rule reading the band and a rule
    # reading the raw number disagree about the same entity.
    facts.update(_derived_facts(registrations, premises, facts))

    jurisdictions = _jurisdictions(entity, profile, registrations, premises)

    return EntityProfileView(
        entity_id=str(entity.pk),
        country=entity.country,
        facts=facts,
        jurisdictions=frozenset(jurisdictions),
        registrations=tuple(_registration_scope(row) for row in registrations),
        premises=tuple(_premises_scope(row) for row in premises),
        events=_events(entity),
        incorporation_date=entity.incorporation_date,
        cessation_date=entity.cessation_date,
    )


def _base_facts(entity: Entity, profile: EntityProfile | None) -> dict[str, Any]:
    if profile is not None:
        return dict(profile.as_fact_dict())
    # An entity with no profile still has the handful of facts that come from its
    # own columns. It will produce a thin calendar rather than an empty one,
    # which is the right behaviour during onboarding.
    return {
        "country": entity.country,
        "entity_type": entity.entity_type,
        "registered_office_state": entity.registered_office_state,
        "incorporation_date": (
            entity.incorporation_date.isoformat() if entity.incorporation_date else None
        ),
    }


def _derived_facts(
    registrations: list[EntityRegistration],
    premises: list[EntityPremises],
    facts: Mapping[str, Any],
) -> dict[str, Any]:
    """Facts computed from other facts, per the registry's ``DERIVED`` source."""
    registration_types = sorted({row.type for row in registrations})
    premises_types = sorted({row.type for row in premises})

    derived: dict[str, Any] = {
        "registrations": registration_types,
        "premises_types": premises_types,
        "has_factory_premises": any(kind in {"FACTORY", "PLANT"} for kind in premises_types),
    }

    band = _turnover_band(facts.get("aggregate_turnover"))
    if band is not None:
        derived["turnover_band"] = band

    return derived


def _turnover_band(value: Any) -> str | None:
    """Bucket a turnover figure. ``None`` when turnover is unknown.

    Returning ``None`` rather than a default band matters: a missing fact must
    reach the evaluator as missing, so the rule resolves to UNKNOWN and the
    obligation is materialised unconfirmed. Substituting the lowest band here
    would silently answer a question nobody asked.
    """
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None
    for boundary, label in TURNOVER_BANDS:
        if amount < boundary:
            return label
    return _TOP_BAND


def _historical_facts(entity: Entity, *, as_of: date) -> dict[str, Any]:
    """Effective-dated facts, resolved for the given date.

    Later ``valid_from`` wins among rows whose window covers ``as_of``, and a
    superseded row is ignored entirely. Ordering is explicit rather than relying
    on the model's default, because "which restatement applies" is precisely the
    question this table exists to answer.
    """
    rows = (
        EntityFactValue.objects.filter(entity=entity, superseded_at__isnull=True)
        .filter(valid_from__lte=as_of)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=as_of))
        .order_by("key", "valid_from", "recorded_at")
    )
    # Iterating in ascending order and overwriting leaves the latest applicable
    # value in place, which is the one that governs.
    return {row.key: row.value for row in rows}


def _jurisdictions(
    entity: Entity,
    profile: EntityProfile | None,
    registrations: list[EntityRegistration],
    premises: list[EntityPremises],
) -> set[str]:
    """Every sub-jurisdiction the entity touches.

    Union of stated operations, registration locations and premises locations —
    not just the declared states. A client who forgot to tick Tamil Nadu but holds
    a Tamil Nadu GSTIN operates in Tamil Nadu, and the calendar should say so.
    """
    found: set[str] = set()
    if profile is not None:
        found.update(profile.states_of_operation or ())
    if entity.registered_office_state:
        found.add(entity.registered_office_state)
    found.update(row.jurisdiction for row in registrations if row.jurisdiction)
    found.update(row.jurisdiction for row in premises if row.jurisdiction)
    return found


def _registration_scope(row: EntityRegistration) -> ScopeRef:
    return ScopeRef(
        kind=InstanceScope.REGISTRATION,
        ref=str(row.pk),
        # The *type*, because that is what the planner matches `scope_selector`
        # against. Display text is resolved from `ref` when the row is written.
        label=row.type,
        jurisdiction=row.jurisdiction,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
    )


def _premises_scope(row: EntityPremises) -> ScopeRef:
    return ScopeRef(
        kind=InstanceScope.PREMISES,
        ref=str(row.pk),
        label=row.type,
        jurisdiction=row.jurisdiction,
        valid_from=row.operational_from,
        valid_to=row.operational_to,
    )


def _events(entity: Entity) -> dict[str, date]:
    """Recorded dates that due rules anchor on.

    The most recent date wins for a repeated event: "the last board meeting" is
    what a 120-day gap rule needs, not the first one ever held.
    """
    events: dict[str, date] = {}
    for row in EntityEvent.objects.filter(entity=entity).order_by("key", "occurred_on"):
        events[row.key] = row.occurred_on
    return events


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


class ScopeDisplay:
    """A resolved, human-readable label for a scope reference."""

    __slots__ = ("jurisdiction", "label")

    def __init__(self, label: str, jurisdiction: str = "") -> None:
        self.label = label
        self.jurisdiction = jurisdiction


def scope_display_map(entity: Entity) -> dict[str, ScopeDisplay]:
    """Map every scope reference of an entity to what a person should read.

    Built once per materialisation and per list render rather than per row: two
    queries beats an N+1 across a calendar that routinely runs to several hundred
    obligations for a multi-state client.
    """
    display: dict[str, ScopeDisplay] = {}

    for row in EntityRegistration.objects.filter(entity=entity):
        label = row.label or f"{row.type} {row.value}"
        display[str(row.pk)] = ScopeDisplay(label.strip(), row.jurisdiction)

    for row in EntityPremises.objects.filter(entity=entity):
        display[str(row.pk)] = ScopeDisplay(row.name, row.jurisdiction)

    return display
