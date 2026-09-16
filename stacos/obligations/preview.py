"""
"What applies to you" — the rule-evaluated partition of the catalog for one
entity, ranked questions and pack suggestions included.

Originally the onboarding wizard's last step, working against a session-held
draft that had never been saved. Moved here and generalised to work against a
real, saved :class:`~stacos.tenancy.models.Entity` instead, because the product
no longer throws this screen away after the entity is created — it is the
permanent "Compliance" section of the entity detail page (see
``stacos.obligations.views.entity_summary`` / ``rebuild_calendar``).

Everything here is pure with respect to Django's ORM except :func:`adopt_pack`
and :func:`revoke_pack`, which write :class:`ObligationInclusion` rows. Building
the :class:`~stacos.engine.planner.EntityProfileView` and the accepted-pack set
for a real entity is the caller's job — see
:func:`stacos.obligations.profile.build_profile_view` and
``_opted_in_codes``-style querying in :mod:`stacos.obligations.services`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from django.utils import timezone

from stacos.accounts.models import User
from stacos.catalog.models import CompliancePack
from stacos.catalog.snapshots import build_catalog
from stacos.engine.planner import EntityProfileView
from stacos.engine.rules import V, evaluate
from stacos.engine.types import DefinitionSnapshot
from stacos.jurisdictions.facts import REGISTRY
from stacos.obligations.models import ObligationInclusion
from stacos.obligations.questions import (
    Question,
    answered_questions,
    askable_facts,
    rank_questions,
)
from stacos.tenancy.models import Entity, Tenant

__all__ = [
    "CategoryGroup",
    "CategoryRow",
    "EntityPreview",
    "FamilyGroup",
    "PackSuggestion",
    "PreviewRow",
    "ReasonGroup",
    "adopt_pack",
    "for_preview",
    "preview_entity",
    "revoke_pack",
    "suggest_packs",
]

#: How many rows to show under a heading before folding the rest away.
#:
#: A display cap and nothing more. It used to be applied to the *stored* tuples,
#: which meant ``applies_count`` — and therefore the number on the button
#: somebody presses to build their calendar — reported 60 for a business with
#: several hundred obligations. A screen short enough to read is worth having;
#: one that is short because it understates the total is not.
_SHOWN_PER_REASON = 4

#: Same idea, for the merged by-category view. Higher than ``_SHOWN_PER_REASON``
#: because a category row carries one status pill rather than a repeated reason
#: sentence, so more of them fit before the list needs folding.
_SHOWN_PER_CATEGORY = 6


@dataclass(frozen=True, slots=True)
class PreviewRow:
    code: str
    title: str
    family: str
    category: str
    reason: str = ""
    #: For a "might apply" row: the fact that would settle it.
    blocked_on: str = ""


@dataclass(frozen=True, slots=True)
class ReasonGroup:
    """Rows within a family that are here for the same reason.

    The reason is the point of the grouping. Printed against every row, the
    column becomes several screens of one repeated sentence and nobody reads any
    of it — which means the rows that are here for a *different* reason, the ones
    somebody checking the product's working is looking for, are the hardest to
    find. Said once per group, the reason is the thing that varies.
    """

    reason: str
    rows: tuple[PreviewRow, ...]
    #: The fact that would settle this group, for a "might apply" column. Empty
    #: when nothing on this screen can settle it.
    blocked_on: str = ""
    #: The question in the queue beside this column that would settle the group,
    #: when there is one. Carrying the object rather than the key is what lets
    #: the template say *what* is being asked and link straight to it, instead of
    #: printing a fact name at somebody and leaving them to find the control.
    question: Question | None = None

    @property
    def is_answerable_here(self) -> bool:
        """Whether the question that settles this group is on screen right now."""
        return self.question is not None

    @property
    def is_askable_later(self) -> bool:
        """Blocked on something a person could answer, just not in the queue yet.

        The queue is capped at the most consequential handful, so a genuinely
        answerable fact can sit outside it. Lumping that in with "comes from your
        registrations" would tell the user something false about their own data —
        the three states are *asked now*, *asked once the queue moves on*, and
        *not a question at all*, and the screen has to say which.
        """
        return self.question is None and bool(self.blocked_on)

    @property
    def blocked_label(self) -> str:
        """The blocking fact in words, for a group with no question on screen."""
        definition = REGISTRY.get(self.blocked_on) if self.blocked_on else None
        return definition.label if definition is not None else ""

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def shown(self) -> tuple[PreviewRow, ...]:
        return self.rows[:_SHOWN_PER_REASON]

    @property
    def hidden(self) -> int:
        return max(0, len(self.rows) - _SHOWN_PER_REASON)


@dataclass(frozen=True, slots=True)
class FamilyGroup:
    family: str
    groups: tuple[ReasonGroup, ...]

    @property
    def count(self) -> int:
        return sum(group.count for group in self.groups)


@dataclass(frozen=True, slots=True)
class CategoryRow:
    """One obligation, its family already resolved, carrying its own verdict.

    The three-column layout answers "what verdict did this row get"; a CA
    scanning the result asks "what do I owe under GST" first and reads the
    verdict off each row, not off which column it landed in. This is that row.
    """

    row: PreviewRow
    #: A status-chip status: "complete" (applies) or "waiting" (might apply).
    status: str
    #: The fact blocking a "waiting" row, in words — empty for "complete".
    waiting_label: str = ""
    #: The question that would settle it, when one is on screen right now.
    question: Question | None = None
    #: Blocked on something askable, just not in the queue yet.
    is_askable_later: bool = False


@dataclass(frozen=True, slots=True)
class CategoryGroup:
    family: str
    rows: tuple[CategoryRow, ...]

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def shown(self) -> tuple[CategoryRow, ...]:
        return self.rows[:_SHOWN_PER_CATEGORY]

    @property
    def hidden_rows(self) -> tuple[CategoryRow, ...]:
        return self.rows[_SHOWN_PER_CATEGORY:]

    @property
    def hidden(self) -> int:
        return len(self.hidden_rows)


@dataclass(frozen=True, slots=True)
class EntityPreview:
    """Three columns, and the questions that would move rows between them.

    The tuples hold **every** row. Counts are read off them, so the number on the
    button is the number of obligations the user will actually get. How much of
    that is drawn is decided in the grouping, one layer down.
    """

    applies: tuple[PreviewRow, ...] = ()
    might_apply: tuple[PreviewRow, ...] = ()
    does_not_apply: tuple[PreviewRow, ...] = ()
    questions: tuple[Question, ...] = ()
    considered: int = 0

    @property
    def applies_count(self) -> int:
        return len(self.applies)

    @property
    def might_count(self) -> int:
        return len(self.might_apply)

    @property
    def excluded_count(self) -> int:
        return len(self.does_not_apply)

    @property
    def answerable_might_count(self) -> int:
        """Of the undecided, how many a question on this screen would settle.

        The rest wait on a registration or a premises. Presenting those as work
        available here is what makes the column read as a list nobody can finish.
        """
        return sum(1 for row in self.might_apply if row.blocked_on)

    def applies_by_family(self) -> list[FamilyGroup]:
        """Grouped by family, then by the reason the rule fired.

        ``family`` is the vocabulary a CA already speaks — ROC, GST, TDS,
        Professional tax. ``category`` is the axis engagements are scoped along
        and reads as jargon on a screen a business owner is looking at.
        """
        return _group(self.applies, key=lambda row: row.reason)

    def might_by_family(self) -> list[FamilyGroup]:
        """Grouped by what is *blocking* them rather than by why they fired.

        An undecided rule has no reason worth printing — it has a missing fact,
        and that fact is the thing the user can do something about. Grouping on
        it is what lets each group point at the one question that clears it.
        """
        return _group(
            self.might_apply,
            key=lambda row: row.blocked_on,
            questions={question.key: question for question in self.questions},
        )

    def by_category(self) -> list[CategoryGroup]:
        """Applies and might-apply, merged one family at a time.

        Built from :meth:`applies_by_family` and :meth:`might_by_family` rather
        than a fresh pass over the rows, so the reason-grouping and the
        blocked-on/question resolution stay the single source of truth — this
        only reshapes their output into one row per obligation with its own
        status pill, applies first.
        """
        combined: dict[str, list[CategoryRow]] = {}

        for family in self.applies_by_family():
            rows = combined.setdefault(family.family, [])
            for group in family.groups:
                rows.extend(CategoryRow(row=row, status="complete") for row in group.rows)

        for family in self.might_by_family():
            rows = combined.setdefault(family.family, [])
            for group in family.groups:
                rows.extend(
                    CategoryRow(
                        row=row,
                        status="waiting",
                        waiting_label=group.blocked_label,
                        question=group.question,
                        is_askable_later=group.is_askable_later,
                    )
                    for row in group.rows
                )

        groups = [
            CategoryGroup(
                family=family,
                rows=tuple(
                    sorted(rows, key=lambda item: (item.status != "complete", item.row.title))
                ),
            )
            for family, rows in combined.items()
        ]
        return sorted(groups, key=lambda group: (-group.count, group.family))

    @property
    def max_question_unlocks(self) -> int:
        """The busiest question in the queue, for scaling the "settles" meter.

        A bare count reads fine for the top question and means nothing for the
        rest, since there is nothing to compare it to. Scaled against the
        highest ``unlocks`` on screen, the meter shows what the number already
        says: this question is worth answering *first*.
        """
        return max((question.unlocks for question in self.questions), default=0)


def _group(
    rows: tuple[PreviewRow, ...],
    *,
    key: Callable[[PreviewRow], str],
    questions: dict[str, Question] | None = None,
) -> list[FamilyGroup]:
    lookup = questions or {}
    families: dict[str, dict[str, list[PreviewRow]]] = {}
    for row in rows:
        families.setdefault(row.family or "Other", {}).setdefault(key(row), []).append(row)

    result = [
        FamilyGroup(
            family=family,
            groups=tuple(
                ReasonGroup(
                    reason=reason,
                    rows=tuple(group),
                    blocked_on=group[0].blocked_on,
                    question=lookup.get(group[0].blocked_on),
                )
                # Largest group first: the shared explanation covers the most
                # ground, and the odd ones out fall to the bottom where they
                # stand out rather than being buried in the middle.
                for reason, group in sorted(
                    by_reason.items(), key=lambda pair: (-len(pair[1]), pair[0])
                )
            ),
        )
        for family, by_reason in families.items()
    ]
    return sorted(result, key=lambda family: (-family.count, family.family))


def for_preview(profile: EntityProfileView) -> EntityProfileView:
    """Soften "no registrations recorded" into "not answered yet", for this
    screen only.

    ``build_profile_view`` sets ``registrations`` (and ``premises_types``) to an
    empty list even when none exist — correct for the nightly materialisation
    run, where an entity that has been through setup and genuinely holds no
    GST registration should have every GST filing settle to FALSE rather than
    sit forever as an unconfirmed row on the calendar. It is the wrong reading
    the moment an entity is created and has not had the *chance* to add its
    first registration yet: every rule gated on ``registrations includes X``
    reads as a confident "doesn't apply", which is a false negative dressed up
    as an answer.

    Dropping the fact entirely — rather than leaving the empty list — is what
    turns that back into ``UNKNOWN`` in three-valued evaluation, matching what
    the retired onboarding draft did by simply never setting the fact until an
    identifier was typed. Once a real registration or premises exists this is
    a no-op: ``profile.registrations``/``profile.premises`` are no longer
    empty, so nothing here fires and the ordinary, decided reading applies.

    Preview-only. The materialisation pipeline (``rebuild_calendar`` →
    ``materialise`` → ``preview_for_profile``) keeps building its own
    ``EntityProfileView`` straight from ``build_profile_view`` and never sees
    this — what actually lands on the calendar is unaffected.
    """
    facts = dict(profile.facts)
    if not profile.registrations:
        facts.pop("registrations", None)
    if not profile.premises:
        facts.pop("premises_types", None)
        facts.pop("has_factory_premises", None)
    return replace(profile, facts=facts)


def preview_entity(
    profile: EntityProfileView, *, country: str, question_limit: int = 8
) -> EntityPreview:
    """Partition the catalog against one entity's current profile.

    Kleene evaluation is what makes this honest: knowing only a country, a legal
    form and a state, ``all`` still short-circuits to FALSE wherever the legal
    form settles it, so the "doesn't apply" column is real rather than empty,
    while everything resting on a fact nobody has supplied lands in "might apply"
    rather than being silently dropped.
    """
    catalog = build_catalog(country=country, jurisdictions=profile.jurisdictions)

    applies: list[PreviewRow] = []
    might: list[PreviewRow] = []
    excluded: list[PreviewRow] = []

    for definition in catalog:
        verdict = evaluate(definition.applicability_rule, profile.facts)
        reasons = verdict.reasons()
        row = PreviewRow(
            code=definition.code,
            title=definition.title,
            family=_family_of(definition),
            category=definition.category,
            reason=reasons[0] if reasons else "",
            blocked_on=_first_askable(verdict.missing_facts),
        )
        if verdict.result is V.TRUE:
            applies.append(row)
        elif verdict.result is V.UNKNOWN:
            might.append(row)
        else:
            excluded.append(row)

    return EntityPreview(
        # Not truncated. The counts are read off these, and they feed the button
        # that builds the calendar — a number short of the truth there is the
        # single most damaging thing on this screen.
        applies=tuple(applies),
        might_apply=tuple(might),
        does_not_apply=tuple(excluded),
        # Still-open questions first, ranked by consequence; already-answered
        # ones after, so a person can find and change one without it crowding
        # out what is still genuinely unknown.
        questions=rank_questions(catalog=catalog, facts=profile.facts, limit=question_limit)
        + answered_questions(catalog=catalog, facts=profile.facts),
        considered=len(catalog),
    )


def _family_of(definition: DefinitionSnapshot) -> str:
    # DefinitionSnapshot carries the category but not the family, and adding a
    # field to the frozen type for a display concern would be the wrong trade.
    # The category is a serviceable grouping until the browser needs better.
    return definition.category.replace("_", " ").title()


def _first_askable(missing: frozenset[str]) -> str:
    """The key of the fact to ask for, or ``""`` if none of them is askable.

    A **key**, not a label, and that is the whole of the change. It used to
    return ``definition.label``, which meant the value could never be matched
    against a question — ``rank_questions`` is keyed on fact names — so the
    column could only ever print a phrase at somebody and leave them to work out
    which control cleared it. The screen had both halves of the answer and no way
    to join them.

    ``askable_facts`` rather than the raw set, for the second half of the same
    problem: a derived fact has no input of its own (nobody types a turnover
    *band*, they type a turnover), and a fact sourced from a registration or a
    premises is not a question at all. Naming those was how the column filled up
    with things like "Registrations held", which is not something anybody can
    answer.
    """
    for key in sorted(askable_facts(missing)):
        if REGISTRY.get(key) is not None:
            return key
    return ""


# ---------------------------------------------------------------------------
# Packs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackSuggestion:
    pack: CompliancePack
    #: How many of the pack's definitions are *not* already applying. A pack that
    #: suggests what you already have is noise.
    adds: int
    reasons: tuple[str, ...] = ()
    accepted: bool = False


def suggest_packs(
    profile: EntityProfileView,
    preview: EntityPreview,
    *,
    country: str,
    accepted: frozenset[str] = frozenset(),
) -> list[PackSuggestion]:
    """Packs worth offering, ranked by how much they would actually add."""
    already = {row.code for row in preview.applies}

    suggestions: list[PackSuggestion] = []
    for pack in CompliancePack.objects.filter(country=country, is_active=True):
        verdict = evaluate(pack.suggestion_rule, profile.facts)
        if verdict.result is V.FALSE:
            continue
        adds = len([code for code in pack.definition_codes if code not in already])
        if not adds:
            continue
        suggestions.append(
            PackSuggestion(
                pack=pack,
                adds=adds,
                reasons=tuple(verdict.reasons()),
                accepted=pack.code in accepted,
            )
        )

    suggestions.sort(key=lambda suggestion: (-suggestion.adds, suggestion.pack.name))
    return suggestions


def adopt_pack(tenant: Tenant, entity: Entity, code: str, *, user: User) -> None:
    """Expand one accepted pack into inclusion rows.

    One row per definition rather than one subscription row per pack. Revoking
    the pack later revokes its rows (:func:`revoke_pack`), so nothing downstream
    needs to know packs exist, and a user can drop a single definition out of a
    pack they otherwise want.
    """
    pack = CompliancePack.objects.filter(code=code, is_active=True).first()
    if pack is None:
        return
    for definition_code in pack.definition_codes:
        ObligationInclusion.objects.get_or_create(
            entity=entity,
            definition_code=definition_code,
            revoked_at=None,
            defaults={
                "tenant": tenant,
                "source": ObligationInclusion.Source.PACK,
                "pack_code": pack.code,
                "reason": f"Added with the {pack.name} pack.",
                "added_by": user,
            },
        )


def revoke_pack(entity: Entity, code: str) -> None:
    """The inverse of :func:`adopt_pack` — revoke every row it added for this pack.

    A definition the user separately opted into by hand (``Source.USER``) is left
    alone even if it happens to share a code with the pack: dropping the pack must
    not also drop a choice the user made on their own.
    """
    ObligationInclusion.objects.filter(
        entity=entity,
        pack_code=code,
        source=ObligationInclusion.Source.PACK,
        revoked_at__isnull=True,
    ).update(revoked_at=timezone.now())
