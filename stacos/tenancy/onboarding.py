"""
What this workspace should be asked to do next.

A product like this one has a first day that looks nothing like its second. On
day one there is no entity, no calendar and nothing to be overdue, and the
dashboard that is genuinely useful afterwards — five counters, four charts and a
list — is on day one five zeros, four empty panels and an empty list. Showing it
anyway is how a new user concludes the product is broken; redirecting straight
into the Add Entity form instead, which is what this replaced, is how they
conclude it is a form.

So the dashboard asks this module what state the workspace is in and renders
accordingly. The states, in the order a workspace passes through them:

``NAME_WORKSPACE``
    Signed up, workspace still carrying the name we derived from the person's
    own. Never blocking — it is offered alongside the real first step, not in
    front of it, because naming the organisation is housekeeping and adding an
    entity is the product.
``ADD_ENTITY``
    No entity yet. The one thing to do.
``FINISH_SETUP``
    An entity exists but has never had a calendar built. Somebody started and
    stopped — closed the tab, lost the connection, ran out of the answers they
    had to hand. Their progress is on file; this is what offers it back.
``TRACK``
    At least one calendar exists. The ordinary dashboard, and nothing from this
    module renders.

Deliberately cheap: two queries, both of which the dashboard needs anyway, and
an ``Exists`` subquery rather than a second aggregate — annotating a ``Count``
over materialisation runs beside the existing ``Count`` over obligations makes
both wrong, because the two joins multiply.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from django.db.models import Exists, OuterRef, QuerySet
from django.urls import reverse

from stacos.tenancy.models import Entity, Tenant
from stacos.tenancy.services import name_is_provisional

__all__ = ["Stage", "WorkspaceState", "entities_with_calendar_flag", "workspace_state"]


class Stage(StrEnum):
    ADD_ENTITY = "add_entity"
    FINISH_SETUP = "finish_setup"
    TRACK = "track"


#: The label and one-line explanation for each step of the guided setup, so the
#: "you are on step 2 of 4" line on the dashboard and the rail inside the flow
#: cannot describe the same step differently.
SETUP_STEP_ORDER: tuple[str, ...] = tuple(choice.value for choice in Entity.SetupStep)


def entities_with_calendar_flag(queryset: QuerySet[Entity] | None = None) -> QuerySet[Entity]:
    """``queryset`` with a ``has_calendar`` boolean attached to each row.

    "Has a calendar" is "has ever been through a materialisation run", not "owns
    at least one obligation". The difference matters for an entity whose first
    build honestly produces nothing — no registrations recorded yet, say — which
    an obligation count would send back into setup forever. The same test
    ``entity_detail`` and ``entity_preview_context`` already make, for the same
    reason.
    """
    from stacos.obligations.models import MaterialisationRun

    base = queryset if queryset is not None else Entity.objects.filter(archived_at__isnull=True)
    return base.annotate(
        has_calendar=Exists(MaterialisationRun.objects.filter(entity=OuterRef("pk")))
    )


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    """Where this workspace is in its own setup, and what to offer next."""

    entity_count: int
    live_count: int
    #: Entities that exist but have never had a calendar built, in the order
    #: they should be finished — oldest first, because that is the one somebody
    #: actually started.
    pending: tuple[Entity, ...]
    name_is_provisional: bool
    tenant: Tenant | None

    @property
    def stage(self) -> Stage:
        if self.entity_count == 0:
            return Stage.ADD_ENTITY
        if self.live_count == 0:
            return Stage.FINISH_SETUP
        return Stage.TRACK

    @property
    def is_first_run(self) -> bool:
        """True while nothing in this workspace is being tracked yet.

        The dashboard's charts and counters are all downstream of a calendar, so
        this is the single test for "render the guided state instead".
        """
        return self.stage is not Stage.TRACK

    @property
    def resume_entity(self) -> Entity | None:
        return self.pending[0] if self.pending else None

    @property
    def resume_url(self) -> str:
        """Where "continue" goes: the step that entity actually stopped on."""
        entity = self.resume_entity
        if entity is None:
            return reverse("app:entity_create")
        return setup_step_url(entity)

    @property
    def resume_step_number(self) -> int:
        entity = self.resume_entity
        if entity is None:
            return 0
        try:
            return SETUP_STEP_ORDER.index(entity.setup_step) + 1
        except ValueError:
            # A step name retired by a later version, read off a row written by
            # an earlier one. Start at the beginning rather than raising: every
            # step is re-derived from the database, so re-walking them is
            # harmless and losing the page is not.
            return 1

    @property
    def setup_step_total(self) -> int:
        return len(SETUP_STEP_ORDER)


def setup_step_url(entity: Entity) -> str:
    """The guided-setup URL for the step ``entity`` last reached."""
    step = entity.setup_step if entity.setup_step in SETUP_STEP_ORDER else SETUP_STEP_ORDER[0]
    return reverse(f"app:entity_setup_{step}", args=[entity.pk])


def workspace_state(tenant: Tenant | None) -> WorkspaceState:
    """Resolve the workspace's onboarding state in one query.

    ``pending`` is capped: a workspace with forty half-finished entities has a
    different problem from the one this is for, and the dashboard card lists a
    few and links to the entity list for the rest.
    """
    rows = list(
        entities_with_calendar_flag()
        .only("id", "name", "entity_type", "setup_step", "tenant")
        .order_by("created_at")
    )
    # `has_calendar` is an `Exists` annotation, not a model field — mypy has no
    # way to know `entities_with_calendar_flag` attaches it to every row.
    live_flags = [bool(entity.has_calendar) for entity in rows]  # type: ignore[attr-defined]
    pending = tuple(entity for entity, live in zip(rows, live_flags, strict=True) if not live)[:5]
    return WorkspaceState(
        entity_count=len(rows),
        live_count=sum(live_flags),
        pending=pending,
        name_is_provisional=bool(tenant and name_is_provisional(tenant)),
        tenant=tenant,
    )
