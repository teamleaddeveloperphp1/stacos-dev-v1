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

from datetime import date, timedelta
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.catalog.models import ComplianceDefinition, DefinitionVersion, PublicationStatus
from stacos.core.audit import record_event
from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.models import AuditAction
from stacos.core.pagination import filters_querystring
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.engine.lifecycle import CLOSED_STATES, DUE_SOON_DAYS, OPEN_STATES, State
from stacos.engine.types import occurrence_number
from stacos.jurisdictions.events import EVENT_TYPES
from stacos.obligations.forms import (
    STATUS_FILTERS,
    AssignForm,
    EntityEventForm,
    RecordEventForm,
    TransitionForm,
    obligation_display,
)
from stacos.obligations.models import (
    EntityEvent,
    MaterialisationRun,
    ObligationEvent,
    ObligationInstance,
)
from stacos.obligations.queries import annotate_status, keyset_page, live, status_counts
from stacos.obligations.services import materialise
from stacos.obligations.transitions import (
    TransitionError,
    apply_transition,
    assign,
    available_actions,
)
from stacos.tenancy.forms import QuestionForm
from stacos.tenancy.models import ComplianceCategory, Entity
from stacos.tenancy.services import record_fact

PAGE_SIZE = 50

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
        # A human's own "yes, this applies" settles an opted-in row the rule
        # itself will never confirm — it must drop out of this filter once
        # someone has actually looked, the same as `status_counts` above.
        queryset = queryset.filter(state__in=_OPEN, confirmed=False, confirmed_by_user=False)
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

    context = {
        "obligation": obligation,
        "definition": definition,
        "as_of": as_of,
        "events": obligation.events.select_related("actor")[:50],
        **_action_context(obligation, _permissions(request)),
        "form": TransitionForm(),
        "event_form": (
            EntityEventForm(initial={"key": obligation.needs_input})
            if obligation.needs_input
            else None
        ),
    }

    template = (
        "obligations/_fragments/detail_body.html"
        if is_fragment_request(request)
        else "obligations/detail.html"
    )
    return render(request, template, context)


