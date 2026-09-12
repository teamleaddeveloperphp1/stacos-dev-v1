"""
What the wizard shows, and what it writes.

Two halves. :func:`preview_draft` answers "what would apply to me" against a
draft that has never been saved, which is the whole point of the wizard.
:func:`commit_draft` turns the draft into real rows in one transaction and builds
the calendar immediately, because a user who has just answered ten questions
should not be told to come back tomorrow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from stacos.accounts.models import User
from stacos.catalog.models import CompliancePack
from stacos.catalog.snapshots import build_catalog
from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.core.scope import tenant_context
from stacos.engine.rules import V, evaluate
from stacos.engine.types import DefinitionSnapshot
from stacos.jurisdictions.facts import REGISTRY
from stacos.obligations.models import MaterialisationRun, ObligationInclusion
from stacos.obligations.services import materialise
from stacos.tenancy.models import (
    Entity,
    EntityFactValue,
    EntityPremises,
    EntityProfile,
    EntityRegistration,
    Tenant,
)
from stacos.tenancy.onboarding.questions import Question, askable_facts, rank_questions
from stacos.tenancy.onboarding.state import OnboardingDraft
from stacos.tenancy.services import provision_tenant

__all__ = [
    "CategoryGroup",
    "CategoryRow",
    "DraftPreview",
    "FamilyGroup",
    "PackSuggestion",
    "PreviewRow",
    "ReasonGroup",
    "commit_draft",
    "preview_draft",
    "suggest_packs",
    "total_obligation_count",
]

#: How many rows to show under a heading before folding the rest away.
#:
#: A display cap and nothing more. It used to be applied to the *stored* tuples,
#: which meant ``applies_count`` — and therefore the number on the button
#: somebody presses to create their calendar — reported 60 for a business with
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
class DraftPreview:
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
        blocked-on/question resolution already covered by
        ``tests/wave1/test_preview_grouping.py`` stay the single source of truth
        — this only reshapes their output into one row per obligation with its
        own status pill, applies first.
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


def preview_draft(draft: OnboardingDraft, *, question_limit: int = 8) -> DraftPreview:
    """Partition the catalog for a draft that has never been saved.

    Kleene evaluation is what makes this honest and what makes it possible so
    early: knowing only a country, a legal form and a state, ``all`` still
    short-circuits to FALSE wherever the legal form settles it, so the
    "doesn't apply" column is real rather than empty, while everything resting on
    a fact nobody has supplied lands in "might apply" rather than being silently
    dropped.
    """
    profile = draft.to_profile_view()
    catalog = build_catalog(country=draft.country, jurisdictions=profile.jurisdictions)

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

    return DraftPreview(
        # Not truncated. The counts are read off these, and they feed the button
        # that creates the calendar — a number short of the truth there is the
        # single most damaging thing on this screen.
        applies=tuple(applies),
        might_apply=tuple(might),
        does_not_apply=tuple(excluded),
        questions=rank_questions(catalog=catalog, facts=profile.facts, limit=question_limit),
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


def suggest_packs(draft: OnboardingDraft, preview: DraftPreview) -> list[PackSuggestion]:
    """Packs worth offering, ranked by how much they would actually add."""
    profile = draft.to_profile_view()
    already = {row.code for row in preview.applies}

    suggestions: list[PackSuggestion] = []
    for pack in CompliancePack.objects.filter(country=draft.country, is_active=True):
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
                accepted=pack.code in draft.packs,
            )
        )

    suggestions.sort(key=lambda suggestion: (-suggestion.adds, suggestion.pack.name))
    return suggestions


def total_obligation_count(preview: DraftPreview, packs: list[PackSuggestion]) -> int:
    """What ``commit_draft`` will actually create — rules, plus accepted packs.

    ``preview.applies_count`` alone used to be the number on the "Create my
    calendar" button, so accepting or dropping a pack changed what the wizard
    was about to build without changing the number promising what it would
    build. A union of codes rather than summing each pack's own ``adds``:
    two accepted packs that both cover the same definition must not be
    counted twice.
    """
    already = {row.code for row in preview.applies}
    added_by_packs = {
        code
        for suggestion in packs
        if suggestion.accepted
        for code in suggestion.pack.definition_codes
        if code not in already
    }
    return preview.applies_count + len(added_by_packs)


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


@transaction.atomic
def commit_draft(
    draft: OnboardingDraft,
    *,
    user: User,
    as_of: date,
    tenant: Tenant | None = None,
) -> Entity:
    """Turn the draft into real rows, and build the calendar before returning.

    One transaction, and the ordering matters: the tenant has to exist before a
    scope can be opened, and the scope has to be open before anything
    tenant-scoped is written.

    ``tenant`` names an organisation that already exists, so the wizard adds an
    entity to a workspace the user already owns instead of minting a second
    organisation for their first entity — the path for somebody adding an entity
    from the tenant switcher. Left ``None`` — the ordinary case, since sign-up no
    longer provisions a tenant itself — one is provisioned here.

    Materialisation runs synchronously at the end rather than being queued. The
    user has just spent five minutes answering questions; showing them the answer
    is the entire payoff, and "your calendar will appear overnight" throws it
    away. If this ever gets slow enough to notice — a six-state retailer produces
    a few thousand rows — the fix is a "building your calendar" screen backed by
    a task, not a silent wait.
    """
    if not draft.is_ready_to_commit:
        raise ValueError("The draft needs at least a name, a legal form and a state.")

    if tenant is None:
        tenant = provision_tenant(
            draft.name, owner=user, country=draft.country, reason="onboarding"
        )

    with tenant_context(tenant_ids={tenant.id}, reason="onboarding"):
        entity = Entity(
            tenant=tenant,
            name=draft.name,
            legal_name=draft.legal_name or draft.name,
            short_code=_short_code(draft.name),
            entity_type=draft.entity_type,
            country=draft.country,
            registered_office_state=draft.registered_office_state,
            incorporation_date=_as_date(draft.incorporation_date),
        )
        entity.full_clean(exclude=["tenant"])
        entity.save()

        EntityProfile.objects.create(
            tenant=tenant,
            entity=entity,
            states_of_operation=sorted(draft.jurisdictions),
            completed_at=timezone.now(),
            facts=_profile_facts(draft.answers),
            **_profile_columns(draft.answers),
        )

        for row in draft.registrations:
            if not row.value:
                continue
            registration = EntityRegistration(
                tenant=tenant,
                entity=entity,
                type=row.type,
                value=row.value,
                jurisdiction=row.jurisdiction,
            )
            # full_clean, so validate_registration_value runs. A GSTIN the
            # decoder happened to parse but whose checksum is wrong must not
            # reach the database because decoding is more forgiving than
            # validation.
            registration.full_clean(exclude=["tenant", "entity"])
            registration.save()

        EntityPremises.objects.create(
            tenant=tenant,
            entity=entity,
            name="Registered office",
            type="REGISTERED_OFFICE",
            jurisdiction=draft.registered_office_state,
        )

        _write_fact_history(tenant, entity, draft.answers, user=user, as_of=as_of)
        _adopt_packs(tenant, entity, draft.packs, user=user)

        # The tenant's own CREATE row is written by `provision_tenant`, which is
        # the only thing that creates one.
        record_event(action=AuditAction.CREATE, actor=user, obj=entity)

        materialise(
            entity,
            as_of=as_of,
            trigger=MaterialisationRun.Trigger.ONBOARDING,
            actor=user,
        )

    return entity


#: Profile facts that have their own typed column rather than living in the
#: JSONB. ``women_employees_count`` and ``net_profit`` are registered,
#: askable facts (``jurisdictions/facts.py``) with no matching column on
#: ``EntityProfile`` — passing either to ``EntityProfile.objects.create()``
#: raises ``TypeError`` for an unexpected keyword argument the moment a user
#: answers that question. Every other caller that builds an ``EntityProfile``
#: (the ``manufacturer`` test fixture, ``seed_dev``, ``catalog.personas``)
#: already puts both in ``facts``, never as a column — this brings onboarding
#: in line with that instead of inventing a third convention.
_PROFILE_COLUMNS = frozenset(
    {
        "aggregate_turnover",
        "employee_count",
        "contractor_count",
        "paid_up_capital",
        "net_worth",
        "nic_code",
        "sector",
        "sub_sector",
    }
)

#: The three ``DecimalField(max_digits=18, decimal_places=2)`` columns above —
#: 16 integer digits, so a magnitude of ``10**16`` or more overflows them.
_DECIMAL_COLUMNS = frozenset({"aggregate_turnover", "paid_up_capital", "net_worth"})
_DECIMAL_COLUMN_LIMIT = Decimal(10) ** 16


def _profile_columns(answers: dict[str, Any]) -> dict[str, Any]:
    columns = {key: value for key, value in answers.items() if key in _PROFILE_COLUMNS}
    for key in _DECIMAL_COLUMNS:
        value = columns.get(key)
        # `QuestionForm` (`stacos.tenancy.forms`) now rejects a value this
        # large at the point of answering, but a draft can carry one from
        # before that cap existed — the wizard never re-asks a settled
        # question, so it would otherwise sit in the session and turn every
        # future "Create my calendar" into `psycopg.errors.NumericValueOutOfRange`.
        # Dropped rather than clamped: a made-up ceiling value is not a
        # turnover anyone typed, and the field simply going back to "not
        # answered" is honest about what we actually know.
        if value is not None and abs(Decimal(str(value))) >= _DECIMAL_COLUMN_LIMIT:
            columns.pop(key)
    return columns


def _profile_facts(answers: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in answers.items() if key not in _PROFILE_COLUMNS}


def _write_fact_history(
    tenant: Tenant, entity: Entity, answers: dict[str, Any], *, user: User, as_of: date
) -> None:
    """Record effective-dated answers with an honest validity window.

    Easy to skip, and wrong to skip. ``build_profile_view`` layers historical
    values *over* the profile columns, so writing only the column makes today's
    answer silently true for every period that ever was — precisely the failure
    ``EntityFactValue`` exists to prevent. A turnover answered today is a
    statement about the current financial year, and saying so is the difference
    between a retrospective explanation that is true and one that is convenient.
    """
    effective_dated = REGISTRY.effective_dated_keys()
    year_start = date(as_of.year if as_of.month >= 4 else as_of.year - 1, 4, 1)

    for key, value in answers.items():
        if key not in effective_dated or value in (None, ""):
            continue
        EntityFactValue.objects.create(
            tenant=tenant,
            entity=entity,
            key=key,
            value=value,
            valid_from=year_start,
            source="USER",
            recorded_by=user,
        )


def _adopt_packs(tenant: Tenant, entity: Entity, packs: tuple[str, ...], *, user: User) -> None:
    """Expand accepted packs into inclusion rows.

    One row per definition rather than one subscription row per pack. Removing a
    pack later revokes its rows, so nothing downstream needs to know packs exist,
    and a user can drop a single definition out of a pack they otherwise want.
    """
    if not packs:
        return
    for pack in CompliancePack.objects.filter(code__in=packs, is_active=True):
        for code in pack.definition_codes:
            ObligationInclusion.objects.get_or_create(
                entity=entity,
                definition_code=code,
                revoked_at=None,
                defaults={
                    "tenant": tenant,
                    "source": ObligationInclusion.Source.PACK,
                    "pack_code": pack.code,
                    "reason": f"Added with the {pack.name} pack.",
                    "added_by": user,
                },
            )


def _short_code(name: str) -> str:
    initials = "".join(word[0] for word in name.split() if word[0].isalnum()).upper()
    return (initials or slugify(name).upper().replace("-", ""))[:12]


def _as_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
