"""
The compliance library: the whole catalog, browsed against one entity.

Distinct from the calendar, which shows what has already been computed period
by period. Browsing here is per-entity — there is no cross-entity view — so
the sidebar link lands on an entity picker rather than requiring a caller to
already have an entity id.

Every view here renders two ways, exactly like the rest of ``obligations`` —
see the module docstring on ``views.py``. Viewing and acting are gated on two
different permissions: a view-only user sees every row and every status, and
no controls to change any of them.
"""

from __future__ import annotations

from datetime import date

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.obligations.forms import (
    LIBRARY_PROGRESS_FILTERS,
    LIBRARY_STATE_FILTERS,
    LibraryReasonForm,
)
from stacos.obligations.library import (
    LibraryError,
    LibraryRow,
    build_library,
    force_add_definition,
    remove_definition,
    restore_definition,
)
from stacos.tenancy.models import ComplianceCategory, Entity

VIEW = "compliance.library.view"
MANAGE = "compliance.library.manage"


def _today() -> date:
    return timezone.localdate()


def _entity_or_404(entity_pk: str) -> Entity:
    """Fetch through the scoped manager, 404 on anything out of reach.

    Same rationale as ``_entity_or_404``/``_get`` in ``views.py``: an entity's
    existence in another tenant is itself a disclosure, so a forged id is a
    404, never a 403.
    """
    entity = Entity.objects.filter(pk=entity_pk, archived_at__isnull=True).first()
    if entity is None:
        raise Http404
    return entity


def _row_or_404(entity: Entity, code: str, *, as_of: date) -> LibraryRow:
    row = next((r for r in build_library(entity, as_of=as_of) if r.code == code), None)
    if row is None:
        raise Http404
    return row


# ---------------------------------------------------------------------------
# The entity picker
# ---------------------------------------------------------------------------


@require_permission(VIEW)
def entity_picker(request: HttpRequest) -> HttpResponse:
    """Where the sidebar link lands. Browsing is per-entity, so this is the
    first thing anyone sees — pick an entity, then browse its shelf."""
    search = request.GET.get("q", "").strip()
    entities = Entity.objects.filter(archived_at__isnull=True).order_by("name")
    if search:
        entities = entities.filter(name__icontains=search)

    context = {"entities": entities, "search": search}
    template = (
        "obligations/_fragments/library_picker_body.html"
        if is_fragment_request(request)
        else "obligations/library_picker.html"
    )
    return render(request, template, context)


# ---------------------------------------------------------------------------
# The shelf
# ---------------------------------------------------------------------------


def _totals(rows: tuple[LibraryRow, ...]) -> dict[str, int]:
    """Entity-wide counts, from the full row set — computed once, before any
    filter is applied, so a filter can never change the numbers shown beside
    it."""
    return {
        "total": len(rows),
        "added": sum(1 for row in rows if row.state == "added"),
        "removed": sum(1 for row in rows if row.state == "removed"),
        "not_added": sum(1 for row in rows if row.state == "not_added"),
    }


def _filtered(rows: tuple[LibraryRow, ...], *, request: HttpRequest) -> list[LibraryRow]:
    state = request.GET.get("state", "").strip()
    progress = request.GET.get("progress", "").strip()
    category = request.GET.get("category", "").strip()
    search = request.GET.get("q", "").strip().lower()

    filtered = list(rows)
    if state:
        filtered = [row for row in filtered if row.state == state]
    if progress:
        filtered = [
            row
            for row in filtered
            if row.occurrence is not None
            and getattr(row.occurrence, "display_status", None) == progress
        ]
    if category:
        filtered = [row for row in filtered if row.category == category]
    if search:
        filtered = [
            row for row in filtered if search in row.title.lower() or search in row.code.lower()
        ]
    return filtered


def _grouped(rows: list[LibraryRow]) -> list[tuple[str, str, list[LibraryRow]]]:
    """Rows grouped by category, category label resolved once per group."""
    groups: dict[str, list[LibraryRow]] = {}
    for row in rows:
        groups.setdefault(row.category, []).append(row)

    def _label(code: str) -> str:
        try:
            return str(ComplianceCategory(code).label)
        except ValueError:
            return code

    return [
        (code, _label(code), sorted(group, key=lambda row: row.title))
        for code, group in sorted(groups.items(), key=lambda item: _label(item[0]))
    ]


