"""
What the wizard shows, and what it writes.

Two halves. :func:`preview_draft` answers "what would apply to me" against a
draft that has never been saved, which is the whole point of the wizard.
:func:`commit_draft` turns the draft into real rows in one transaction and builds
the calendar immediately, because a user who has just answered ten questions
should not be told to come back tomorrow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
from stacos.tenancy.onboarding.questions import Question, rank_questions
from stacos.tenancy.onboarding.state import OnboardingDraft
from stacos.tenancy.services import provision_tenant

__all__ = [
    "DraftPreview",
    "PackSuggestion",
    "commit_draft",
    "preview_draft",
    "suggest_packs",
]

#: How many definitions to name in each column before saying "and N more".
_SHOWN_PER_GROUP = 60


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
class DraftPreview:
    """Three columns, and the questions that would move rows between them."""

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

    def applies_by_family(self) -> list[tuple[str, list[PreviewRow]]]:
        """Grouped by family, not category.

        ``family`` is the vocabulary a CA already speaks — ROC, GST, TDS,
        Professional tax. ``category`` is the axis engagements are scoped along
        and reads as jargon on a screen a business owner is looking at.
        """
        return _group(self.applies)

    def might_by_family(self) -> list[tuple[str, list[PreviewRow]]]:
        return _group(self.might_apply)


def _group(rows: tuple[PreviewRow, ...]) -> list[tuple[str, list[PreviewRow]]]:
    grouped: dict[str, list[PreviewRow]] = {}
    for row in rows:
        grouped.setdefault(row.family or "Other", []).append(row)
    return sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0]))


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
        applies=tuple(applies[:_SHOWN_PER_GROUP]),
        might_apply=tuple(might[:_SHOWN_PER_GROUP]),
        does_not_apply=tuple(excluded[:_SHOWN_PER_GROUP]),
        questions=rank_questions(catalog=catalog, facts=profile.facts, limit=question_limit),
        considered=len(catalog),
    )


def _family_of(definition: DefinitionSnapshot) -> str:
    # DefinitionSnapshot carries the category but not the family, and adding a
    # field to the frozen type for a display concern would be the wrong trade.
    # The category is a serviceable grouping until the browser needs better.
    return definition.category.replace("_", " ").title()


def _first_askable(missing: frozenset[str]) -> str:
    for key in sorted(missing):
        definition = REGISTRY.get(key)
        if definition is not None:
            return definition.label
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

    ``tenant`` names an organisation that already exists — which is the ordinary
    case now that sign-up provisions one from the Organisation Name. Running the
    wizard then adds an entity to the workspace the user already owns; creating a
    second organisation for their first entity is the bug that argument exists to
    prevent. Left ``None``, one is provisioned here, which is the path for
    somebody adding a second organisation from the tenant switcher.

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


#: Profile facts that have their own typed column rather than living in the JSONB.
_PROFILE_COLUMNS = frozenset(
    {
        "aggregate_turnover",
        "employee_count",
        "contractor_count",
        "women_employees_count",
        "paid_up_capital",
        "net_worth",
        "net_profit",
        "nic_code",
        "sector",
        "sub_sector",
    }
)


def _profile_columns(answers: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in answers.items() if key in _PROFILE_COLUMNS}


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
