"""
The compliance calendar.

Every view here renders two ways — the full page on a direct GET, the fragment on
an HTMX request — which is what keeps deep links, the back button and "open in a
new tab" working while navigation still swaps only ``#main``.

Every view also declares a permission, including the fragment endpoints. An HTMX
fragment is a directly reachable URL; "it is only loaded from an authenticated
page" is how HTMX applications leak data.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Q, QuerySet
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.catalog.models import ComplianceDefinition, DefinitionVersion, PublicationStatus
from stacos.core.audit import record_event
from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.models import AuditAction
from stacos.core.pagination import filters_querystring
from stacos.core.permissions import public_view, require_permission
from stacos.core.typing import current_user
from stacos.engine.lifecycle import CLOSED_STATES, DUE_SOON_DAYS, OPEN_STATES, State, Transition
from stacos.engine.lifecycle import days_late as _days_late_calc
from stacos.engine.penalty import compute_penalties
from stacos.engine.types import occurrence_number
from stacos.jurisdictions.events import EVENT_TYPES
from stacos.obligations.feed import (
    create_feed_token,
    feed_events_for_user,
    render_ics,
    resolve_feed_token,
    revoke_feed_token,
)
from stacos.obligations.forms import (
    STATUS_FILTERS,
    AcknowledgementForm,
    AssignForm,
    BulkNotApplicableForm,
    CommentForm,
    EntityEventForm,
    FilingCompletedForm,
    FilingPendingForm,
    RecordEventForm,
    TransitionForm,
    obligation_display,
)
from stacos.obligations.models import (
    CalendarFeedToken,
    EntityEvent,
    MaterialisationRun,
    ObligationEvent,
    ObligationInclusion,
    ObligationInstance,
    ObligationStep,
    ObligationSuppression,
)
from stacos.obligations.preview import (
    adopt_pack,
    for_preview,
    preview_entity,
    revoke_pack,
    suggest_packs,
)
from stacos.obligations.profile import build_profile_view
from stacos.obligations.queries import (
    annotate_status,
    apply_text_filters,
    keyset_page,
    live,
    overdue_aging,
    overdue_penalty_exposure,
    related_scope_instances,
    scaled_bars,
    sibling_instances,
    status_counts,
    weekly_workload,
)
from stacos.obligations.services import materialise
from stacos.obligations.services import preview as preview_materialisation
from stacos.obligations.transitions import (
    TransitionError,
    add_comment,
    apply_transition,
    assign,
    assign_step,
    attach_acknowledgement,
    available_actions,
    block_step,
    complete_step,
    nudge,
    outstanding_mandatory_evidence,
    record_completion,
    record_pending,
    reopen_step,
    unblock_step,
)
from stacos.tenancy.forms import QuestionForm
from stacos.tenancy.models import ComplianceCategory, Entity, EntityRegistration
from stacos.tenancy.services import record_fact

PAGE_SIZE = 50

#: The window behind the "Due in 30 days" filter. Deliberately separate from
#: ``DUE_SOON_DAYS`` (7) rather than a second constant threaded through
#: ``annotate_status``/``derive_display_status`` — this chip doesn't touch
#: ``display_status`` or the overdue/due-soon parity those two enforce, it is
#: just a wider, independent slice of the same open queryset.
DUE_IN_30_DAYS = 30

#: Same reasoning as ``DUE_IN_30_DAYS``, one window wider. Also the width of the
#: calendar's default landing scope (``status="latest"``, see ``_filtered``) —
#: unlike the "Due in 90 days" chip, that scope has no lower bound at ``as_of``,
#: so it is the same number of days but not the same query.
DUE_IN_90_DAYS = 90

#: Rendered once rather than per request. Sorted so generated SQL is stable and
#: query-plan caching is not defeated by set iteration order.
_OPEN = sorted(str(state) for state in OPEN_STATES)
#: Kept in step with ``status_counts``'s own ``_CLOSED`` in ``queries.py`` — the
#: "Completed" tile counts ``NOT_APPLICABLE`` alongside ``FILED``/``CLOSED``, so
#: the filter behind it must too, or a click lands on a shorter list than the
#: number promised.
_CLOSED = sorted(str(state) for state in CLOSED_STATES)


def _today() -> date:
    """The date every request is judged against.

    Local date rather than UTC: a client in Ahmedabad must not see a filing marked
    overdue because a server elsewhere has already ticked past midnight. Django's
    ``TIME_ZONE`` carries the jurisdiction, which comes from the pack.
    """
    return timezone.localdate()


def _permissions(request: HttpRequest) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


def _parse_uuid(value: str) -> UUID | None:
    """A stale bookmark or a hand-edited ``?entity=`` must not crash a filter.

    Filtering a ``UUIDField`` with a string that is not a UUID at all raises
    ``ValidationError`` rather than just matching nothing — the same failure
    the dashboard's entity filter hit.
    """
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


def _entity_options() -> QuerySet[Entity]:
    """The dropdown's own lean query — never derived from ``live()``'s
    ``select_related`` queryset, which raises ``FieldError`` if you chain
    ``.only()`` onto a relation it already joins.
    """
    return Entity.objects.filter(archived_at__isnull=True).order_by("name").only("id", "name")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@require_permission("compliance.obligation.view")
def calendar_list(request: HttpRequest) -> HttpResponse:
    """The register, filtered and keyset-paginated.

    Keyset rather than ``LIMIT/OFFSET``: a practice with three hundred clients
    reaches page four hundred within a month, and offset pagination makes
    PostgreSQL walk and discard every row before it.
    """
    as_of = _today()
    queryset = _filtered(request, as_of=as_of)
    page = keyset_page(queryset, cursor=request.GET.get("cursor", ""), page_size=PAGE_SIZE)

    entity_id = _parse_uuid(request.GET.get("entity", "").strip())
    context = {
        "obligations": page.rows,
        "page": page,
        "as_of": as_of,
        "counts": status_counts(as_of=as_of, entity_ids=[entity_id] if entity_id else None),
        "status_filters": STATUS_FILTERS,
        "status": request.GET.get("status", ""),
        "search": request.GET.get("q", ""),
        "category": request.GET.get("category", ""),
        "categories": ComplianceCategory.choices,
        "entity": str(entity_id) if entity_id else "",
        "entity_id": entity_id,
        "entities": _entity_options(),
        "querystring": filters_querystring(request),
    }

    # A cursor request is asking for more rows, not for the whole screen again.
    if request.GET.get("cursor") and is_fragment_request(request):
        return render(request, "obligations/_fragments/calendar_rows.html", context)

    template = (
        "obligations/_fragments/calendar_body.html"
        if is_fragment_request(request)
        else "obligations/calendar.html"
    )
    return render(request, template, context)


def _filtered(request: HttpRequest, *, as_of: date) -> QuerySet[ObligationInstance]:
    """Apply the toolbar filters.

    ``select_related`` on entity and tenant is not optional here: without it a
    fifty-row page issues a hundred extra queries, and the query-count assertion
    in the test suite exists to keep it that way.
    """
    queryset = live().select_related("entity", "assigned_to")

    status = request.GET.get("status", "")
    if status == "overdue":
        queryset = queryset.filter(state__in=_OPEN, due_date__lt=as_of)
    elif status == "due_soon":
        queryset = queryset.filter(
            state__in=_OPEN,
            due_date__gte=as_of,
            due_date__lte=as_of + timedelta(days=DUE_SOON_DAYS),
        )
    elif status == "due_30":
        queryset = queryset.filter(
            state__in=_OPEN,
            due_date__gte=as_of,
            due_date__lte=as_of + timedelta(days=DUE_IN_30_DAYS),
        )
    elif status == "pending":
        # The dashboard's "Pending" tile: open, but neither overdue nor due
        # soon. Mirrors `pending = counts["open"] - overdue - due_soon` in
        # `stacos.tenancy.views` — overdue and due-soon are disjoint subsets
        # of open, so excluding both here can never double-subtract.
        #
        # Each excluded clause pins `due_date__isnull=False` before the
        # comparison, so it resolves to a definite `False` — not SQL's
        # NULL/"unknown" — for a row with no due date yet. Negating a bare
        # `due_date__lt=as_of` would instead have dropped every such row,
        # since `NOT NULL` is itself NULL and a WHERE clause only keeps rows
        # that evaluate to true.
        overdue_q = Q(due_date__isnull=False, due_date__lt=as_of)
        due_soon_q = Q(
            due_date__isnull=False,
            due_date__gte=as_of,
            due_date__lte=as_of + timedelta(days=DUE_SOON_DAYS),
        )
        queryset = queryset.filter(Q(state__in=_OPEN) & ~overdue_q & ~due_soon_q)
    elif status == "unconfirmed":
        # Rows the rule could not decide. An opt-in is confirmed by the act of
        # adding it and never appears here — same rule as `status_counts`.
        queryset = queryset.filter(state__in=_OPEN, confirmed=False)
    elif status == "needs_input":
        queryset = queryset.filter(state__in=_OPEN).exclude(needs_input="")
    elif status == "completed":
        queryset = queryset.filter(state__in=_CLOSED)
    elif status != "all":
        queryset = queryset.filter(state__in=_OPEN)

    search = request.GET.get("q", "").strip()
    if search:
        queryset = queryset.filter(
            Q(title__icontains=search)
            | Q(definition_code__icontains=search)
            | Q(scope_label__icontains=search)
            | Q(entity__name__icontains=search)
        )

    category = request.GET.get("category", "").strip()
    if category:
        queryset = queryset.filter(category=category)

    entity_id = _parse_uuid(request.GET.get("entity", "").strip())
    if entity_id:
        queryset = queryset.filter(entity_id=entity_id)

    return annotate_status(queryset, as_of=as_of)


# ---------------------------------------------------------------------------
# Month grid
# ---------------------------------------------------------------------------


@require_permission("compliance.obligation.view")
def calendar_month(request: HttpRequest) -> HttpResponse:
    """A month at a glance.

    The month grid is a second read of the same data rather than a second source
    of truth: it runs one query for the window and groups in Python, because
    thirty-one days of obligations is a few hundred rows at most and a query per
    cell would be thirty-one round trips.
    """
    as_of = _today()
    anchor = _parse_month(request.GET.get("month", ""), fallback=as_of)

    first = anchor.replace(day=1)
    last = _month_end(first)

    rows = annotate_status(
        live().filter(due_date__gte=first, due_date__lte=last).select_related("entity"),
        as_of=as_of,
    ).order_by("due_date", "title")

    by_day: dict[date, list[ObligationInstance]] = {}
    for row in rows:
        if row.due_date is not None:
            by_day.setdefault(row.due_date, []).append(row)

    context = {
        "as_of": as_of,
        "month_start": first,
        "month_end": last,
        "weeks": _weeks(first, last, by_day),
        "previous_month": (first - timedelta(days=1)).replace(day=1),
        "next_month": (last + timedelta(days=1)),
        "total": sum(len(v) for v in by_day.values()),
    }

    template = (
        "obligations/_fragments/month_body.html"
        if is_fragment_request(request)
        else "obligations/month.html"
    )
    return render(request, template, context)


def _parse_month(raw: str, *, fallback: date) -> date:
    try:
        year, month = raw.split("-")
        return date(int(year), int(month), 1)
    except (ValueError, AttributeError):
        return fallback.replace(day=1)


def _month_end(first: date) -> date:
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


def _weeks(
    first: date,
    last: date,
    by_day: dict[date, list[ObligationInstance]],
) -> list[list[dict[str, Any]]]:
    """Lay the month out as calendar weeks, Monday first.

    Leading and trailing cells belong to adjacent months and are rendered muted
    rather than omitted — a grid with ragged edges is harder to read than one with
    greyed-out days.
    """
    start = first - timedelta(days=first.weekday())
    end = last + timedelta(days=6 - last.weekday())

    weeks: list[list[dict[str, Any]]] = []
    cursor = start
    while cursor <= end:
        week: list[dict[str, Any]] = []
        for _index in range(7):
            week.append(
                {
                    "date": cursor,
                    "in_month": first <= cursor <= last,
                    "obligations": by_day.get(cursor, []),
                }
            )
            cursor += timedelta(days=1)
        weeks.append(week)
    return weeks


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@require_permission("compliance.obligation.view")
def obligation_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """One obligation: why it exists, when it is due, and what to do next."""
    as_of = _today()
    obligation = _get(pk, as_of=as_of)

    definition = (
        DefinitionVersion.objects.select_related("definition", "definition__authority")
        .filter(
            definition__code=obligation.definition_code,
            version=obligation.definition_version,
        )
        .first()
    )

    events = list(obligation.events.select_related("actor")[:50])
    permissions = _permissions(request)

    context = {
        "definition": definition,
        **_panel_context(obligation, request, as_of=as_of),
        **_discussion_context(events),
        "comment_form": CommentForm(),
        "registrations": (
            EntityRegistration.objects.filter(
                entity=obligation.entity, archived_at__isnull=True
            ).order_by("-is_primary", "type")[:5]
            if "tenancy.registration.view" in permissions
            else None
        ),
        "computed_penalties": (
            compute_penalties(
                definition.penalty_rules,
                days_late=_days_late_calc(
                    due_date=obligation.due_date, filed_on=obligation.filed_on, as_of=as_of
                ),
            )
            if definition is not None and definition.penalty_rules
            else []
        ),
    }
    template = (
        "obligations/_fragments/detail_body.html"
        if is_fragment_request(request)
        else "obligations/detail.html"
    )
    return render(request, template, context)


def _discussion_context(events: list[ObligationEvent]) -> dict[str, Any]:
    """Split one obligation's timeline into the system's history and the humans'.

    Both read from the same 50-row fetch — a comment is an :class:`ObligationEvent`
    like any other, just one nobody had written yet (`Kind.NOTE`, defined beside
    every transition kind but unused until now). Comments are handed back oldest
    first, the way a conversation reads; the timeline keeps the newest-first order
    a history is read in.
    """
    comments = [event for event in events if event.kind == ObligationEvent.Kind.NOTE]
    timeline_events = [event for event in events if event.kind != ObligationEvent.Kind.NOTE]
    return {"timeline_events": timeline_events, "comments": list(reversed(comments))}


#: The four permissions that gated the maker-checker flow the detail page used
#: to render as buttons — start work, request information, submit for review,
#: approve. The transitions are still in the lifecycle table and the API still
#: serialises them; this page simply stops offering them, because "is it done?"
#: is now the only question it asks. Filtering here rather than deleting the
#: transitions keeps one table describing the whole machine.
_FLOW_PERMISSIONS: frozenset[str] = frozenset(
    {
        "compliance.obligation.request_info",
        "compliance.obligation.prepare",
        "compliance.obligation.review",
        "compliance.obligation.approve",
    }
)


def _action_context(obligation: ObligationInstance, permissions: frozenset[str]) -> dict[str, Any]:
    """The two shapes the detail panel still renders actions as.

    A plain state change is a button; a judgement call (deferring, disputing,
    marking not applicable) needs a reason, so it is a tile that only opens its
    note field once chosen. Grouped here, once, rather than in the template, so
    an empty group renders no container at every one of this view's render
    sites.

    Recording the filing is deliberately *not* among them any more: it is the
    "yes" answer to the question the panel opens with, and offering the same act
    twice on one page invites two different dates for one filing.
    """
    actions = available_actions(obligation, permissions=permissions)
    offered = [
        a
        for a in actions
        if a.permission not in _FLOW_PERMISSIONS and not a.requires_filing_reference
    ]
    return {
        "actions": actions,
        "plain_actions": [a for a in offered if not a.requires_note],
        "note_actions": [a for a in offered if a.requires_note],
        "can_record_filing": "compliance.obligation.file" in permissions,
        "can_record_pending": "compliance.obligation.prepare" in permissions,
    }


def _panel_context(
    obligation: ObligationInstance,
    request: HttpRequest,
    *,
    as_of: date,
    completed_form: FilingCompletedForm | None = None,
    pending_form: FilingPendingForm | None = None,
    open_answer: str = "",
) -> dict[str, Any]:
    """Everything ``detail_panel.html`` needs, built once for every one of its
    render sites (the initial page load and the action endpoints that swap it
    afterwards) so a new field never has to be added in several places.

    The two question forms are parameters rather than always-fresh instances so
    a rejected answer comes back *bound*, with the field errors on it and the
    branch it was typed into still open. Re-rendering an empty form would throw
    away what the user typed and say nothing about what was wrong with it.
    """
    return {
        "obligation": obligation,
        "as_of": as_of,
        "completed_form": completed_form or FilingCompletedForm(initial={"filed_on": as_of}),
        "pending_form": pending_form or FilingPendingForm(),
        "open_answer": open_answer,
        **_action_context(obligation, _permissions(request)),
        "form": TransitionForm(),
        "event_form": (
            EntityEventForm(initial={"key": obligation.needs_input})
            if obligation.needs_input
            else None
        ),
    }


def _get(pk: str, *, as_of: date) -> ObligationInstance:
    """Fetch through the scoped manager, 404 on anything out of reach.

    404 rather than 403: confirming that an obligation exists in another tenant is
    itself a disclosure, and the hostile-client test asserts exactly this.
    """
    obligation = (
        annotate_status(ObligationInstance.objects.all(), as_of=as_of)
        .select_related("entity", "assigned_to")
        .filter(pk=pk)
        .first()
    )
    if obligation is None:
        raise Http404
    return obligation


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@require_permission("compliance.obligation.view")
@require_http_methods(["POST"])
def obligation_transition(request: HttpRequest, pk: str) -> HttpResponse:
    """Move an obligation along, updating several regions in one response.

    The declared permission is only ``view``: which *transition* is allowed is
    decided by the transition table, per target state, inside
    :func:`~stacos.obligations.transitions.apply_transition`. Declaring every
    workflow permission here with ``any_of`` would pass the CI check and enforce
    nothing useful.
    """
    as_of = _today()
    obligation = _get(pk, as_of=as_of)
    form = TransitionForm(request.POST)

    if not form.is_valid():
        return _detail_error(request, obligation, _("That form could not be read."))

    try:
        result = apply_transition(
            obligation,
            target=form.cleaned_data["target"],
            actor=current_user(request),
            permissions=_permissions(request),
            note=form.cleaned_data.get("note", ""),
            filing_reference=form.cleaned_data.get("filing_reference", ""),
            filed_on=form.cleaned_data.get("filed_on"),
            as_of=as_of,
        )
    except TransitionError as exc:
        return _detail_error(
            request, obligation, str(exc), status=409 if exc.code == "stale" else 422
        )

    refreshed = _get(pk, as_of=as_of)
    counts = status_counts(as_of=as_of)

    return oob(
        request,
        Fragment(
            "obligations/_fragments/detail_panel.html",
            _panel_context(refreshed, request, as_of=as_of),
        ),
        also=[
            Fragment(
                "obligations/_fragments/status_counts.html",
                {"counts": counts},
                oob_target="calendar-counts",
            )
        ],
        toast=Toast(
            _("%(action)s — %(what)s")
            % {"action": result.transition.label, "what": obligation_display(refreshed)}
        ),
        triggers={"stacos:obligation-changed": {"id": str(refreshed.pk)}},
    )


@require_permission("compliance.obligation.view")
@require_http_methods(["POST"])
def obligation_status(request: HttpRequest, pk: str) -> HttpResponse:
    """Answer the detail page's one question: is this filing done?

    Both answers land here rather than on two URLs, because they are two answers
    to one question and splitting them would let a page offer the "yes" form
    while the "no" endpoint had quietly stopped existing.

    The declared permission is only ``view``. Which answer the caller may give is
    a finer check made underneath — ``compliance.obligation.file`` for "yes",
    ``compliance.obligation.prepare`` for "no" — the same split
    :func:`obligation_transition` uses, and for the same reason: declaring both
    here with ``any_of`` would pass the CI check and enforce nothing useful.
    """
    as_of = _today()
    obligation = _get(pk, as_of=as_of)
    answer = request.POST.get("answer", "")

    if answer == "yes":
        form = FilingCompletedForm(request.POST, request.FILES)
        if not form.is_valid():
            return _detail_error(request, obligation, completed_form=form, open_answer="yes")
        try:
            record_completion(
                obligation,
                actor=current_user(request),
                permissions=_permissions(request),
                filed_on=form.cleaned_data["filed_on"],
                filing_reference=form.cleaned_data["filing_reference"],
                acknowledgement=form.cleaned_data.get("acknowledgement"),
                as_of=as_of,
            )
        except TransitionError as exc:
            return _detail_error(
                request,
                obligation,
                message=str(exc),
                status=409 if exc.code == "stale" else 422,
                completed_form=form,
                open_answer="yes",
            )
        message = _("Recorded as filed — %(what)s") % {"what": obligation_display(obligation)}

    elif answer == "no":
        form_no = FilingPendingForm(request.POST)
        if not form_no.is_valid():
            return _detail_error(request, obligation, pending_form=form_no, open_answer="no")
        try:
            record_pending(
                obligation,
                actor=current_user(request),
                permissions=_permissions(request),
                reason=form_no.cleaned_data["pending_reason"],
                expected_on=form_no.cleaned_data["expected_completion_date"],
            )
        except TransitionError as exc:
            return _detail_error(
                request,
                obligation,
                message=str(exc),
                status=422,
                pending_form=form_no,
                open_answer="no",
            )
        message = _("Noted — still pending.")

    else:
        return _detail_error(request, obligation, message=_("Answer yes or no."))

    refreshed = _get(pk, as_of=as_of)
    return oob(
        request,
        Fragment(
            "obligations/_fragments/detail_panel.html",
            _panel_context(refreshed, request, as_of=as_of),
        ),
        also=[
            Fragment(
                "obligations/_fragments/status_counts.html",
                {"counts": status_counts(as_of=as_of)},
                oob_target="calendar-counts",
            )
        ],
        toast=Toast(message),
        triggers={"stacos:obligation-changed": {"id": str(refreshed.pk)}},
    )


@require_permission("compliance.obligation.view")
@require_http_methods(["GET", "POST"])
def obligation_acknowledgement(request: HttpRequest, pk: str) -> HttpResponse | FileResponse:
    """Download the acknowledgement, or attach one to an already-filed row.

    The download exists as a view rather than as a ``MEDIA_URL`` link on
    purpose. ``MEDIA_ROOT`` is served directly only in development, so in
    production a bare link would simply 404 — but the more important half is
    that these bytes are a client's statutory evidence, and reaching them has to
    cost a scope check. ``_get`` supplies it: an obligation in another tenant is
    a 404 here exactly as it is everywhere else.

    ``X-Content-Type-Options: nosniff`` and the explicit type together stop an
    uploaded file being interpreted as anything but what it claims to be. The
    response is an attachment: an acknowledgement is evidence to keep, not a
    page to render inside the app's own origin.
    """
    as_of = _today()
    obligation = _get(pk, as_of=as_of)

    if request.method == "POST":
        if "compliance.obligation.file" not in _permissions(request):
            return oob(
                request,
                "",
                toast=Toast(_("You do not have permission to do that."), level="danger"),
                status=403,
            )
        form = AcknowledgementForm(request.POST, request.FILES)
        if not form.is_valid():
            return _detail_error(
                request,
                obligation,
                message=" ".join(
                    str(problem) for errors in form.errors.values() for problem in errors
                ),
            )
        attach_acknowledgement(
            obligation, upload=form.cleaned_data["acknowledgement"], actor=current_user(request)
        )
        refreshed = _get(pk, as_of=as_of)
        return oob(
            request,
            Fragment(
                "obligations/_fragments/detail_panel.html",
                _panel_context(refreshed, request, as_of=as_of),
            ),
            toast=Toast(_("Acknowledgement attached.")),
        )

    if not obligation.acknowledgement:
        raise Http404

    record_event(
        action=AuditAction.DOWNLOAD,
        actor=current_user(request),
        obj=obligation,
        after={"acknowledgement": obligation.acknowledgement.name},
    )

    response = FileResponse(
        obligation.acknowledgement.open("rb"),
        as_attachment=True,
        filename=obligation.acknowledgement_name or "acknowledgement",
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_permission("compliance.obligation.assign")
@require_http_methods(["GET", "POST"])
def obligation_assign(request: HttpRequest, pk: str) -> HttpResponse:
    """Hand an obligation to a colleague, or clear who is holding it.

    Reached both from the calendar row's "Owner" cell and from the detail
    panel, so the same modal, form and permission govern it wherever it is
    edited. ``AssignForm`` does the actual narrowing — the picker only ever
    lists people the caller can see — so this view does not need to re-check
    the assignee's tenant itself.
    """
    as_of = _today()
    obligation = _get(pk, as_of=as_of)

    if request.method == "GET":
        form = AssignForm(initial={"assigned_to": obligation.assigned_to_id})
        return render(
            request,
            "obligations/_fragments/assign_modal.html",
            {"obligation": obligation, "form": form},
        )

    form = AssignForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "obligations/_fragments/assign_modal.html",
            {"obligation": obligation, "form": form},
            status=422,
        )

    assignee = form.cleaned_data["assigned_to"]
    assign(obligation, assignee=assignee, actor=current_user(request))

    refreshed = _get(pk, as_of=as_of)
    return oob(
        request,
        "",
        also=[
            Fragment(
                "obligations/_fragments/obligation_row.html",
                {"obligation": refreshed, "as_of": as_of},
                oob_target=f"obligation-{refreshed.pk}",
            ),
            Fragment(
                "obligations/_fragments/detail_panel.html",
                _panel_context(refreshed, request, as_of=as_of),
                oob_target="obligation-panel",
            ),
        ],
        toast=Toast(
            _("Assigned to %(name)s.") % {"name": assignee}
            if assignee is not None
            else _("Assignment cleared.")
        ),
        triggers={
            "stacos:modal-close": True,
            "stacos:obligation-changed": {"id": str(refreshed.pk)},
        },
    )


@require_permission("compliance.obligation.comment")
@require_http_methods(["POST"])
def obligation_comment(request: HttpRequest, pk: str) -> HttpResponse:
    """Post a remark to the obligation's discussion.

    Not a workflow action — nothing about the obligation changes — so it writes
    only the timeline (``ObligationEvent``), never the tenant-wide audit log.
    """
    as_of = _today()
    obligation = _get(pk, as_of=as_of)
    form = CommentForm(request.POST)

    if not form.is_valid():
        return render(
            request,
            "obligations/_fragments/discussion.html",
            {**_discussion_context_for(obligation), "comment_form": form},
            status=422,
        )

    add_comment(obligation, actor=current_user(request), note=form.cleaned_data["note"])

    return oob(
        request,
        Fragment(
            "obligations/_fragments/discussion.html",
            {**_discussion_context_for(obligation), "comment_form": CommentForm()},
        ),
        toast=Toast(_("Comment posted.")),
    )


@require_permission("compliance.obligation.request_info")
@require_http_methods(["POST"])
def obligation_nudge(request: HttpRequest, pk: str) -> HttpResponse:
    """Re-ping whoever is holding this obligation, by email and WhatsApp."""
    as_of = _today()
    obligation = _get(pk, as_of=as_of)

    if obligation.assigned_to is None:
        return oob(
            request,
            "",
            toast=Toast(_("Nobody is assigned yet — there is no one to nudge."), level="info"),
        )

    assignee = obligation.assigned_to
    nudge(
        obligation,
        person=assignee,
        actor=current_user(request),
        as_of=as_of,
        url=request.build_absolute_uri(reverse("compliance:detail", args=[obligation.pk])),
    )

    refreshed = _get(pk, as_of=as_of)
    return oob(
        request,
        Fragment(
            "obligations/_fragments/detail_panel.html",
            _panel_context(refreshed, request, as_of=as_of),
        ),
        also=[
            Fragment(
                "obligations/_fragments/discussion.html",
                {**_discussion_context_for(refreshed), "comment_form": CommentForm()},
                oob_target="obligation-discussion",
            ),
        ],
        toast=Toast(_("%(name)s has been nudged.") % {"name": assignee}),
    )


# ---------------------------------------------------------------------------
# The checklist
# ---------------------------------------------------------------------------


def _get_step(pk: str) -> ObligationStep:
    """Fetch through the scoped manager, 404 on anything out of reach.

    Same rationale as ``_get`` — a step's existence in another tenant is
    itself a disclosure.
    """
    step = (
        ObligationStep.objects.select_related("obligation", "obligation__entity", "assigned_to")
        .filter(pk=pk)
        .first()
    )
    if step is None:
        raise Http404
    return step


def _step_panel_response(
    request: HttpRequest,
    obligation: ObligationInstance,
    *,
    toast: Toast | None = None,
    triggers: dict[str, Any] | None = None,
) -> HttpResponse:
    """Every checklist mutation re-renders the whole panel, the same way a
    lifecycle transition already does — the checklist is part of it, not a
    region of its own."""
    as_of = _today()
    refreshed = _get(str(obligation.pk), as_of=as_of)
    return oob(
        request,
        Fragment(
            "obligations/_fragments/detail_panel.html",
            _panel_context(refreshed, request, as_of=as_of),
        ),
        toast=toast,
        triggers=triggers,
    )


@require_permission("compliance.obligation.view")
@require_http_methods(["POST"])
def obligation_step_toggle(request: HttpRequest, pk: str) -> HttpResponse:
    """Mark a checklist item done, or reopen a completed one.

    Declared permission is only ``view`` — which of PREPARE/REVIEW/APPROVE/SIGN
    a step needs is a finer check made inside ``complete_step``/``reopen_step``,
    the same split ``obligation_transition`` uses for the same reason.
    """
    step = _get_step(pk)
    permissions = _permissions(request)
    actor = current_user(request)

    try:
        if step.state == ObligationStep.State.DONE:
            reopen_step(step, actor=actor, permissions=permissions)
            message = _("Reopened.")
        else:
            complete_step(
                step,
                actor=actor,
                permissions=permissions,
                evidence_note=request.POST.get("evidence_note", ""),
            )
            message = _("Marked done.")
    except TransitionError as exc:
        return oob(request, "", toast=Toast(str(exc), level="danger"), status=422)

    return _step_panel_response(request, step.obligation, toast=Toast(message))


@require_permission("compliance.obligation.view")
@require_http_methods(["POST"])
def obligation_step_block(request: HttpRequest, pk: str) -> HttpResponse:
    """Mark a step blocked with a reason, or clear an existing block."""
    step = _get_step(pk)
    permissions = _permissions(request)
    actor = current_user(request)

    try:
        if step.state == ObligationStep.State.BLOCKED:
            unblock_step(step, actor=actor, permissions=permissions)
            message = _("Unblocked.")
        else:
            block_step(
                step, actor=actor, permissions=permissions, reason=request.POST.get("reason", "")
            )
            message = _("Marked blocked.")
    except TransitionError as exc:
        return oob(request, "", toast=Toast(str(exc), level="danger"), status=422)

    return _step_panel_response(request, step.obligation, toast=Toast(message))


@require_permission("compliance.obligation.assign")
@require_http_methods(["GET", "POST"])
def obligation_step_assign(request: HttpRequest, pk: str) -> HttpResponse:
    """Hand one checklist item to someone — the same modal and form as
    assigning the obligation as a whole, since ``AssignForm`` carries no
    obligation-specific coupling."""
    step = _get_step(pk)

    if request.method == "GET":
        form = AssignForm(initial={"assigned_to": step.assigned_to_id})
        return render(
            request,
            "obligations/_fragments/assign_modal.html",
            {"obligation": step.obligation, "step": step, "form": form},
        )

    form = AssignForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "obligations/_fragments/assign_modal.html",
            {"obligation": step.obligation, "step": step, "form": form},
            status=422,
        )

    assignee = form.cleaned_data["assigned_to"]
    assign_step(step, assignee=assignee, actor=current_user(request))

    message = (
        _("Assigned to %(name)s.") % {"name": assignee}
        if assignee is not None
        else _("Assignment cleared.")
    )
    return _step_panel_response(
        request, step.obligation, toast=Toast(message), triggers={"stacos:modal-close": True}
    )


@require_permission("compliance.obligation.request_info")
@require_http_methods(["POST"])
def obligation_step_nudge(request: HttpRequest, pk: str) -> HttpResponse:
    """Re-ping whoever is holding up one checklist item."""
    step = _get_step(pk)
    if step.assigned_to is None:
        return oob(
            request,
            "",
            toast=Toast(_("Nobody is assigned yet — there is no one to nudge."), level="info"),
        )

    as_of = _today()
    nudge(
        step.obligation,
        person=step.assigned_to,
        actor=current_user(request),
        as_of=as_of,
        url=request.build_absolute_uri(reverse("compliance:detail", args=[step.obligation_id])),
    )

    return _step_panel_response(
        request,
        step.obligation,
        toast=Toast(_("%(name)s has been nudged.") % {"name": step.assigned_to}),
    )


def _discussion_context_for(obligation: ObligationInstance) -> dict[str, Any]:
    events = list(obligation.events.select_related("actor")[:50])
    return {"obligation": obligation, **_discussion_context(events)}


def _detail_error(
    request: HttpRequest,
    obligation: ObligationInstance,
    message: str = "",
    *,
    status: int = 422,
    completed_form: FilingCompletedForm | None = None,
    pending_form: FilingPendingForm | None = None,
    open_answer: str = "",
) -> HttpResponse:
    """Re-render the action panel with the problem stated, keeping the page put.

    A bound form may be passed back in, which is what makes a rejected answer
    recoverable: the panel comes back with what the user typed still in it, the
    field-level errors beside the fields that caused them, and the branch they
    were typed into still open. ``message`` is for problems that belong to no
    single field — a stale transition, a missing permission.
    """
    return render(
        request,
        "obligations/_fragments/detail_panel.html",
        {
            **_panel_context(
                obligation,
                request,
                as_of=_today(),
                completed_form=completed_form,
                pending_form=pending_form,
                open_answer=open_answer,
            ),
            "error": message,
        },
        status=status,
    )


@require_permission("compliance.event.record")
@require_http_methods(["POST"])
def record_entity_event(request: HttpRequest, pk: str) -> HttpResponse:
    """Answer a "we cannot schedule this until you tell us X" prompt.

    Rebuilds the entity's calendar immediately afterwards, because the whole
    reason the user typed a date is that something was waiting on it — asking them
    to come back tomorrow for the nightly run would be absurd.
    """
    as_of = _today()
    obligation = _get(pk, as_of=as_of)
    form = EntityEventForm(request.POST)

    if not form.is_valid():
        return render(
            request,
            "obligations/_fragments/needs_input.html",
            {"obligation": obligation, "event_form": form},
            status=422,
        )

    event: EntityEvent = form.save(commit=False)
    event.tenant = obligation.entity.tenant
    event.entity = obligation.entity
    event.recorded_by = current_user(request)
    event.save()

    materialise(obligation.entity, as_of=as_of, trigger="MANUAL", actor=current_user(request))

    refreshed = _get(pk, as_of=as_of)
    return oob(
        request,
        Fragment(
            "obligations/_fragments/detail_panel.html",
            _panel_context(refreshed, request, as_of=as_of),
        ),
        toast=Toast(_("Date recorded. The calendar has been rebuilt.")),
    )


# ---------------------------------------------------------------------------
# Confirming an obligation
#
# "Confirm" was a badge and nothing else — an inert `<span>` whose tooltip said
# "confirm a few details" beside no way to do so. The detail the user is being
# asked for is `Verdict.missing_facts`, which the engine computes on every
# evaluation and which is now persisted on the row (`missing_facts`) instead of
# being discarded with the verdict.
#
# Only obligations with a fact to ask about get the action, and they are the only
# unconfirmed ones there are: an obligation somebody opted into by hand is
# confirmed by the act of adding it (`stacos.engine.planner`), so it carries no
# badge and no question. Asking a user to confirm what they have just asked for
# is a question with one honest answer. Changing their mind is a dismissal — the
# detail page's "mark not applicable" — not a confirmation.
# ---------------------------------------------------------------------------


def _askable_questions(obligation: ObligationInstance, *, as_of: date) -> list[Any]:
    """The blocking facts, ranked, in the order worth asking them.

    Reuses ``rank_questions`` — the same pure function the entity preview ranks
    by, against the same fact registry — so the questions asked here are the
    questions asked there, in the same order, phrased the same way. It also does
    the work this view could not do for itself: mapping a fact nobody can answer
    directly onto one they can, and dropping the facts that are derived from a
    registration or a premises rather than typed by a person.
    """
    from stacos.catalog.snapshots import build_catalog
    from stacos.obligations.questions import askable_facts, rank_questions

    raw = frozenset(obligation.missing_facts or ())
    if not raw:
        return []

    # Not the raw set. A derived fact has no input of its own — nobody types a
    # turnover *band*, they type a turnover — so the question that settles this
    # obligation may be keyed differently from the fact the rule went looking
    # for. `askable_facts` follows `depends_on` to find it, and drops the facts
    # that come from a registration or a premises rather than from a person.
    # Filtering the ranked list on the raw set instead would silently offer no
    # question for exactly the obligations that have one.
    blocking = askable_facts(raw)
    if not blocking:
        return []

    profile = build_profile_view(obligation.entity, as_of=as_of)
    # Same prefilter the materialiser uses: country and the jurisdictions this
    # entity actually operates in. Ranking against the whole catalog would score
    # the question by rules that could never apply here.
    catalog = build_catalog(
        country=obligation.entity.country,
        jurisdictions=profile.jurisdictions,
    )
    # No `limit`: the default keeps the wizard's queue short, which is right
    # there and wrong here — this is filtered down to one obligation's blocking
    # facts immediately afterwards, and a fact ranked thirteenth overall is still
    # the only thing standing between this row and a decision.
    ranked = rank_questions(catalog=catalog, facts=profile.facts, limit=len(catalog) or 1)
    return [question for question in ranked if question.key in blocking]


@require_permission("tenancy.profile.edit")
@require_http_methods(["GET", "POST"])
def confirm_obligation(request: HttpRequest, pk: str) -> HttpResponse:
    """Ask the question behind an unconfirmed obligation, and answer it.

    Recalculates immediately rather than queueing. The user has just told the
    product something that changes what applies to them; "your calendar will
    catch up overnight" throws away the entire point of asking. Same trade as
    ``record_entity_event`` two functions up, and for the same reason.

    Gated on ``tenancy.profile.edit`` rather than on an obligation permission:
    the answer is written to the entity's compliance profile, and changing the
    profile changes what applies.
    """
    as_of = _today()
    obligation = _get(pk, as_of=as_of)
    questions = _askable_questions(obligation, as_of=as_of)

    if not questions and not obligation.missing_facts:
        # Nothing was ever unknown about this one: either the rule decided it or
        # a person opted into it, and both are settled. No row offers a Confirm
        # control for it, so this is a stale page or a guessed URL.
        raise Http404

    if not questions:
        # Undecided, but on something nobody can type: a fact that comes from a
        # registration or a premises. Say so and say where it is answered, rather
        # than showing an empty form or a 404 that reads as a broken button.
        return render(
            request,
            "obligations/_fragments/confirm_elsewhere.html",
            {"obligation": obligation},
        )

    if request.method == "GET":
        return render(
            request,
            "obligations/_fragments/confirm_modal.html",
            {"obligation": obligation, "questions": questions},
        )

    fact_key = request.POST.get("fact_key", "")
    question = next((q for q in questions if q.key == fact_key), None)
    if question is None:
        raise Http404

    form = QuestionForm(request.POST, fact_key=fact_key)
    if not form.is_valid():
        return render(
            request,
            "obligations/_fragments/confirm_modal.html",
            {"obligation": obligation, "questions": questions, "form": form},
            status=422,
        )

    answer = form.answer()
    if answer is None:
        # "Not sure yet" is a real answer in three-valued logic — it just is not
        # a change. Saying so beats writing a null and rebuilding the calendar to
        # produce exactly what is already on screen.
        return oob(
            request,
            "",
            toast=Toast(_("Nothing recorded — the obligation stays unconfirmed."), level="info"),
            triggers={"stacos:modal-close": True},
        )

    with transaction.atomic():
        record_fact(
            obligation.entity,
            fact_key,
            answer,
            actor=current_user(request),
            as_of=as_of,
        )
        materialise(
            obligation.entity,
            as_of=as_of,
            trigger=MaterialisationRun.Trigger.MANUAL,
            actor=current_user(request),
        )

    return _confirmed_response(
        request,
        pk,
        as_of=as_of,
        changed_message=_("Answered — %(fact)s. Your calendar has been rebuilt.")
        % {"fact": question.fact.label},
        removed_message=_("Answered — %(fact)s. This one does not apply to you after all.")
        % {"fact": question.fact.label},
    )


def _confirmed_response(
    request: HttpRequest,
    pk: str | UUID,
    *,
    as_of: date,
    changed_message: str,
    removed_message: str,
) -> HttpResponse:
    """Update the row and the counters, and say what actually changed.

    The obligation may no longer exist: an answer that resolves the rule to FALSE
    archives it, which is the correct outcome and a confusing one to discover as
    a blank row. So the row is removed rather than re-rendered, and the toast
    says which way it went.

    One answer can settle several obligations at once — that is the whole point
    of ranking questions by how much each one unlocks — so the counters are
    re-rendered too rather than only the row that was clicked.
    """
    counts = status_counts(as_of=as_of)
    counters = Fragment(
        "obligations/_fragments/status_counts.html",
        {"counts": counts},
        oob_target="calendar-counts",
    )

    refreshed = (
        annotate_status(ObligationInstance.objects.all(), as_of=as_of)
        .select_related("entity", "assigned_to")
        .filter(pk=pk)
        .first()
    )

    if refreshed is None or refreshed.archived_at is not None:
        return oob(
            request,
            f'<tr id="obligation-{pk}" hx-swap-oob="delete"></tr>',
            also=[counters],
            toast=Toast(removed_message),
            triggers={"stacos:modal-close": True},
        )

    return oob(
        request,
        Fragment(
            "obligations/_fragments/obligation_row.html",
            {"obligation": refreshed, "as_of": as_of},
            oob_target=f"obligation-{pk}",
        ),
        also=[counters],
        toast=Toast(changed_message),
        triggers={"stacos:modal-close": True},
    )


@require_permission("compliance.obligation.view")
def entity_events(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """The events recorded against one entity, and what each one produced.

    Lazily loaded as a panel on the entity page, like the compliance summary
    beside it. The "obligations produced" column is what makes the feature
    legible: an event with nothing under it is either a definition gap or a date
    somebody typed wrong, and both are worth seeing.
    """
    entity = _entity_or_404(entity_pk)
    return render(
        request,
        "obligations/_fragments/entity_events.html",
        _entity_events_context(entity),
    )


@require_permission("compliance.event.record")
@require_http_methods(["GET", "POST"])
def event_create(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """Record something that happened. Rebuilds the calendar immediately.

    Synchronously, and in the same transaction as the event: an appointment
    recorded without the DIR-12 it triggers is a calendar that is wrong in the
    direction that costs money, and the whole reason somebody typed the date is
    that something was waiting on it.
    """
    entity = _entity_or_404(entity_pk)
    as_of = _today()
    form = RecordEventForm(request.POST or None, country=entity.country)

    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            event: EntityEvent = form.save(commit=False)
            event.tenant = entity.tenant
            event.entity = entity
            event.recorded_by = current_user(request)
            event.save()

            run = materialise(
                entity,
                as_of=as_of,
                trigger=MaterialisationRun.Trigger.EVENT_RECORDED,
                actor=current_user(request),
            )

        return oob(
            request,
            Fragment("obligations/_fragments/entity_events.html", _entity_events_context(entity)),
            toast=Toast(
                _("%(event)s recorded. %(summary)s.")
                % {"event": event.display_label(), "summary": run.summary()}
            ),
            triggers={"stacos:modal-close": True},
        )

    status = 422 if request.method == "POST" else 200
    return render(
        request,
        "obligations/_fragments/event_form_modal.html",
        {"form": form, "entity": entity},
        status=status,
    )


@require_permission("compliance.event.withdraw")
@require_http_methods(["POST"])
def event_withdraw(request: HttpRequest, pk: str) -> HttpResponse:
    """Take back an event recorded in error.

    Soft, always. The event's primary key is the engine's stable occurrence
    reference, so destroying the row destroys the identity of every obligation
    derived from it — and undoing the mistake would then create a duplicate
    beside the one somebody had already started work on.
    """
    event = EntityEvent.objects.filter(pk=pk, superseded_at__isnull=True).first()
    if event is None:
        raise Http404

    entity = event.entity
    as_of = _today()

    with transaction.atomic():
        event.superseded_at = timezone.now()
        event.supersede_reason = request.POST.get("reason", "")[:250]
        event.save(update_fields=["superseded_at", "supersede_reason"])
        run = materialise(
            entity,
            as_of=as_of,
            trigger=MaterialisationRun.Trigger.EVENT_RECORDED,
            actor=current_user(request),
        )

    return oob(
        request,
        Fragment("obligations/_fragments/entity_events.html", _entity_events_context(entity)),
        toast=Toast(
            _("Event withdrawn. %(summary)s.") % {"summary": run.summary()},
            level="warning" if run.needs_review else "info",
        ),
    )


def _entity_or_404(entity_pk: str) -> Entity:
    entity = Entity.objects.filter(pk=entity_pk, archived_at__isnull=True).first()
    if entity is None:
        raise Http404
    return entity


def _entity_events_context(entity: Entity) -> dict[str, object]:
    """Events plus the obligations each one produced, in two queries.

    The obligations are fetched once for the whole page and grouped in Python
    rather than queried per row, because a company with thirty recorded events
    would otherwise cost thirty-one queries to render a panel.
    """
    events = list(
        EntityEvent.objects.filter(entity=entity, superseded_at__isnull=True).order_by(
            "-occurred_on", "-id"
        )[:50]
    )
    period_keys = {f"EV-{event.occurred_on.isoformat()}" for event in events}
    produced: dict[tuple[str, int], list[ObligationInstance]] = {}
    if period_keys:
        for instance in ObligationInstance.objects.filter(
            entity=entity, period_key__in=period_keys, archived_at__isnull=True
        ):
            key = (instance.period_key, instance.occurrence)
            produced.setdefault(key, []).append(instance)

    rows = [
        {
            "event": event,
            "type": EVENT_TYPES.get(event.key),
            "obligations": produced.get(
                (f"EV-{event.occurred_on.isoformat()}", occurrence_number(event.ref)), []
            ),
        }
        for event in events
    ]
    return {"entity": entity, "event_rows": rows}


@require_permission("compliance.calendar.rebuild")
@require_http_methods(["POST"])
def rebuild_calendar(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """Re-run materialisation for one entity, on demand.

    Safe by construction: the planner never destroys an obligation carrying
    history, and re-running against unchanged inputs produces an empty plan. The
    button exists because "I updated the profile, where are my filings" is the
    first thing a user asks after adding a registration.

    ``?finish_setup=1`` is the one difference: the guided setup flow's Build
    step (``stacos.tenancy.entity_setup.build``) posts here with it set, and
    the response answers with ``HX-Location`` to the dashboard instead of a
    re-rendered card.

    ``HX-Location`` rather than the ``stacos:navigate`` trigger the rest of the
    product uses. The trigger route needs a custom listener in ``app.js`` to
    fire a second request, and it was losing a race against the card replacing
    itself back when the button was rendered inside that card; the observed
    failure was the whole flow dead-ending, obligations built and the user still
    looking at "what applies to you". The button has since moved out to the
    wizard footer, but ``HX-Location`` stays: htmx handles it in core, before
    any swap, and returns — no second in-flight request, nothing to lose a race
    to, and no dependency on ``app.js`` having been rebuilt. The toast still
    arrives, because ``HX-Trigger`` is processed first.

    The calendar, filtered to this entity, rather than the dashboard: finishing
    setup is the moment the obligations that were just planned become visible,
    and the calendar is where they live. ``entity_detail`` is safe to leave
    either way — its "have you ever built" guard is a
    ``MaterialisationRun.exists()`` check, which this run has just satisfied,
    so it no longer bounces back into the flow whenever the user navigates
    there themselves.
    """
    as_of = _today()
    entity = Entity.objects.filter(pk=entity_pk, archived_at__isnull=True).first()
    if entity is None:
        raise Http404

    run = materialise(entity, as_of=as_of, trigger="MANUAL", actor=current_user(request))

    triggers: dict[str, Any] = {"stacos:calendar-rebuilt": {"summary": run.summary()}}
    toast = Toast(
        _("Calendar rebuilt: %(summary)s.") % {"summary": run.summary()},
        level="success" if not run.needs_review else "warning",
    )

    if request.GET.get("finish_setup") == "1":
        # No body: htmx discards the response when it acts on HX-Location, so
        # rendering the card here would be work thrown away — and this is the
        # one caller that is guaranteed to navigate off the page it swaps.
        # `target`/`swap` keep the app shell in place rather than reloading the
        # document, which is what `navigate()` in app.js does for every other
        # server-directed move.
        response = oob(request, toast=toast, triggers=triggers)
        response["HX-Location"] = json.dumps(
            {
                "path": f"{reverse('compliance:calendar')}?entity={entity.pk}",
                "target": "#main",
                "swap": "morph:innerHTML",
            }
        )
        return response

    # The re-rendered panel, not the calendar's counter strip. This button only
    # exists inside the entity summary card, and that page has no
    # `#calendar-counts` to swap — so the counters were being rendered into a
    # response the caller then discarded, and the user saw the toast fire while
    # the table in front of them stayed stale until a manual reload.
    return oob(
        request,
        Fragment(
            ENTITY_SUMMARY_TEMPLATE,
            entity_preview_context(entity, as_of=as_of),
        ),
        toast=toast,
        triggers=triggers,
    )


# ---------------------------------------------------------------------------
# Dashboard fragment
# ---------------------------------------------------------------------------


@require_permission("catalog.view")
def definition_detail(request: HttpRequest, code: str) -> HttpResponse:
    """What the law says, behind an obligation.

    A client asking "why do I have to file this" deserves the statutory reference
    and a plain-language summary, not an assertion. Shown with the review date, and
    with a caveat when that review is stale.

    Reached both from an obligation's "Full definition" link (which is also a
    direct, bookmarkable URL — the fragment must therefore carry its own page,
    not rely on a shell it may not be swapped into) and, in principle, from
    nowhere at all, so the back link degrades to the calendar rather than
    demanding a caller.
    """
    definition = ComplianceDefinition.objects.filter(code=code).first()
    if definition is None:
        raise Http404

    version = (
        definition.versions.filter(status=PublicationStatus.PUBLISHED)
        .order_by("-effective_from")
        .first()
    )
    if version is None:
        raise Http404

    back_url = reverse("compliance:calendar")
    from_pk = request.GET.get("from", "")
    if from_pk:
        try:
            UUID(from_pk)
        except ValueError:
            pass
        else:
            # The scoped manager is the check: a forged id from another tenant
            # simply is not there, and the link falls back rather than 404ing —
            # nothing about a stray query parameter is worth an error page.
            if ObligationInstance.objects.filter(pk=from_pk).exists():
                back_url = reverse("compliance:detail", args=[from_pk])

    context = {"definition": definition, "version": version, "back_url": back_url}
    template = (
        "obligations/_fragments/definition_detail.html"
        if is_fragment_request(request)
        else "obligations/definition.html"
    )
    return render(request, template, context)


#: The panel the entity detail page loads, and the panel "Rebuild calendar"
#: sends back. Named once so the two can never drift.
ENTITY_SUMMARY_TEMPLATE = "obligations/_fragments/entity_summary.html"


def _accepted_pack_codes(entity: Entity) -> frozenset[str]:
    """Packs this entity currently has adopted, for the pack strip's toggle state.

    There is no per-entity "accepted packs" field — a pack's acceptance is
    recorded the same way a single opted-in obligation is, one
    ``ObligationInclusion`` row per definition it added
    (``stacos.obligations.preview.adopt_pack``). This is the inverse read.
    """
    return frozenset(
        ObligationInclusion.objects.filter(
            entity=entity,
            source=ObligationInclusion.Source.PACK,
            revoked_at__isnull=True,
        ).values_list("pack_code", flat=True)
    )


def entity_preview_context(
    entity: Entity,
    *,
    as_of: date,
    hide_build: bool = False,
    in_setup: bool = False,
    hide_questions: bool = False,
    hide_columns: bool = False,
    hide_packs: bool = False,
) -> dict[str, Any]:
    """What applies to this entity right now, and what would settle the rest.

    Shared by the panel's own endpoint, by ``rebuild_calendar`` (has to render
    the same card so what the user is looking at reflects the plan that just
    ran), by ``answer_entity_question``/``toggle_entity_pack`` (has to
    re-render the same numbers after writing one fact or one pack), and by the
    guided setup flow (``stacos.tenancy.entity_setup``), whose four steps each
    show this exact card with a different subset of its sections switched off
    — ``hide_build`` is the only reason this card ever renders without a
    button at all.

    ``in_setup`` says the card is being shown inside the guided setup flow
    rather than on the entity page it lives on permanently. The pack strip hangs
    off it: a first-run suggestion, not something the steady-state page should
    keep offering.

    ``hide_questions``/``hide_columns``/``hide_packs`` say which of the card's
    other sections to leave out — the question queue, the category breakdown,
    and the pack strip respectively. The guided setup's four steps each ask
    for a different combination: the Answers step hides the columns and packs
    so a question's effect is felt against the headline counts alone; the
    Packs step hides the questions and columns; the final Review step hides
    the questions and packs, leaving the columns as a stable snapshot. The
    steady-state entity page passes none of them, and sees the whole card, as
    it always has.

    All five flags have to survive the card re-rendering itself — answering a
    question and toggling a pack are each their own POST, with nothing but the
    URL to carry the calling page's identity across. They are therefore handed
    to the templates pre-rendered as ``card_query``, one string built in one
    place, rather than every template that posts to one of these endpoints
    rebuilding the same querystring by hand.

    Nothing here decides ``finish_setup``. That flag belongs to one button on
    one page — the setup flow's Review step, which renders it in its wizard
    footer, outside this card. It used to be derived here, back when the button
    lived in the card header and every re-render of the card had to reconstruct
    it; moving the button out of the swapped region removed the problem rather
    than working around it.
    """
    profile = for_preview(build_profile_view(entity, as_of=as_of))
    preview = preview_entity(profile, country=entity.country)
    packs = suggest_packs(
        profile, preview, country=entity.country, accepted=_accepted_pack_codes(entity)
    )
    # Whether a calendar has ever been built, for the button's label — "Build"
    # reads as an offer, "Rebuild" as a correction, and confusing the two the
    # first time somebody opens a brand-new entity is the whole first
    # impression of the product. A run, not an instance count: an entity
    # with no registrations yet can run materialisation and truthfully
    # create nothing, and "Build" would then never stop being offered for
    # an entity that has already been asked.
    has_calendar = MaterialisationRun.objects.filter(entity=entity).exists()
    # The button only ever names a number while it still says "Create my
    # calendar" (see `entity_build_button_label.html`) — once a calendar
    # exists and this isn't the wizard, it says "Save changes" and no count is
    # rendered, so there is nothing here worth the extra planner run.
    #
    # `preview.applies_count` used to stand in for this number, which is a
    # count of *rules*, not of the rows a build creates — one rule can cover
    # several registrations and, over the horizon, several filing periods
    # each. `services.preview` runs the real planner against the entity's
    # actual, current facts (packs already accepted included, since those are
    # written to `ObligationInclusion` the moment a pack is toggled).
    #
    # The number shown is the *total* the register will hold once this build
    # runs, not `len(plan.to_create)` alone. Those agree for a brand-new
    # entity, but the guided setup's Build step is also reachable for an
    # entity that already has a calendar (browser back, a bookmarked step)
    # — there, a rule that already has its obligation on file needs no new
    # row, `to_create` is correctly empty, and a button reading "0
    # obligations" for an entity that plainly has some already reads as
    # broken rather than as "nothing changed". `live()` is the same "not
    # archived, not superseded" filter the calendar's own total tile counts
    # by, so this number matches what the calendar shows immediately
    # afterwards in both the first-build and the revisit case.
    show_obligation_count = not has_calendar or in_setup
    obligation_count = 0
    if show_obligation_count:
        plan = preview_materialisation(entity, as_of=as_of).plan
        live_now = live(ObligationInstance.objects.filter(entity=entity)).count()
        obligation_count = (
            live_now
            + len(plan.to_create)
            + len(plan.to_revive)
            - len(plan.to_archive)
            - len(plan.to_supersede)
        )
    return {
        "entity": entity,
        "preview": preview,
        "packs": packs,
        "total_count": obligation_count,
        "show_obligation_count": show_obligation_count,
        "has_calendar": has_calendar,
        "hide_build": hide_build,
        "in_setup": in_setup,
        "hide_questions": hide_questions,
        "hide_columns": hide_columns,
        "hide_packs": hide_packs,
        "card_query": _card_query(
            hide_build=hide_build,
            in_setup=in_setup,
            hide_questions=hide_questions,
            hide_columns=hide_columns,
            hide_packs=hide_packs,
        ),
    }


def _card_query(
    *,
    hide_build: bool,
    in_setup: bool,
    hide_questions: bool = False,
    hide_columns: bool = False,
    hide_packs: bool = False,
) -> str:
    """The querystring every action on this card has to carry back.

    Built here, once, so the flags cannot drift apart across the templates
    that post to these endpoints — see ``entity_preview_context``.
    """
    flags = (
        ("hide_build", hide_build),
        ("setup", in_setup),
        ("hide_questions", hide_questions),
        ("hide_columns", hide_columns),
        ("hide_packs", hide_packs),
    )
    params = [(name, "1") for name, on in flags if on]
    return f"?{urlencode(params)}" if params else ""


def _hide_build(request: HttpRequest) -> bool:
    """Whether this card is being shown mid-setup, one step before "build" —
    see ``entity_preview_context``. Read off the querystring rather than a
    session flag: every question/pack action that has to re-render this same
    card is its own POST, with nothing else to carry the calling page's
    identity across the request.
    """
    return request.GET.get("hide_build") == "1"


def _in_setup(request: HttpRequest) -> bool:
    """Whether this card is being shown inside the guided setup flow at all.

    Separate from :func:`_hide_build` because they disagree on the Review
    step: that step is in setup *and* shows its button.
    """
    return request.GET.get("setup") == "1"


def _hide_questions(request: HttpRequest) -> bool:
    """Whether the question queue is left off this render of the card — see
    ``entity_preview_context``."""
    return request.GET.get("hide_questions") == "1"


def _hide_columns(request: HttpRequest) -> bool:
    """Whether the category breakdown is left off this render of the card —
    see ``entity_preview_context``."""
    return request.GET.get("hide_columns") == "1"


def _hide_packs(request: HttpRequest) -> bool:
    """Whether the pack strip is left off this render of the card, on top of
    ``in_setup`` already gating it entirely on the steady-state page — see
    ``entity_preview_context``."""
    return request.GET.get("hide_packs") == "1"


@require_permission("compliance.obligation.view")
def entity_summary(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """What applies to one entity, for the entity detail page's Compliance card."""
    as_of = _today()
    entity = Entity.objects.filter(pk=entity_pk).first()
    if entity is None:
        raise Http404

    return render(
        request,
        ENTITY_SUMMARY_TEMPLATE,
        entity_preview_context(
            entity,
            as_of=as_of,
            hide_build=_hide_build(request),
            in_setup=_in_setup(request),
            hide_questions=_hide_questions(request),
            hide_columns=_hide_columns(request),
            hide_packs=_hide_packs(request),
        ),
    )


