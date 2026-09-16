"""
The guided path from "an entity now exists" to "its calendar exists".

Four steps, the third conditional: review the registrations the Add Entity
screen already collected (and add any it did not); answer the ranked
questions that settle the most; opt into any optional packs suggested for
this entity, if there are any; then review the full result and create the
calendar. A brand-new entity has nowhere near enough reason to justify the
entity detail page's five cards at once — showing them all together the
moment an entity exists is how a user ends up staring at a Compliance card
with no idea it was the thing to do *next*. This says which one thing to do
now, in order, and shows what comes after only once this one is behind them.

Each step is its own page showing only the part of the Compliance card
(`obligations/_fragments/entity_summary.html`) relevant to the decision it is
asking for — the headline counts, the question queue, the pack strip, the
category breakdown — controlled by the `hide_questions`/`hide_columns`/
`hide_packs` flags on `stacos.obligations.views.entity_preview_context`.
Interleaving all of them on one screen, which is what this flow used to do,
is what made answering one question feel like it was rearranging a page of
unrelated content underneath the user.

``tenancy.views.entity_detail`` redirects here — always to step one — for as
long as the entity owns no obligations at all; once a calendar exists even
once, it never redirects again. This is a first-run path, not a permanent
fixture: adding a further registration, answering a question, opting into a
pack, or rebuilding the calendar afterwards all still happen on the ordinary
entity detail page, exactly as they did before this existed.
"""

from __future__ import annotations

from datetime import date

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.htmx import derive_fragment_template, is_fragment_request
from stacos.core.permissions import require_permission
from stacos.obligations.views import entity_preview_context
from stacos.tenancy.models import Entity
from stacos.tenancy.onboarding import SETUP_STEP_ORDER

#: One entry per step in the rail. The key is also the url name suffix
#: (`app:entity_setup_<key>`) and the fragment's directory name. "packs" is
#: dropped from the rendered rail — see `_step_context` — whenever this
#: entity has no pack suggestions at all; the page itself is skipped the same
#: way, by `packs()` redirecting straight past it.
STEPS = (
    ("registrations", _("Registrations")),
    ("answers", _("Answer what applies")),
    ("packs", _("Optional add-ons")),
    ("build", _("Review & create")),
)


def _today() -> date:
    return timezone.localdate()


def _entity_or_404(pk: str) -> Entity:
    entity = Entity.objects.filter(pk=pk, archived_at__isnull=True).first()
    if entity is None:
        # 404, not 403: confirming that an entity exists in another tenant is
        # itself a disclosure — the same reason `entity_detail` gives.
        raise Http404
    return entity  # type: ignore[no-any-return]


def _template(request: HttpRequest, page: str) -> str:
    """The page, or just its body — the same dual-render idiom every other
    view in the project uses; see ``stacos.core.htmx``.
    """
    return derive_fragment_template(page) if is_fragment_request(request) else page


def _step_context(step: str, entity: Entity, *, has_packs: bool) -> dict[str, object]:
    steps = tuple((key, label) for key, label in STEPS if key != "packs" or has_packs)
    return {"steps": steps, "current_step": step, "entity": entity}


def _mark_reached(entity: Entity, step: str) -> None:
    """Remember this as the furthest step reached, so a closed browser or a
    lost connection resumes here rather than at step one.

    Never moves backwards: clicking "Back" to recheck a registration must not
    make the dashboard's "resume" link forget that the questions were already
    reached. Compared by position in :data:`SETUP_STEP_ORDER` rather than by
    string, since "packs" sorts after "answers" alphabetically by accident, not
    by design, and a future step added to the rail should not have to keep that
    coincidence true.
    """
    try:
        new_index = SETUP_STEP_ORDER.index(step)
        current_index = SETUP_STEP_ORDER.index(entity.setup_step)
    except ValueError:
        return
    if new_index > current_index:
        entity.setup_step = step
        entity.save(update_fields=["setup_step", "updated_at"])


@require_permission("tenancy.entity.view")
def registrations(request: HttpRequest, pk: str) -> HttpResponse:
    """Step one: what the Add Entity screen already recorded, and anything else."""
    entity = _entity_or_404(pk)
    _mark_reached(entity, "registrations")
    has_packs = bool(entity_preview_context(entity, as_of=_today())["packs"])
    context = {
        **_step_context("registrations", entity, has_packs=has_packs),
        "registrations": entity.registrations.filter(archived_at__isnull=True),
    }
    return render(request, _template(request, "tenancy/setup/registrations.html"), context)


