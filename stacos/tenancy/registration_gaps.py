"""
Which registrations an entity still has to record, and which it may still add.

The "Add a registration" picker used to offer the full identifier vocabulary no
matter what the entity already held, so a PAN recorded on the Add Entity screen
was offered again, and saving it failed on ``entityreg_active_unique``. This
works out what is left instead, from two pieces of pack data:

- ``registration_types.by_entity_type`` — the identifiers this kind of entity
  holds (see ``stacos.jurisdictions.registration_requirements``);
- ``registration_types.catalog[].per_jurisdiction`` — the identifiers issued
  once per state (a GSTIN, a professional-tax number), which stay on offer
  after the first one, and are ruled out only for the states already used.

"Held" follows the database's uniqueness rule exactly: an active row (not
archived) with no ``valid_to``. A registration with an end date set does not
block its successor, since recording a renewed licence is how a lapsing one
gets replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stacos.jurisdictions.facts import IN_STATE_CODES, REGISTRATION_TYPES
from stacos.jurisdictions.registration_requirements import (
    RegistrationRequirement,
    get_per_jurisdiction_codes,
    get_registration_requirements,
)
from stacos.tenancy.models import Entity

__all__ = ["RegistrationGaps", "registration_gaps"]

#: Every value the State picker can submit, "not state-specific" included.
_ALL_JURISDICTIONS: tuple[str, ...] = ("", *IN_STATE_CODES)


@dataclass(frozen=True, slots=True)
class RegistrationGaps:
    #: The identifiers the picker may offer, in display order, before anything
    #: held is taken away. The entity type's list from the pack, or — where
    #: the pack names nothing for this entity type — the full vocabulary, which
    #: is how the picker behaved before this existed.
    universe: tuple[RegistrationRequirement, ...]
    #: False when ``universe`` is the full-vocabulary fallback. There is then no
    #: list to measure "finished" against, so the step does not claim either way.
    has_checklist: bool
    #: code → the jurisdictions an unexpired row of that type already occupies.
    held: dict[str, frozenset[str]] = field(default_factory=dict)

    def taken_jurisdictions(self, code: str) -> frozenset[str]:
        return self.held.get(code, frozenset())

    def is_available(self, requirement: RegistrationRequirement) -> bool:
        taken = self.taken_jurisdictions(requirement.code)
        if requirement.per_jurisdiction:
            return len(taken) < len(_ALL_JURISDICTIONS)
        return not taken

    @property
    def available(self) -> list[RegistrationRequirement]:
        """What the picker offers: never held, or per-state with a state free."""
        return [r for r in self.universe if self.is_available(r)]

    @property
    def outstanding(self) -> list[RegistrationRequirement]:
        """What is on the entity type's list and not yet held at all.

        A per-state identifier counts as recorded from its first state on:
        the pack knows GST applies to a private company, not how many states
        this one is registered in.
        """
        if not self.has_checklist:
            return []
        return [r for r in self.universe if not self.taken_jurisdictions(r.code)]

    @property
    def is_complete(self) -> bool:
        return self.has_checklist and not self.outstanding

    def requirement(self, code: str) -> RegistrationRequirement | None:
        return next((r for r in self.universe if r.code == code), None)


def registration_gaps(entity: Entity) -> RegistrationGaps:
    """Must be called inside a scope that can see ``entity``'s registrations."""
    requirements = get_registration_requirements(entity.country, entity.entity_type)
    has_checklist = bool(requirements)
    if not has_checklist:
        per_jurisdiction = get_per_jurisdiction_codes(entity.country)
        requirements = [
            RegistrationRequirement(
                code=code,
                requirement="",
                per_jurisdiction=code in per_jurisdiction,
                verified=True,
            )
            for code in REGISTRATION_TYPES
        ]

    held: dict[str, set[str]] = {}
    rows = entity.registrations.filter(archived_at__isnull=True, valid_to__isnull=True)
    for code, jurisdiction in rows.values_list("type", "jurisdiction"):
        held.setdefault(code, set()).add(jurisdiction)

    return RegistrationGaps(
        universe=tuple(requirements),
        has_checklist=has_checklist,
        held={code: frozenset(states) for code, states in held.items()},
    )
