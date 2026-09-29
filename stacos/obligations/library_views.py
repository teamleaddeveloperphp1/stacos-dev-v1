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

from collections.abc import Iterable
from datetime import date
from typing import Any, Protocol

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.permissions import require_entity_permission, require_permission
from stacos.core.typing import current_user
from stacos.obligations.custom import (
    CustomObligationError,
    create_custom_obligation,
    current_version,
    schedule_summary,
    update_custom_obligation,
    withdraw_custom_obligation,
)
from stacos.obligations.forms import (
    LIBRARY_ORIGIN_FILTERS,
    LIBRARY_PROGRESS_FILTERS,
    LIBRARY_STATE_FILTERS,
    CustomObligationForm,
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
from stacos.obligations.models import CustomObligation, ObligationInstance
from stacos.obligations.queries import (
    CALENDAR_WINDOW_MONTHS,
    annotate_status,
    apply_due_window,
    calendar_window_end,
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
        "custom": sum(1 for row in rows if row.is_custom),
    }


def _filtered(rows: tuple[LibraryRow, ...], *, request: HttpRequest) -> list[LibraryRow]:
    state = request.GET.get("state", "").strip()
    progress = request.GET.get("progress", "").strip()
    category = request.GET.get("category", "").strip()
    origin = request.GET.get("origin", "").strip()
    search = request.GET.get("q", "").strip().lower()

    filtered = list(rows)
    if origin:
        filtered = [row for row in filtered if row.origin == origin]
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


class _CatalogRow(Protocol):
    @property
    def category(self) -> str: ...

    @property
    def title(self) -> str: ...


def group_by_category[RowT: _CatalogRow](rows: Iterable[RowT]) -> list[tuple[str, str, list[RowT]]]:
    """Rows grouped by category, category label resolved once per group.

    Groups in label order, rows by title within each. Shared with the Learn page
    (``learn_views``) so the two catalog listings order categories identically.
    """
    groups: dict[str, list[RowT]] = {}
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
        "groups": group_by_category(_filtered(rows, request=request)),
        "state": request.GET.get("state", ""),
        "progress": request.GET.get("progress", ""),
        "category": request.GET.get("category", ""),
        "origin": request.GET.get("origin", ""),
        "search": request.GET.get("q", ""),
        "state_filters": LIBRARY_STATE_FILTERS,
        "origin_filters": LIBRARY_ORIGIN_FILTERS,
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
    require_entity_permission(request, "compliance.library.manage", entity.pk)
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
    require_entity_permission(request, "compliance.library.manage", entity.pk)
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
    require_entity_permission(request, "compliance.library.manage", entity.pk)
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


# ---------------------------------------------------------------------------
# The entity's own obligations
# ---------------------------------------------------------------------------

CUSTOM_MODAL = "obligations/_fragments/custom_obligation_modal.html"
CUSTOM_WITHDRAW_MODAL = "obligations/_fragments/custom_withdraw_modal.html"
CUSTOM_DETAIL_BODY = "obligations/_fragments/custom_obligation_body.html"

#: How much of what one custom obligation produced its page lists. The
#: calendar is where the whole series is worked from; this is its history.
CUSTOM_HISTORY_ROWS = 24


def _custom_or_404(entity: Entity, pk: str) -> CustomObligation:
    """Through the scoped manager, so another tenant's — or, for a
    category-limited member, another department's — is a 404."""
    obligation = CustomObligation.objects.filter(pk=pk, entity=entity).first()
    if obligation is None:
        raise Http404
    return obligation


def _allowed_categories(request: HttpRequest, entity: Entity) -> frozenset[str] | None:
    """The categories this user may file their own obligation under here.

    Both limits the scoped manager applies on read — the member's own, and the
    engagement's for this entity — so nothing can be saved that its author
    would not be able to see.
    """
    scope = getattr(request, "access_scope", None)
    if scope is None or scope.bypass:
        return None
    allowed = scope.categories
    per_entity = scope.entity_categories.get(entity.pk)
    if per_entity is not None:
        allowed = frozenset(per_entity) if allowed is None else allowed & per_entity
    return allowed


def _custom_detail_context(
    request: HttpRequest, entity: Entity, obligation: CustomObligation, *, as_of: date
) -> dict[str, Any]:
    versions = list(obligation.versions.order_by("-version"))
    # The same 12-month window the calendar shows, so every due date in the
    # coming year is listed here and nothing past it: the planner builds
    # eighteen months, and the far end of that is noise on this page too.
    window_end = calendar_window_end(as_of)
    produced = annotate_status(
        apply_due_window(
            ObligationInstance.objects.filter(
                entity=entity, definition_code=obligation.code, archived_at__isnull=True
            ),
            window_end=window_end,
        ),
        as_of=as_of,
    ).order_by("-period_end", "-due_date")
    # The latest rows are the ones kept (the page promises "the latest N"),
    # but they read soonest-due first, like the calendar: the next filing on
    # top, not the one a year out. Undated rows go last.
    shown = sorted(
        produced[:CUSTOM_HISTORY_ROWS],
        key=lambda row: (
            row.due_date is None,
            row.due_date or date.min,
            row.period_end or date.min,
        ),
    )
    return {
        "entity": entity,
        "obligation": obligation,
        "versions": [
            {"version": version, "summary": schedule_summary(version)} for version in versions
        ],
        "instances": shown,
        "instance_total": produced.count(),
        "window_end": window_end,
        "window_months": CALENDAR_WINDOW_MONTHS,
        "can_manage": MANAGE in _permissions(request),
    }


def _land_on_custom_detail(
    request: HttpRequest, entity: Entity, obligation: CustomObligation, *, toast: str
) -> HttpResponse:
    """Close the modal and show the obligation's own page, wherever it was opened.

    The page is the answer to "what did that just do": the schedule as it now
    stands and the occurrences it put on the calendar. Swapped into ``#main``
    and pushed to history, so the back button returns to the library.
    """
    as_of = _today()
    response = oob(
        request,
        Fragment(
            CUSTOM_DETAIL_BODY, _custom_detail_context(request, entity, obligation, as_of=as_of)
        ),
        toast=Toast(toast),
        triggers={"stacos:modal-close": True},
        retarget="#main",
        reswap="innerHTML",
    )
    response["HX-Push-Url"] = reverse("compliance:custom_detail", args=[entity.pk, obligation.pk])
    return response


@require_permission(VIEW)
def custom_detail(request: HttpRequest, entity_pk: str, pk: str) -> HttpResponse:
    """One custom obligation: what it asks, every schedule it has had, and what
    it has put on the calendar — including after it was withdrawn."""
    entity = _entity_or_404(entity_pk)
    obligation = _custom_or_404(entity, pk)
    context = _custom_detail_context(request, entity, obligation, as_of=_today())
    template = (
        CUSTOM_DETAIL_BODY if is_fragment_request(request) else "obligations/custom_obligation.html"
    )
    return render(request, template, context)


@require_permission(MANAGE)
@require_http_methods(["GET", "POST"])
def custom_create(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """Add an obligation of the entity's own, and schedule it straight away."""
    entity = _entity_or_404(entity_pk)
    require_entity_permission(request, MANAGE, entity.pk)
    allowed = _allowed_categories(request, entity)

    if request.method == "GET":
        form = CustomObligationForm(allowed_categories=allowed)
        return render(request, CUSTOM_MODAL, {"entity": entity, "form": form})

    form = CustomObligationForm(request.POST, allowed_categories=allowed)
    if not form.is_valid():
        return render(request, CUSTOM_MODAL, {"entity": entity, "form": form}, status=422)

    try:
        obligation = create_custom_obligation(
            entity,
            actor=current_user(request),
            details=form.details,
            schedule=form.schedule,
            as_of=_today(),
        )
    except CustomObligationError as exc:
        form.add_error(None, str(exc))
        return render(request, CUSTOM_MODAL, {"entity": entity, "form": form}, status=422)

    return _land_on_custom_detail(
        request, entity, obligation, toast=_("Added — %(title)s.") % {"title": obligation.title}
    )


def _next_due(obligation: CustomObligation) -> date | None:
    """The soonest due date still ahead, which the edit form offers back."""
    row = (
        ObligationInstance.objects.filter(
            definition_code=obligation.code,
            entity_id=obligation.entity_id,
            archived_at__isnull=True,
            superseded_at__isnull=True,
            due_date__gte=_today(),
        )
        .order_by("due_date")
        .only("due_date")
        .first()
    )
    return row.due_date if row is not None else None


@require_permission(MANAGE)
@require_http_methods(["GET", "POST"])
def custom_edit(request: HttpRequest, entity_pk: str, pk: str) -> HttpResponse:
    """Correct the details in place, or change the schedule from a date on."""
    entity = _entity_or_404(entity_pk)
    require_entity_permission(request, MANAGE, entity.pk)
    obligation = _custom_or_404(entity, pk)
    if obligation.is_withdrawn:
        raise Http404
    allowed = _allowed_categories(request, entity)
    context: dict[str, Any] = {"entity": entity, "obligation": obligation}

    if request.method == "GET":
        form = CustomObligationForm(
            initial=CustomObligationForm.initial_for(
                obligation, current_version(obligation), next_due=_next_due(obligation)
            ),
            editing=True,
            allowed_categories=allowed,
        )
        return render(request, CUSTOM_MODAL, {**context, "form": form})

    form = CustomObligationForm(request.POST, editing=True, allowed_categories=allowed)
    if not form.is_valid():
        return render(request, CUSTOM_MODAL, {**context, "form": form}, status=422)

    try:
        update_custom_obligation(
            obligation,
            actor=current_user(request),
            details=form.details,
            schedule=form.schedule,
            applies_from=form.cleaned_data.get("applies_from"),
            as_of=_today(),
        )
    except CustomObligationError as exc:
        form.add_error(None, str(exc))
        return render(request, CUSTOM_MODAL, {**context, "form": form}, status=422)

    obligation.refresh_from_db()
    return _land_on_custom_detail(
        request, entity, obligation, toast=_("Saved — %(title)s.") % {"title": obligation.title}
    )


@require_permission(MANAGE)
@require_http_methods(["GET", "POST"])
def custom_withdraw(request: HttpRequest, entity_pk: str, pk: str) -> HttpResponse:
    """Stop tracking an obligation, keeping everything it already produced."""
    entity = _entity_or_404(entity_pk)
    require_entity_permission(request, MANAGE, entity.pk)
    obligation = _custom_or_404(entity, pk)
    context: dict[str, Any] = {"entity": entity, "obligation": obligation}

    if request.method == "GET":
        return render(request, CUSTOM_WITHDRAW_MODAL, {**context, "form": LibraryReasonForm()})

    form = LibraryReasonForm(request.POST)
    if not form.is_valid():
        return render(request, CUSTOM_WITHDRAW_MODAL, {**context, "form": form}, status=422)

    try:
        withdraw_custom_obligation(
            obligation,
            actor=current_user(request),
            reason=form.cleaned_data["reason"],
            as_of=_today(),
        )
    except CustomObligationError as exc:
        form.add_error(None, str(exc))
        return render(request, CUSTOM_WITHDRAW_MODAL, {**context, "form": form}, status=422)

    return _land_on_custom_detail(
        request, entity, obligation, toast=_("Withdrawn — %(title)s.") % {"title": obligation.title}
    )