@require_permission("tenancy.entity.view")
def answers(request: HttpRequest, pk: str) -> HttpResponse:
    """Step two: the ranked question queue, on its own.

    Shown beside the headline counts alone — no category breakdown, no pack
    strip — so answering a question is felt as "the counts moved" rather than
    as a page of unrelated content reflowing at the same time. See
    ``obligations.views.entity_preview_context``'s ``hide_columns``/
    ``hide_packs``.
    """
    entity = _entity_or_404(pk)
    _mark_reached(entity, "answers")
    preview_context = entity_preview_context(
        entity,
        as_of=_today(),
        hide_build=True,
        in_setup=True,
        hide_columns=True,
        hide_packs=True,
    )
    context = {
        **_step_context("answers", entity, has_packs=bool(preview_context["packs"])),
        **preview_context,
        "back_url": reverse("app:entity_setup_registrations", args=[entity.pk]),
    }
    return render(request, _template(request, "tenancy/setup/answers.html"), context)


@require_permission("tenancy.entity.view")
def packs(request: HttpRequest, pk: str) -> HttpResponse:
    """Step three: optional packs, skipped entirely when there is nothing to offer.

    Not gated on anything answered in step two — a suggestion list with
    nothing in it is not a step somebody should have to click through, it is
    an empty page. ``suggest_packs`` is re-evaluated fresh here regardless of
    how the user arrived (forward from step two, back from step four,
    a bookmark), so a stale "nothing to suggest" never strands a real
    suggestion, and a stale suggestion never strands an empty step.
    """
    entity = _entity_or_404(pk)
    _mark_reached(entity, "packs")
    preview_context = entity_preview_context(
        entity,
        as_of=_today(),
        hide_build=True,
        in_setup=True,
        hide_questions=True,
        hide_columns=True,
    )
    if not preview_context["packs"]:
        return redirect("app:entity_setup_build", pk=entity.pk)

    context = {
        **_step_context("packs", entity, has_packs=True),
        **preview_context,
        "back_url": reverse("app:entity_setup_answers", args=[entity.pk]),
    }
    return render(request, _template(request, "tenancy/setup/packs.html"), context)


@require_permission("tenancy.entity.view")
def build(request: HttpRequest, pk: str) -> HttpResponse:
    """Step four, and the end of the flow: the full reviewed result, and the
    button that creates the calendar.

    Neither the question queue nor the pack strip render here — both belong to
    the steps behind this one, so what is left is a stable snapshot to review
    rather than a screen still inviting more editing. See
    ``obligations.views.entity_preview_context``'s ``hide_questions``/
    ``hide_packs``.

    No separate submit handler here — the button posts to ``compliance:rebuild``
    like the steady-state entity page's does. What makes it the *end* of the
    flow rather than a dead end is ``?finish_setup=1``, which the template puts
    on it: the response then answers with ``HX-Location`` to the dashboard
    instead of a re-rendered card.

    ``hide_build=True`` because the button is not in the card at all — it is in
    the wizard footer, where every earlier step of the rail put its forward
    action, and where a user who has scrolled to the bottom of a wizard is
    actually looking for it. Keeping it out of the card also keeps it out of
    the region that re-renders itself on every answered question, which is
    what used to drop ``finish_setup``. See
    ``tenancy/setup/_fragments/build_body.html``.
    """
    entity = _entity_or_404(pk)
    _mark_reached(entity, "build")
    preview_context = entity_preview_context(
        entity,
        as_of=_today(),
        hide_build=True,
        in_setup=True,
        hide_questions=True,
        hide_packs=True,
    )
    has_packs = bool(preview_context["packs"])
    back_step = "app:entity_setup_packs" if has_packs else "app:entity_setup_answers"
    context = {
        **_step_context("build", entity, has_packs=has_packs),
        **preview_context,
        "back_url": reverse(back_step, args=[entity.pk]),
    }
    return render(request, _template(request, "tenancy/setup/build.html"), context)