@require_permission("tenancy.profile.edit")
@require_http_methods(["POST"])
def answer_entity_question(request: HttpRequest, entity_pk: str, fact_key: str) -> HttpResponse:
    """Record one answer from the ranked question queue and re-rank what is left.

    Gated on ``tenancy.profile.edit``, the same permission ``confirm_obligation``
    uses for the same reason: the answer is written to the entity's compliance
    profile via ``record_fact``, not to one obligation.

    Re-ranking on every answer costs one evaluation of the catalog, well under a
    millisecond, and it is what makes the queue shrink faster than the user
    expects: answering "yes, GST registered" settles thirty definitions at once
    and the remaining questions reorder around what is left.

    A question stays in the queue once it is answered rather than disappearing
    (``stacos.obligations.questions.answered_questions``), so "Not sure yet" now
    has a real job here: chosen again on a fact that already carries a value,
    it is not "nothing was submitted" but "go back to not knowing", and has to
    clear that value rather than be silently skipped — the whole point of
    letting somebody come back and change their mind. It is only skipped for a
    ``fact_key`` the form did not recognise in the first place (``form.fact``
    unset), which nothing on screen can ever submit but a hand-built request
    could.
    """
    as_of = _today()
    entity = Entity.objects.filter(pk=entity_pk, archived_at__isnull=True).first()
    if entity is None:
        raise Http404

    form = QuestionForm(request.POST, fact_key=fact_key)

    toast = None
    if form.is_valid() and getattr(form, "fact", None) is not None:
        answer = form.answer()
        with transaction.atomic():
            record_fact(entity, fact_key, answer, actor=current_user(request), as_of=as_of)
            materialise(
                entity,
                as_of=as_of,
                trigger=MaterialisationRun.Trigger.MANUAL,
                actor=current_user(request),
            )
    elif not form.is_valid():
        # An invalid answer used to be discarded in silence — the field just
        # reverted on the next render with no explanation, which reads as the
        # page ignoring what was typed rather than as a rejected value.
        message = next(iter(form.errors.get("answer", ())), _("That answer could not be saved."))
        toast = Toast(str(message), level="danger")

    return _render_entity_preview(
        request,
        entity,
        as_of=as_of,
        toast=toast,
        hide_build=_hide_build(request),
        in_setup=_in_setup(request),
        hide_questions=_hide_questions(request),
        hide_columns=_hide_columns(request),
        hide_packs=_hide_packs(request),
    )


