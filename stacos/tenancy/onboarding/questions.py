"""
Which question to ask next, and why that one.

An onboarding form that asks everything is abandoned; one that asks too little
produces a calendar so thin the client does not believe it. The way out is to ask
in order of *consequence* — and the engine has been computing exactly that from
the beginning and handing it to nobody.

``Verdict.missing_facts`` is the set of facts that were absent **and decisive**
for a rule that could not be decided. Collect it across the whole catalog, count
how many undecided definitions each fact would settle, and the ranking falls out.
"Are you registered under GST?" settles thirty filings; "do you hold an FCRA
registration?" settles one. Asking them in that order is the difference between a
two-minute wizard and a twenty-minute one.

Pure: no Django, no clock. It takes a catalog and a fact dictionary and returns an
order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from stacos.engine.rules import V, evaluate
from stacos.engine.types import DefinitionSnapshot, Periodicity
from stacos.jurisdictions.facts import REGISTRY, FactDef, FactSource

__all__ = ["Question", "rank_questions"]

#: How much an undecided definition is worth to the ranking, by how often it
#: recurs. A monthly filing is worth twelve annual ones to somebody deciding
#: whether a question is worth answering. Ranking on definition *count* alone
#: puts "do you hold an FCRA registration" above "are you GST registered",
#: which is the wrong way round by an order of magnitude.
_PERIODICITY_WEIGHT: dict[str, int] = {
    Periodicity.MONTHLY: 12,
    Periodicity.QUARTERLY: 4,
    Periodicity.HALF_YEARLY: 2,
    Periodicity.ANNUAL: 1,
    Periodicity.EVENT_BASED: 1,
    Periodicity.ONE_TIME: 1,
}

#: Facts a user cannot answer in a form, mapped to the question that stands in
#: for them. ``registrations`` is not a checkbox — it is the registrations step;
#: ``turnover_band`` is derived, so the thing to ask for is the turnover itself.
#: ``FactDef.depends_on`` has carried this information since the fact registry
#: was written and has been read by nothing.
_UNASKABLE_SOURCES = frozenset({FactSource.DERIVED, FactSource.REGISTRATION, FactSource.PREMISES})


@dataclass(frozen=True, slots=True)
class Question:
    """One fact worth asking for, and what answering it would settle."""

    fact: FactDef
    #: Definitions currently undecided that this fact is decisive for.
    unlocks: int
    #: Those definitions weighted by how often they recur — the real measure of
    #: how much work the answer removes.
    weight: int
    #: A handful of codes, so the UI can say "answering this settles GSTR-3B,
    #: GSTR-1 and 28 others" rather than showing a bare number.
    decisive_for: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.fact.key


def rank_questions(
    *,
    catalog: Sequence[DefinitionSnapshot],
    facts: Mapping[str, Any],
    limit: int = 12,
) -> tuple[Question, ...]:
    """The facts worth asking for next, most consequential first.

    Only *undecided* definitions contribute. A rule that is already definitely
    true or definitely false is settled, and asking a question that cannot change
    it is how a wizard becomes long enough to abandon.
    """
    unlocks: dict[str, int] = {}
    weight: dict[str, int] = {}
    examples: dict[str, list[str]] = {}

    for definition in catalog:
        verdict = evaluate(definition.applicability_rule, facts)
        if verdict.result is not V.UNKNOWN:
            continue
        recurrence = _PERIODICITY_WEIGHT.get(definition.periodicity, 1)
        for key in _askable(verdict.missing_facts):
            unlocks[key] = unlocks.get(key, 0) + 1
            weight[key] = weight.get(key, 0) + recurrence
            if len(examples.setdefault(key, [])) < 5:
                examples[key].append(definition.code)

    questions = [
        Question(
            fact=fact,
            unlocks=unlocks[key],
            weight=weight[key],
            decisive_for=tuple(examples.get(key, ())),
        )
        for key in unlocks
        if (fact := REGISTRY.get(key)) is not None
    ]

    # Weight first, not count. "Are you registered under GST?" settles fifteen
    # definitions and "what is your turnover?" settles twenty-six — but the GST
    # fifteen are monthly, so answering it removes three times as many filings
    # from the unknown column. Count alone gets that backwards.
    #
    # The final key is the fact name, so the same profile always produces the same
    # order and a golden test on it is stable.
    questions.sort(key=lambda question: (-question.weight, -question.unlocks, question.fact.key))
    return tuple(questions[:limit])


def _askable(missing: frozenset[str]) -> set[str]:
    """Substitute a question a person can actually answer.

    A derived fact has no input of its own: nobody types a turnover *band*, they
    type a turnover. Following ``depends_on`` turns an unanswerable fact into the
    one that would produce it. Registration- and premises-sourced facts are
    dropped entirely — they are separate steps in the wizard, not questions.
    """
    resolved: set[str] = set()
    for key in missing:
        definition = REGISTRY.get(key)
        if definition is None:
            continue
        if definition.source not in _UNASKABLE_SOURCES:
            resolved.add(key)
            continue
        for dependency in definition.depends_on:
            dependent = REGISTRY.get(dependency)
            if dependent is not None and dependent.source not in _UNASKABLE_SOURCES:
                resolved.add(dependency)
    return resolved