def _action_context(obligation: ObligationInstance, permissions: frozenset[str]) -> dict[str, Any]:
    """The three shapes the detail panel renders actions as.

    A plain state change is a button; a filing needs its acknowledgement number
    recorded as evidence, so it gets its own boxed form; a judgement call
    (deferring, disputing, marking not applicable) needs a reason, so it is a
    tile that only opens its note field once chosen. Grouped here, once, rather
    than in the template, so an empty group renders no container at every one
    of this view's four render sites.
    """
    actions = available_actions(obligation, permissions=permissions)
    return {
        "actions": actions,
        "plain_actions": [
            a for a in actions if not a.requires_note and not a.requires_filing_reference
        ],
        "filing_actions": [a for a in actions if a.requires_filing_reference],
        "note_actions": [a for a in actions if a.requires_note and not a.requires_filing_reference],
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
            {
                "obligation": refreshed,
                "as_of": as_of,
                **_action_context(refreshed, _permissions(request)),
                "form": TransitionForm(),
                "events": refreshed.events.select_related("actor")[:50],
            },
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
                {
                    "obligation": refreshed,
                    "as_of": as_of,
                    **_action_context(refreshed, _permissions(request)),
                    "form": TransitionForm(),
                    "events": refreshed.events.select_related("actor")[:50],
                },
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


def _detail_error(
    request: HttpRequest,
    obligation: ObligationInstance,
    message: str,
    *,
    status: int = 422,
) -> HttpResponse:
    """Re-render the action panel with the problem stated, keeping the page put."""
    return render(
        request,
        "obligations/_fragments/detail_panel.html",
        {
            "obligation": obligation,
            "as_of": _today(),
            **_action_context(obligation, _permissions(request)),
            "form": TransitionForm(),
            "events": obligation.events.select_related("actor")[:50],
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
            {
                "obligation": refreshed,
                "as_of": as_of,
                **_action_context(refreshed, _permissions(request)),
                "form": TransitionForm(),
                "events": refreshed.events.select_related("actor")[:50],
            },
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
# Only obligations with a fact to ask about get the action. One that is
# unconfirmed because a person opted into it by hand has an empty `missing_facts`
# and stays a plain badge: there is no question, and inventing one would be
# worse than the silence.
# ---------------------------------------------------------------------------


def _askable_questions(obligation: ObligationInstance, *, as_of: date) -> list[Any]:
    """The blocking facts, ranked, in the order worth asking them.

    Reuses ``rank_questions`` — the same pure function the setup wizard ranks by,
    against the same fact registry — so the questions asked here are the questions
    asked there, in the same order, phrased the same way. It also does the work
    this view could not do for itself: mapping a fact nobody can answer directly
    onto one they can, and dropping the facts that are derived from a
    registration or a premises rather than typed by a person.
    """
    from stacos.catalog.snapshots import build_catalog
    from stacos.obligations.profile import build_profile_view
    from stacos.tenancy.onboarding.questions import askable_facts, rank_questions

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
    profile changes what applies. ``tenancy.onboarding.start`` would be the wrong
    one twice over — it is the pre-tenant permission, and it is granted to people
    who are not members of this tenant at all.
    """
    as_of = _today()
    obligation = _get(pk, as_of=as_of)
    questions = _askable_questions(obligation, as_of=as_of)

    if not questions and not obligation.missing_facts:
        # Nothing was ever unknown about this one — it is unconfirmed because a
        # person opted into it by hand. There is no fact to ask about, but a
        # human can still say whether it applies — see `_confirm_opt_in`.
        if obligation.confirmed_by_user:
            # Already settled; the row should not have offered a Confirm
            # control at all. A stale page or a guessed URL, not a real state.
            raise Http404
        return _confirm_opt_in(request, obligation, as_of=as_of)

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


def _confirm_opt_in(
    request: HttpRequest, obligation: ObligationInstance, *, as_of: date
) -> HttpResponse:
    """Yes or no, for an obligation nobody's rule ever decided.

    There is no fact behind this one — a person chose to add it, via a pack or
    by hand — so the only question left is the plainest one: does it actually
    apply? "Yes" has to survive the next materialisation run on its own
    (`ObligationInstance.confirmed_by_user`, never touched by the planner);
    "no" reuses the calendar's existing dismissal machinery
    (`stacos.obligations.transitions.apply_transition`) rather than writing a
    second suppression path, which also means it is gated on the real
    ``compliance.obligation.dismiss`` permission, not waved through just
    because this modal happened to open.
    """
    if request.method == "GET":
        return render(
            request,
            "obligations/_fragments/confirm_opt_in.html",
            {"obligation": obligation},
        )

    decision = request.POST.get("decision", "")

    if decision == "yes":
        obligation.confirmed_by_user = True
        obligation.save(update_fields=["confirmed_by_user", "updated_at"])
        record_event(
            action=AuditAction.UPDATE,
            actor=current_user(request),
            obj=obligation,
            before={"confirmed_by_user": False},
            after={"confirmed_by_user": True},
            context={"channel": "calendar-confirm"},
        )
        return _confirmed_response(
            request,
            obligation.pk,
            as_of=as_of,
            changed_message=_("Confirmed — this stays on your calendar."),
        )

    if decision == "no":
        try:
            apply_transition(
                obligation,
                target=State.NOT_APPLICABLE,
                actor=current_user(request),
                permissions=_permissions(request),
                note=_(
                    "Marked not applicable — declined via the compliance "
                    "calendar's Confirm control."
                ),
                as_of=as_of,
            )
        except TransitionError as exc:
            # A permission the confirm modal cannot itself see, or a race
            # with someone else's change — either way, a clean message beats
            # a stack trace.
            status = 403 if exc.code == "forbidden" else 422
            return oob(request, "", toast=Toast(str(exc), level="danger"), status=status)

        counts = status_counts(as_of=as_of)
        return oob(
            request,
            f'<tr id="obligation-{obligation.pk}" hx-swap-oob="delete"></tr>',
            also=[
                Fragment(
                    "obligations/_fragments/status_counts.html",
                    {"counts": counts},
                    oob_target="calendar-counts",
                )
            ],
            toast=Toast(_("Marked not applicable.")),
            triggers={"stacos:modal-close": True},
        )

    raise Http404


def _confirmed_response(
    request: HttpRequest,
    pk: str | UUID,
    *,
    as_of: date,
    changed_message: str,
    removed_message: str | None = None,
) -> HttpResponse:
    """Update the row and the counters, and say what actually changed.

    The obligation may no longer exist: an answer that resolves the rule to FALSE
    archives it, which is the correct outcome and a confusing one to discover as
    a blank row. So the row is removed rather than re-rendered, and the toast
    says which way it went — ``removed_message`` for that case, falling back
    to ``changed_message`` for a caller (the opt-in "yes" path) that never
    reaches it, since flipping ``confirmed_by_user`` never archives anything.

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
            toast=Toast(removed_message or changed_message),
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
    first thing a user asks during onboarding.
    """
    as_of = _today()
    entity = Entity.objects.filter(pk=entity_pk, archived_at__isnull=True).first()
    if entity is None:
        raise Http404

    run = materialise(entity, as_of=as_of, trigger="MANUAL", actor=current_user(request))

    # The re-rendered panel, not the calendar's counter strip. This button only
    # exists inside the entity summary card, and that page has no
    # `#calendar-counts` to swap — so the counters were being rendered into a
    # response the caller then discarded, and the user saw the toast fire while
    # the table in front of them stayed stale until a manual reload.
    return oob(
        request,
        Fragment(
            ENTITY_SUMMARY_TEMPLATE,
            _entity_summary_context(entity, as_of=as_of),
        ),
        toast=Toast(
            _("Calendar rebuilt: %(summary)s.") % {"summary": run.summary()},
            level="success" if not run.needs_review else "warning",
        ),
        triggers={"stacos:calendar-rebuilt": {"summary": run.summary()}},
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


def _entity_summary_context(entity: Entity, *, as_of: date) -> dict[str, Any]:
    """Per-category counts for one entity.

    Shared by the panel's own endpoint and by ``rebuild_calendar``, which has to
    render the same card so the table the user is looking at reflects the plan
    that just ran.
    """
    rows = (
        live()
        .filter(entity=entity, state__in=_OPEN)
        .values("category")
        .annotate(
            total=Count("id"),
            overdue=Count("id", filter=Q(due_date__lt=as_of)),
        )
        .order_by("category")
    )

    labels = dict(ComplianceCategory.choices)
    return {
        "entity": entity,
        "as_of": as_of,
        "rows": [{**row, "label": labels.get(row["category"], row["category"])} for row in rows],
        "timeline": ObligationEvent.objects.filter(entity=entity).select_related("actor")[:10],
    }


@require_permission("compliance.obligation.view")
def entity_summary(request: HttpRequest, entity_pk: str) -> HttpResponse:
    """Per-category counts for one entity, for the entity detail page."""
    as_of = _today()
    entity = Entity.objects.filter(pk=entity_pk).first()
    if entity is None:
        raise Http404

    return render(
        request,
        ENTITY_SUMMARY_TEMPLATE,
        _entity_summary_context(entity, as_of=as_of),
    )