@require_permission(VIEW)
def library_detail(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """The whole catalog, against one entity — grouped by category, with
    combinable filters that never change the entity-wide totals."""
    entity = _entity_or_404(entity_pk)
    as_of = _today()
    rows = build_library(entity, as_of=as_of)

    context = {
        "entity": entity,
        "totals": _totals(rows),
        "groups": _grouped(_filtered(rows, request=request)),
        "state": request.GET.get("state", ""),
        "progress": request.GET.get("progress", ""),
        "category": request.GET.get("category", ""),
        "search": request.GET.get("q", ""),
        "state_filters": LIBRARY_STATE_FILTERS,
        "progress_filters": LIBRARY_PROGRESS_FILTERS,
        "categories": ComplianceCategory.choices,
        "can_manage": MANAGE in _permissions(request),
    }

    template = (
        "obligations/_fragments/library_body.html"
        if is_fragment_request(request)
        else "obligations/library.html"
    )
    return render(request, template, context)


def _permissions(request: HttpRequest) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


def _row_fragment(entity: Entity, code: str, *, request: HttpRequest, as_of: date) -> Fragment:
    row = _row_or_404(entity, code, as_of=as_of)
    return Fragment(
        "obligations/_fragments/library_row.html",
        {"entity": entity, "row": row, "can_manage": MANAGE in _permissions(request)},
        oob_target=f"library-row-{code}",
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@require_permission(MANAGE)
@require_http_methods(["GET", "POST"])
def library_remove(request: HttpRequest, entity_pk: str, code: str) -> HttpResponse:
    """Rule a definition out for this entity, with a typed reason."""
    entity = _entity_or_404(entity_pk)
    as_of = _today()
    row = _row_or_404(entity, code, as_of=as_of)

    if request.method == "GET":
        return render(
            request,
            "obligations/_fragments/library_reason_modal.html",
            {"entity": entity, "row": row, "form": LibraryReasonForm(), "action": "remove"},
        )

    form = LibraryReasonForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "obligations/_fragments/library_reason_modal.html",
            {"entity": entity, "row": row, "form": form, "action": "remove"},
            status=422,
        )

    try:
        remove_definition(
            entity, code, actor=current_user(request), reason=form.cleaned_data["reason"]
        )
    except LibraryError as exc:
        form.add_error(None, str(exc))
        return render(
            request,
            "obligations/_fragments/library_reason_modal.html",
            {"entity": entity, "row": row, "form": form, "action": "remove"},
            status=422,
        )

    return oob(
        request,
        "",
        also=[_row_fragment(entity, code, request=request, as_of=as_of)],
        toast=Toast(_("Removed — %(title)s.") % {"title": row.title}),
        triggers={"stacos:modal-close": True},
    )


@require_permission(MANAGE)
@require_http_methods(["GET", "POST"])
def library_force_add(request: HttpRequest, entity_pk: str, code: str) -> HttpResponse:
    """Add a definition the engine has not selected, with a typed reason."""
    entity = _entity_or_404(entity_pk)
    as_of = _today()
    row = _row_or_404(entity, code, as_of=as_of)

    if request.method == "GET":
        return render(
            request,
            "obligations/_fragments/library_reason_modal.html",
            {"entity": entity, "row": row, "form": LibraryReasonForm(), "action": "force_add"},
        )

    form = LibraryReasonForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "obligations/_fragments/library_reason_modal.html",
            {"entity": entity, "row": row, "form": form, "action": "force_add"},
            status=422,
        )

    try:
        force_add_definition(
            entity,
            code,
            actor=current_user(request),
            reason=form.cleaned_data["reason"],
            as_of=as_of,
        )
    except LibraryError as exc:
        form.add_error(None, str(exc))
        return render(
            request,
            "obligations/_fragments/library_reason_modal.html",
            {"entity": entity, "row": row, "form": form, "action": "force_add"},
            status=422,
        )

    return oob(
        request,
        "",
        also=[_row_fragment(entity, code, request=request, as_of=as_of)],
        toast=Toast(_("Added — %(title)s.") % {"title": row.title}),
        triggers={"stacos:modal-close": True},
    )


@require_permission(MANAGE)
@require_http_methods(["POST"])
def library_restore(request: HttpRequest, entity_pk: str, code: str) -> HttpResponse:
    """Undo an earlier removal. No reason needed — the removal was already
    justified, and this simply reverses it."""
    entity = _entity_or_404(entity_pk)
    as_of = _today()
    row = _row_or_404(entity, code, as_of=as_of)

    try:
        restore_definition(entity, code, actor=current_user(request), as_of=as_of)
    except LibraryError as exc:
        return oob(request, "", toast=Toast(str(exc), level="danger"), status=422)

    return oob(
        request,
        "",
        also=[_row_fragment(entity, code, request=request, as_of=as_of)],
        toast=Toast(_("Restored — %(title)s.") % {"title": row.title}),
    )
