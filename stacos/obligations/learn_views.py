"""
Learn: the compliance catalog by legal form, before any entity exists.

Two read-only pages — every legal form the jurisdiction recognises, then one
form's obligations — built entirely by ``stacos.catalog.learn`` from platform
reference data. There is no entity here and no register: the only thing about
the requester these views read is which country their own tenant is in, because
that is whose law the catalog is.

Both declare ``catalog.view``, the permission that reads the catalog these pages
list and the definition pages every row links to. It sits in the floor bundle
every organisation and practice role carries, and is absent from the dealer role,
which sees no compliance data at all — the same line the sidebar draws.

Both render two ways, like every view in ``obligations``.
"""

from __future__ import annotations

from datetime import date

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from stacos.catalog import learn
from stacos.core.htmx import is_fragment_request
from stacos.core.permissions import require_permission
from stacos.jurisdictions.models import JurisdictionPack
from stacos.obligations.library_views import group_by_category

VIEW = "catalog.view"


def _today() -> date:
    return timezone.localdate()


def _pack(request: HttpRequest) -> JurisdictionPack | None:
    """The pack for the signed-in tenant's country. ``None`` when it has none
    loaded, which the index renders as its empty state."""
    tenant = getattr(request, "tenant", None)
    return learn.pack_for(tenant.country) if tenant is not None else None


@require_permission(VIEW)
def learn_index(request: HttpRequest) -> HttpResponse:
    """Every legal form, each with a count of what the catalog carries for it."""
    pack = _pack(request)
    cards = learn.entity_type_cards(pack, as_of=_today()) if pack else ()

    template = (
        "obligations/_fragments/learn_body.html"
        if is_fragment_request(request)
        else "obligations/learn.html"
    )
    return render(request, template, {"cards": cards})


@require_permission(VIEW)
def learn_entity_type(request: HttpRequest, code: str) -> HttpResponse:
    """One legal form's obligations, grouped by category, each with the
    standard statutory date it next falls due."""
    pack = _pack(request)
    entity_type = learn.find_entity_type(pack, code) if pack else None
    if pack is None or entity_type is None:
        raise Http404

    rows = learn.obligations_for(entity_type.code, pack=pack, as_of=_today())
    context = {
        "entity_type": entity_type,
        "total": len(rows),
        "groups": group_by_category(rows),
    }

    template = (
        "obligations/_fragments/learn_type_body.html"
        if is_fragment_request(request)
        else "obligations/learn_type.html"
    )
    return render(request, template, context)
