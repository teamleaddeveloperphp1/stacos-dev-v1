"""
Which registration identifiers an entity type sees on the Add/Edit Entity
screen, and at what requirement level.

The mapping itself is data — ``catalog/packs/{country}.yaml``'s
``registration_types.by_entity_type``, loaded verbatim into
``JurisdictionPack.registration_types`` by ``manage.py loadpack``. This module
only resolves that data into something a form can iterate, plus the one
display-order exception the product spec calls out explicitly: CIN, where it
applies, always renders first with PAN immediately after it, regardless of
where the pack lists it. That is UI layout policy, not a fact about India, so
it is code rather than a column repeated in every row of the pack.

Not part of ``stacos.engine`` — it depends on the database (``JurisdictionPack``)
directly rather than taking pack data as a parameter, because the Add/Edit
Entity view has no other reason to hold a pack object around.
"""

from __future__ import annotations

from dataclasses import dataclass

from stacos.jurisdictions.models import JurisdictionPack
from stacos.jurisdictions.validators import get_validator

__all__ = ["RegistrationRequirement", "get_registration_requirements"]

#: The one identifier that jumps to the front of the list wherever it applies,
#: with PAN pinned immediately after it. See the module docstring.
_PINNED_FIRST = "CIN"
_PINNED_SECOND = "PAN"


@dataclass(frozen=True, slots=True)
class RegistrationRequirement:
    code: str
    #: "M" (mandatory), "C" (conditional), "M/C" (mandatory-or-conditional, a
    #: literal mark preserved from the source mapping), or "" (present,
    #: unmarked — treated as not-required until a conditional rule exists).
    requirement: str
    per_jurisdiction: bool
    #: False when the identifier's format has not been verified — see
    #: ``RegistrationValidator.verified`` in ``stacos.jurisdictions.validators``.
    verified: bool

    @property
    def is_mandatory(self) -> bool:
        return self.requirement == "M"


def _reorder_cin_first(
    requirements: list[RegistrationRequirement],
) -> list[RegistrationRequirement]:
    by_code = {r.code: r for r in requirements}
    if _PINNED_FIRST not in by_code:
        return requirements

    pinned = [by_code[_PINNED_FIRST]]
    if _PINNED_SECOND in by_code:
        pinned.append(by_code[_PINNED_SECOND])
    pinned_codes = {r.code for r in pinned}
    return pinned + [r for r in requirements if r.code not in pinned_codes]


def get_registration_requirements(country: str, entity_type: str) -> list[RegistrationRequirement]:
    """The identifier fields the Add/Edit Entity screen should show for
    ``entity_type``, in display order.

    Returns an empty list — never raises — when the pack does not exist, has
    not been (re)loaded since ``registration_types`` gained its
    ``by_entity_type`` shape, or does not name ``entity_type``. Each of those
    is "nothing has been authored for this yet", and the Add/Edit Entity
    screen's fallback for that is to behave exactly as it did before this
    feature existed, not to error.
    """
    if not entity_type:
        return []

    pack = JurisdictionPack.objects.filter(country=country).first()
    if pack is None:
        return []

    data = pack.registration_types
    if not isinstance(data, dict):
        return []

    catalog = {entry["code"]: entry for entry in data.get("catalog", [])}
    entries = data.get("by_entity_type", {}).get(entity_type, [])

    requirements: list[RegistrationRequirement] = []
    for entry in entries:
        if isinstance(entry, str):
            code, requirement = entry, ""
        else:
            code, requirement = entry["code"], entry.get("requirement", "")

        validator = get_validator(code)
        requirements.append(
            RegistrationRequirement(
                code=code,
                requirement=requirement,
                per_jurisdiction=bool(catalog.get(code, {}).get("per_jurisdiction", False)),
                verified=validator.verified if validator is not None else True,
            )
        )

    return _reorder_cin_first(requirements)