@require_permission("tenancy.profile.edit")
@require_http_methods(["POST"])
def toggle_entity_pack(request: HttpRequest, entity_pk: str, code: str) -> HttpResponse:
    """Add or remove one suggested compliance pack.

    Same permission as ``answer_entity_question``: accepting a pack is a
    decision about the entity's compliance profile, the same as answering a
    question is — see ``stacos.obligations.preview.adopt_pack``/``revoke_pack``.
    """
    as_of = _today()
    entity = Entity.objects.filter(pk=entity_pk, archived_at__isnull=True).first()
    if entity is None:
        raise Http404

    added = code not in _accepted_pack_codes(entity)
    with transaction.atomic():
        if added:
            adopt_pack(entity.tenant, entity, code, user=current_user(request))
        else:
            revoke_pack(entity, code)
        materialise(
            entity,
            as_of=as_of,
            trigger=MaterialisationRun.Trigger.MANUAL,
            actor=current_user(request),
        )

    return _render_entity_preview(
        request,
        entity,
        as_of=as_of,
        toast=Toast(_("Pack added.") if added else _("Pack removed.")),
        hide_build=_hide_build(request),
        in_setup=_in_setup(request),
        hide_questions=_hide_questions(request),
        hide_columns=_hide_columns(request),
        hide_packs=_hide_packs(request),
    )


