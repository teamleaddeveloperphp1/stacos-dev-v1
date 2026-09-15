"""
The guided path from "an entity now exists" to "its calendar exists".

Three pages, each one step in a rail: review the registrations the Add Entity
screen already collected (and add any it did not), see what applies, build
the calendar. A brand-new entity has nowhere near enough reason to justify the
entity detail page's five cards at once — showing them all together the
moment an entity exists is how a user ends up staring at a Compliance card
with no idea it was the thing to do *next*. This says which one thing to do
now, in order, and shows what comes after only once this one is behind them.

``tenancy.views.entity_detail`` redirects here — always to step one — for as
long as the entity owns no obligations at all; once a calendar exists even
once, it never redirects again. This is a first-run path, not a permanent
fixture: adding a further registration, answering a question, or rebuilding
the calendar afterwards all still happen on the ordinary entity detail page,
exactly as they did before this existed.
"""

from __future__ import annotations

from datetime import date

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.htmx import derive_fragment_template, is_fragment_request
from stacos.core.permissions import require_permission
from stacos.obligations.views import entity_preview_context
from stacos.tenancy.models import Entity

#: One entry per step in the rail. The key is also the url name suffix
#: (`app:entity_setup_<key>`) and the fragment's directory name.
STEPS = (
    ("registrations", _("Registrations")),
    ("preview", _("What applies")),
    ("build", _("Build calendar")),
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


def _step_context(step: str, entity: Entity) -> dict[str, object]:
    return {"steps": STEPS, "current_step": step, "entity": entity}


@require_permission("tenancy.entity.view")
def registrations(request: HttpRequest, pk: str) -> HttpResponse:
    """Step one: what the Add Entity screen already recorded, and anything else."""
    entity = _entity_or_404(pk)
    context = {
        **_step_context("registrations", entity),
        "registrations": entity.registrations.filter(archived_at__isnull=True),
    }
    return render(request, _template(request, "tenancy/setup/registrations.html"), context)


@require_permission("tenancy.entity.view")
def preview(request: HttpRequest, pk: str) -> HttpResponse:
    """Step two: the same Compliance card the entity detail page shows later,
    its build button hidden — see ``obligations.views.entity_preview_context``.
    Answering a question or toggling a pack here re-renders through the exact
    same endpoints the steady-state card uses, carrying ``?hide_build=1`` so
    the button stays hidden across the exchange.
    """
    entity = _entity_or_404(pk)
    context = {
        **_step_context("preview", entity),
        **entity_preview_context(entity, as_of=_today(), hide_build=True, in_setup=True),
    }
    return render(request, _template(request, "tenancy/setup/preview.html"), context)


@require_permission("tenancy.entity.view")
def build(request: HttpRequest, pk: str) -> HttpResponse:
    """Step three: the same card as step two, and the button that ends the flow.

    No separate submit handler here — the button posts to ``compliance:rebuild``
    like the steady-state entity page's does. What makes it the *end* of the
    flow rather than a dead end is ``?finish_setup=1``, which the template puts
    on it: the response then answers with ``HX-Location`` to the dashboard
    instead of a re-rendered card.

    ``hide_build=True``, the same as step two, because on this step the button
    is not in the card at all — it is in the wizard footer, where every earlier
    step of the rail put its forward action, and where a user who has scrolled
    to the bottom of a wizard is actually looking for it. Keeping it out of the
    card also keeps it out of the region that re-renders itself on every
    answered question, which is what used to drop ``finish_setup``. See
    ``tenancy/setup/_fragments/build_body.html``.

    This page listens for nothing. An earlier version of this docstring claimed
    it watched for ``stacos:calendar-rebuilt`` and navigated on it; no such
    listener was ever written, and while the navigation went through a custom
    trigger the flow really did dead-end here — the calendar was built and the
    user was left on the page with the button gone. See ``rebuild_calendar``.
    """
    entity = _entity_or_404(pk)
    context = {
        **_step_context("build", entity),
        **entity_preview_context(entity, as_of=_today(), hide_build=True, in_setup=True),
    }
    return render(request, _template(request, "tenancy/setup/build.html"), context)
