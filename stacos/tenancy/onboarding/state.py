"""
The wizard's working state, and how it becomes something the engine can read.

Kept in the session rather than in a table. A draft belongs to a tenant that does
not exist yet, so a model would have to be added to the global allowlist and
would leave a row behind every time somebody opened the wizard and changed their
mind. The cost is that an expired session loses a draft — acceptable, because
every step is re-enterable on the created entity through the profile-edit screen.

The important method is :meth:`OnboardingDraft.to_profile_view`, and the
important thing about it is what it *omits*.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from stacos.engine.planner import EntityProfileView
from stacos.engine.types import InstanceScope, ScopeRef

__all__ = ["SESSION_KEY", "DraftRegistration", "OnboardingDraft"]

SESSION_KEY = "stacos_onboarding_draft"


@dataclass(frozen=True, slots=True)
class DraftRegistration:
    type: str
    value: str = ""
    jurisdiction: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"type": self.type, "value": self.value, "jurisdiction": self.jurisdiction}


@dataclass(frozen=True, slots=True)
class OnboardingDraft:
    country: str = "IN"
    name: str = ""
    legal_name: str = ""
    entity_type: str = ""
    registered_office_state: str = ""
    incorporation_date: str = ""
    registrations: tuple[DraftRegistration, ...] = ()
    states_of_operation: tuple[str, ...] = ()
    #: Answers to the ranked questions, keyed by fact.
    answers: dict[str, Any] = field(default_factory=dict)
    #: Pack codes the user has accepted.
    packs: tuple[str, ...] = ()
    #: Raw identifiers as typed, so the identity step can be re-rendered.
    identifiers: dict[str, str] = field(default_factory=dict)

    # -- Session round-tripping ---------------------------------------------

    @classmethod
    def from_session(cls, session: Any) -> OnboardingDraft:
        raw = session.get(SESSION_KEY)
        if not raw:
            return cls()
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError):
            return cls()
        return cls(
            country=str(data.get("country", "IN")),
            name=str(data.get("name", "")),
            legal_name=str(data.get("legal_name", "")),
            entity_type=str(data.get("entity_type", "")),
            registered_office_state=str(data.get("registered_office_state", "")),
            incorporation_date=str(data.get("incorporation_date", "")),
            registrations=tuple(
                DraftRegistration(**row) for row in data.get("registrations", []) if row.get("type")
            ),
            states_of_operation=tuple(data.get("states_of_operation", [])),
            answers=dict(data.get("answers", {})),
            packs=tuple(data.get("packs", [])),
            identifiers=dict(data.get("identifiers", {})),
        )

    def save(self, session: Any) -> None:
        session[SESSION_KEY] = {
            "country": self.country,
            "name": self.name,
            "legal_name": self.legal_name,
            "entity_type": self.entity_type,
            "registered_office_state": self.registered_office_state,
            "incorporation_date": self.incorporation_date,
            "registrations": [row.as_dict() for row in self.registrations],
            "states_of_operation": list(self.states_of_operation),
            "answers": self.answers,
            "packs": list(self.packs),
            "identifiers": self.identifiers,
        }
        session.modified = True

    def with_(self, **changes: Any) -> OnboardingDraft:
        return replace(self, **changes)

    # -- What the engine reads ----------------------------------------------

    @property
    def jurisdictions(self) -> frozenset[str]:
        found = set(self.states_of_operation)
        if self.registered_office_state:
            found.add(self.registered_office_state)
        found.update(row.jurisdiction for row in self.registrations if row.jurisdiction)
        return frozenset(found)

    def to_profile_view(self) -> EntityProfileView:
        """The draft as the planner sees it.

        **Every key is omitted until it is actually known**, and the ``if`` guards
        below are the whole trick. ``EntityProfileView`` as built for a saved
        entity always sets ``registrations``, even to the empty list — and an
        empty list is *definite absence*, so ``registrations includes GST`` is
        FALSE rather than UNKNOWN. Most of the catalog reads exactly that clause.
        Copy the saved-entity shape here and the preview turns confidently empty
        at the very moment it is supposed to be inviting.
        """
        facts: dict[str, Any] = {"country": self.country}
        if self.entity_type:
            facts["entity_type"] = self.entity_type
        if self.registered_office_state:
            facts["registered_office_state"] = self.registered_office_state
        if self.states_of_operation:
            facts["states_of_operation"] = sorted(self.states_of_operation)
        if self.registrations:
            facts["registrations"] = sorted({row.type for row in self.registrations})
        facts.update(self.answers)

        incorporation: date | None = None
        if self.incorporation_date:
            try:
                incorporation = date.fromisoformat(self.incorporation_date)
            except ValueError:
                incorporation = None

        return EntityProfileView(
            entity_id="draft",
            country=self.country,
            facts=facts,
            jurisdictions=self.jurisdictions,
            registrations=tuple(
                ScopeRef(
                    kind=InstanceScope.REGISTRATION,
                    # A stable reference within the draft, so the preview is the
                    # same on every keystroke. Real ids arrive at commit.
                    ref=f"draft:{row.type}:{row.jurisdiction}",
                    label=row.type,
                    jurisdiction=row.jurisdiction,
                )
                for row in self.registrations
            ),
            # A registered office is a premises, and declaring it is what makes
            # `has_factory_premises` resolvable to False rather than unknown for
            # an office-only business.
            premises=(
                (
                    ScopeRef(
                        kind=InstanceScope.PREMISES,
                        ref="draft:registered-office",
                        label="REGISTERED_OFFICE",
                        jurisdiction=self.registered_office_state,
                    ),
                )
                if self.registered_office_state
                else ()
            ),
            incorporation_date=incorporation,
        )

    @property
    def is_ready_to_commit(self) -> bool:
        return bool(self.name and self.entity_type and self.registered_office_state)