def _render_entity_preview(
    request: HttpRequest,
    entity: Entity,
    *,
    as_of: date,
    toast: Toast | None,
    hide_build: bool = False,
    in_setup: bool = False,
    hide_questions: bool = False,
    hide_columns: bool = False,
    hide_packs: bool = False,
) -> HttpResponse:
    """Re-render the whole Compliance card after an answer or a pack toggle.

    One response rather than several, because the columns, the question queue,
    the pack strip and the build button are all views of one computation, and
    letting them arrive separately would show the user a momentarily
    inconsistent screen. Swapping the outer ``#entity-obligations`` section
    outerHTML — the same target ``rebuild_calendar`` swaps — is what keeps its
    ``aria-live`` region the single thing a screen reader has to watch.

    ``hide_build`` means the button itself is rendered outside this section —
    the wizard footer, currently — so the outerHTML swap above never touches
    it. Its obligation count still has to track every answer, so it is
    refreshed separately, out of band, by its own id
    (``entity_build_button.html``'s ``#build-button-label``) rather than by
    moving the button into the swapped region — see that template's comment
    on why ``finish_setup`` depends on the button staying put.
    """
    context = entity_preview_context(
        entity,
        as_of=as_of,
        hide_build=hide_build,
        in_setup=in_setup,
        hide_questions=hide_questions,
        hide_columns=hide_columns,
        hide_packs=hide_packs,
    )
    also = []
    if hide_build:
        also.append(
            Fragment(
                "obligations/_fragments/entity_build_button_label.html",
                {
                    "has_calendar": context["has_calendar"],
                    "total_count": context["total_count"],
                    "in_setup": context["in_setup"],
                },
                oob_target="build-button-label",
                swap="innerHTML",
            )
        )
    return oob(request, Fragment(ENTITY_SUMMARY_TEMPLATE, context), also=also, toast=toast)
