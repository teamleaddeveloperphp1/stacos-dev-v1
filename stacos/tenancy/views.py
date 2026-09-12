"""
The application shell's own views.

These are the reference implementation of the dual-render convention every later
module copies: one view, two templates, a direct GET renders the page and an HTMX
request renders only the fragment.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from django.contrib import messages
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import Count, Q, QuerySet
from django.forms.models import model_to_dict
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods
from django.views.generic import ListView

from stacos.core.audit import diff_fields, record_event
from stacos.core.htmx import Fragment, HtmxFragmentMixin, Toast, oob
from stacos.core.models import AuditAction
from stacos.core.permissions import RequirePermissionMixin, require_permission
from stacos.core.rls import rls_bootstrap
from stacos.core.typing import current_user
from stacos.tenancy.forms import EntityForm, PremisesForm, RegistrationForm
from stacos.tenancy.models import Entity, EntityProfile, Membership
from stacos.tenancy.scope_resolver import SESSION_TENANT_KEY

#: The one modal that both creates and edits an entity.
ENTITY_FORM_TEMPLATE = "tenancy/_fragments/entity_form_modal.html"

#: What an edit is allowed to change, and therefore what the audit entry has to
#: describe. Taken from the form so the two cannot drift apart.
AUDITED_ENTITY_FIELDS = list(EntityForm.Meta.fields)


def _scaled_bars(rows: list[tuple[Any, int, str]]) -> list[dict[str, Any]]:
    """Bar-list rows scaled to the busiest one, not stacked to a 100% total.

    Shared by the category, weekly-workload and overdue-ageing panels so the
    "scale to the busiest row" rule lives in one place rather than three.
    """
    busiest = max((count for _, count, _ in rows), default=0) or 1
    return [
        {"label": label, "count": count, "pct": round(count / busiest * 100, 1), "color": color}
        for label, count, color in rows
    ]


def _donut_geometry(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach SVG ring geometry to a list of ``{status, label, value}`` segments.

    A circle of radius 15.915 has a circumference of ~100, so normalising every
    segment to a percentage of the total lets ``stroke-dasharray`` /
    ``stroke-dashoffset`` be plain percentages — no unit conversion in the
    template, and no running sum that Django's template language cannot do
    cleanly on its own.
    """
    total = sum(segment["value"] for segment in segments) or 1
    cumulative = 0.0
    geometry = []
    for segment in segments:
        pct = segment["value"] / total * 100
        geometry.append(
            {
                **segment,
                "pct": pct,
                "dasharray": f"{pct:.3f} {100 - pct:.3f}",
                "offset": -cumulative,
            }
        )
        cumulative += pct
    return geometry


@require_permission("tenancy.entity.view")
def dashboard(request: HttpRequest) -> HttpResponse:
    """Compliance health at a glance.

    The headline counters come from one conditional aggregation over the register
    rather than several ``.count()`` calls, so the tiles cannot disagree with each
    other — they were computed from one snapshot.
    """
    from datetime import timedelta

    from django.utils import timezone

    from stacos.engine.lifecycle import OPEN_STATES
    from stacos.obligations.queries import (
        category_counts,
        overdue_aging,
        status_counts,
        upcoming,
        weekly_workload,
    )

    as_of = timezone.localdate()
    open_states = [str(s) for s in OPEN_STATES]
    entities = (
        Entity.objects.select_related("tenant")
        .filter(archived_at__isnull=True)
        .annotate(
            overdue_count=Count(
                "obligations",
                filter=Q(
                    obligations__archived_at__isnull=True,
                    obligations__superseded_at__isnull=True,
                    obligations__state__in=open_states,
                    obligations__due_date__isnull=False,
                    obligations__due_date__lt=as_of,
                ),
            )
        )
    )

    # Everything below defaults to every entity in the tenant combined. Picking
    # one from `?entity=` narrows every breakdown to it — the same `entity_ids`
    # parameter each query function already takes for exactly this reason.
    selected_entity = None
    entity_ids: list[Any] | None = None
    requested_entity_id = request.GET.get("entity", "").strip()
    if requested_entity_id:
        try:
            requested_uuid: UUID | None = UUID(requested_entity_id)
        except ValueError:
            # A stale bookmark or a hand-edited URL, not a real entity id —
            # fall back to "all entities" rather than a 500 on a malformed
            # query string filtering a UUID field.
            requested_uuid = None
        if requested_uuid is not None:
            selected_entity = entities.filter(id=requested_uuid).first()
            if selected_entity is not None:
                entity_ids = [selected_entity.id]

    counts = status_counts(as_of=as_of, entity_ids=entity_ids)
    # `overdue` and `due_soon` are disjoint subsets of `open` (split on
    # `due_date` vs. `as_of`), so this can never go negative — unlike deriving
    # it from `total`, which also includes NOT_APPLICABLE/DISPUTED states.
    pending = counts["open"] - counts["overdue"] - counts["due_soon"]

    # Colour cycles through a small fixed palette — categories aren't part of
    # the status vocabulary and must not borrow its colours, which each mean
    # something specific about health.
    category_rows = category_counts(entity_ids=entity_ids)
    categories = _scaled_bars(
        [
            (row["label"], row["count"], f"category-{(index % 6) + 1}")
            for index, row in enumerate(category_rows)
        ]
    )

    # "Overdue" plus each of the next few weeks, so a backlog and a look-ahead
    # share one chart without a negative week number.
    workload = weekly_workload(as_of=as_of, entity_ids=entity_ids)
    weekly_pairs: list[tuple[Any, int, str]] = [
        (_("Overdue"), workload["overdue"], "status-overdue")
    ]
    for week_index, count in enumerate(workload["weeks"]):
        start = as_of + timedelta(days=week_index * 7)
        end = start + timedelta(days=6)
        weekly_pairs.append((f"{start:%d %b}–{end:%d %b}", count, "status-in-progress"))
    weekly_bars = _scaled_bars(weekly_pairs) if any(count for _, count, _ in weekly_pairs) else []

    # A flat "overdue" count conflates a two-day slip with a two-month one;
    # this buckets by how late, all in the same colour since it is one status
    # split by recency, not several statuses.
    aging = overdue_aging(as_of=as_of, entity_ids=entity_ids)
    aging_pairs: list[tuple[Any, int, str]] = [
        (_("1–7 days late"), aging["recent"], "status-overdue"),
        (_("8–30 days late"), aging["stale"], "status-overdue"),
        (_("31+ days late"), aging["old"], "status-overdue"),
    ]
    aging_bars = _scaled_bars(aging_pairs) if any(count for _, count, _ in aging_pairs) else []

    # The entity list itself narrows to match: showing every entity's row while
    # every number above is scoped to one of them would make the page look
    # like it disagrees with itself.
    entities_display = entities.filter(id=selected_entity.id) if selected_entity else entities[:6]

    # A separate, lean query rather than `.only("id", "name")` on `entities`:
    # that queryset already carries `select_related("tenant")`, and Django
    # raises `FieldError` ("cannot be both deferred and traversed") for a
    # field that is both select-related and left out of `.only()`. `{% if %}`
    # swallows exceptions while resolving its condition, so that error never
    # surfaced as a 500 — the dropdown just silently never rendered.
    entity_options = (
        Entity.objects.filter(archived_at__isnull=True).order_by("name").only("id", "name")
    )

    # Appended verbatim after `?status=...` on every stat tile's (and now the
    # donut legend's) link to the calendar, so a tile clicked while the
    # dashboard is narrowed to one entity lands on that entity's rows, not
    # every entity's.
    dashboard_entity_qs = f"&entity={selected_entity.id}" if selected_entity else ""
    calendar_url = reverse("compliance:calendar")

    context = {
        "entity_count": entities.count(),
        "entities": entities_display,
        "entity_options": entity_options,
        "selected_entity": selected_entity,
        "dashboard_entity_qs": dashboard_entity_qs,
        "tenant": getattr(request, "tenant", None),
        "as_of": as_of,
        "counts": counts,
        "pending": pending,
        "categories": categories,
        "weekly_bars": weekly_bars,
        "aging_bars": aging_bars,
        "health_segments": _donut_geometry(
            [
                {
                    "status": "overdue",
                    "label": _("Overdue"),
                    "value": counts["overdue"],
                    "href": f"{calendar_url}?status=overdue{dashboard_entity_qs}",
                },
                {
                    "status": "due-soon",
                    "label": _("Due soon"),
                    "value": counts["due_soon"],
                    "href": f"{calendar_url}?status=due_soon{dashboard_entity_qs}",
                },
                {
                    "status": "pending",
                    "label": _("Pending"),
                    "value": pending,
                    "href": f"{calendar_url}?status=pending{dashboard_entity_qs}",
                },
                {
                    "status": "on-track",
                    "label": _("Completed"),
                    "value": counts["completed"],
                    "href": f"{calendar_url}?status=completed{dashboard_entity_qs}",
                },
            ]
        ),
        "upcoming": upcoming(as_of=as_of, within_days=30, entity_ids=entity_ids).select_related(
            "entity"
        )[:8],
    }

    template = (
        "tenancy/_fragments/dashboard_body.html"
        if getattr(request, "htmx", False)
        else "tenancy/dashboard.html"
    )
    return render(request, template, context)


def entity_rows() -> QuerySet[Entity]:
    """The queryset every entity *row* is rendered from.

    ``registration_count`` is an annotation, not a model field, and
    ``entity_row.html`` falls back to zero when it is absent. That fallback is
    right for a newly created entity and wrong for an edited one, which is why
    the list and the edit response share this rather than each building their
    own.
    """
    # `annotate()` widens the row type to Any, and the cast used to sit at the
    # end of the list view's `get_queryset`. It belongs here now, where the
    # annotation is introduced.
    return cast(
        "QuerySet[Entity]",
        Entity.objects.select_related("tenant")
        .filter(archived_at__isnull=True)
        .annotate(
            registration_count=Count(
                "registrations", filter=Q(registrations__archived_at__isnull=True)
            )
        )
        .order_by("name"),
    )


class EntityListView(RequirePermissionMixin, HtmxFragmentMixin, ListView[Entity]):
    """Entity list — the pattern every later list view follows.

    Note the query discipline: ``select_related`` on the tenant and an annotated
    count instead of a per-row query. N+1 is the actual reason server-rendered
    applications feel slow; HTMX only makes it more visible.
    """

    required_permissions = ("tenancy.entity.view",)
    model = Entity
    context_object_name = "entities"
    paginate_by = 25
    template_name = "tenancy/entity_list.html"

    def get_queryset(self) -> QuerySet[Entity]:
        queryset = entity_rows()

        search = self.request.GET.get("q", "").strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(legal_name__icontains=search)
                | Q(short_code__icontains=search)
            )

        status = self.request.GET.get("status", "").strip()
        if status:
            queryset = queryset.filter(status=status)

        return queryset

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["search"] = self.request.GET.get("q", "")
        context["status"] = self.request.GET.get("status", "")
        context["status_choices"] = Entity.Status.choices
        return context


@require_permission("tenancy.tenant.view")
@require_http_methods(["POST"])
def switch_tenant(request: HttpRequest) -> HttpResponse:
    """Change which tenant the user is working in.

    Filtered by ``user`` so a forged tenant id in the form cannot reach a tenant
    the user is not a member of — the switcher is a convenience, never an
    authorisation boundary.

    ``rls_bootstrap`` is load-bearing, and its absence is why switching appeared
    to be broken rather than merely restricted. ``objects_unscoped`` lifts the
    *ORM* tenant filter and nothing else; PostgreSQL's Row-Level Security is
    still pointed at the tenant the user is currently in, because
    ``ScopeMiddleware`` published that set for this transaction. The membership
    row being switched *to* therefore belongs to a tenant the policy excludes,
    the lookup returns nothing, and a user who genuinely belongs to both
    organisations is told they are not a member of the second one. Both layers
    have to be satisfied, which is the whole point of having two.

    The transaction the setting is local to is already open — ``ScopeMiddleware``
    opened it. Same bootstrap as ``scope_resolver._select_membership`` and the
    mobile ``/me/`` endpoint, and for the same reason.
    """
    tenant_id = request.POST.get("tenant_id", "")
    with rls_bootstrap():
        membership = (
            Membership.objects_unscoped.filter(
                user=request.user, tenant_id=tenant_id, status=Membership.Status.ACTIVE
            )
            .select_related("tenant")
            .first()
        )

    if membership is None:
        messages.error(request, _("You are not a member of that organisation."))
        return redirect("/app/")

    request.session[SESSION_TENANT_KEY] = str(membership.tenant_id)

    if getattr(request, "htmx", False):
        # A tenant switch changes every part of the shell, so the honest response
        # is a full navigation rather than a partial swap.
        response = HttpResponse(status=204)
        response["HX-Redirect"] = "/app/"
        return response

    return redirect("/app/")


#: Fixed destinations the palette always offers, filtered by what was typed.
#:
#: The fourth element is the chord hint the palette renders as ``<kbd>`` keys, so
#: it has to agree with the ``SHORTCUTS`` map in ``assets/js/app.js``. Advertising
#: a chord that goes somewhere else is worse than advertising none.
PALETTE_DESTINATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("Dashboard", "app:dashboard", "home", "g o"),
    ("Entities", "app:entity_list", "building", "g e"),
    ("Compliance calendar", "compliance:calendar", "calendar", "g c"),
    ("Security and devices", "accounts:security", "shield", ""),
)


@require_permission("core.search")
def palette_search(request: HttpRequest) -> HttpResponse:
    """Search behind the command palette.

    Trigram similarity as well as substring matching, so "vaibav" still finds
    "Vaibhav" — a CA typing fast from memory across three hundred clients will
    misspell things, and an exact-match-only palette is one they stop using.

    Scoped by the default manager like everything else, so a practice user
    searching only ever reaches the client entities their engagements cover.
    """
    query = request.GET.get("q", "").strip()

    entities: list[Entity] = []
    if query:
        entities = list(
            Entity.objects.filter(archived_at__isnull=True)
            .annotate(similarity=TrigramSimilarity("name", query))
            .filter(
                Q(name__icontains=query)
                | Q(short_code__icontains=query)
                | Q(legal_name__icontains=query)
                | Q(similarity__gt=0.2)
            )
            .order_by("-similarity", "name")[:8]
        )

    lowered = query.lower()
    destinations = [
        {"label": label, "url": reverse(route), "icon": icon, "keys": keys}
        for label, route, icon, keys in PALETTE_DESTINATIONS
        if not query or lowered in label.lower()
    ]

    return render(
        request,
        "tenancy/_fragments/palette_results.html",
        {"query": query, "entities": entities, "destinations": destinations},
    )


@require_permission("tenancy.entity.create")
@require_http_methods(["GET", "POST"])
def entity_create(request: HttpRequest) -> HttpResponse:
    """Create an entity, in a modal loaded on demand.

    The two render paths differ here in a way worth noting: a GET returns just
    the modal markup for injection, and a successful POST returns the new row
    plus out-of-band updates for the sidebar counter and a toast — one request,
    three regions, no refetch.

    A validation failure re-renders **only the form fragment** with a 422, so the
    modal stays open and the user keeps what they typed.
    """
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404

    form = EntityForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        entity = form.save(commit=False)
        entity.tenant = tenant
        entity.country = tenant.country
        entity.full_clean(exclude=["tenant"])
        entity.save()

        # An entity with no profile has nothing for the compliance engine to
        # evaluate, so one is always created alongside it.
        EntityProfile.objects.get_or_create(entity=entity, defaults={"tenant": tenant})

        record_event(action=AuditAction.CREATE, actor=current_user(request), obj=entity)

        remaining = Entity.objects.filter(archived_at__isnull=True).count()

        return oob(
            request,
            # `registration_count` is a queryset annotation on the list view,
            # not a model field. A brand-new entity has none, and the template
            # falls back to zero.
            Fragment("tenancy/_fragments/entity_row.html", {"entity": entity}),
            also=[
                Fragment(
                    "tenancy/_fragments/entity_count.html",
                    {"entity_count": remaining},
                    oob_target="entity-count",
                )
            ],
            toast=Toast(_("%(name)s added.") % {"name": entity.name}),
            triggers={"stacos:modal-close": True},
        )

    status = 422 if request.method == "POST" else 200
    return render(
        request,
        ENTITY_FORM_TEMPLATE,
        {
            "form": form,
            "tenant": tenant,
            # One template serves both create and edit. What differs is where it
            # posts and what it swaps, so the view says — a conditional in the
            # markup would have to know about both, and would be read wrong the
            # first time someone changed one of them.
            "form_action": reverse("app:entity_create"),
            "form_target": "#entity-rows tbody",
            "form_swap": "afterbegin",
            "modal_title": _("Add an entity"),
            "submit_label": _("Add entity"),
        },
        status=status,
    )


@require_permission("tenancy.entity.edit")
@require_http_methods(["GET", "POST"])
def entity_edit(request: HttpRequest, pk: str) -> HttpResponse:
    """Correct an entity's details, in the same modal that creates one.

    A typo in a name, the wrong entity type, a registered office in the wrong
    state: before this existed the only remedy was to archive the entity and
    start again, which throws away every obligation, document and audit entry
    attached to it.

    Fetched through the scoped manager, so another tenant's id in the address is
    a 404 rather than an edit — 404 and not 403 for the reason ``entity_detail``
    gives. Archived entities are excluded: an archived entity is history, and
    history is not retyped.

    Deliberately does *not* rebuild the calendar. ``entity_type`` and
    ``registered_office_state`` both drive applicability, so an edit can change
    what the entity owes — and this product's rule is that such a change
    produces a reviewable plan rather than taking effect silently. The toast
    points at "Rebuild calendar", which is that review.
    """
    entity = Entity.objects.filter(pk=pk, archived_at__isnull=True).first()
    if entity is None:
        raise Http404

    # Snapshot before the form binds. `EntityForm(..., instance=entity)` writes
    # the submitted values onto the instance during `is_valid()`, so by the time
    # there is something to compare against, the "before" side is already gone.
    original = model_to_dict(entity, fields=AUDITED_ENTITY_FIELDS)

    form = EntityForm(request.POST or None, instance=entity)

    if request.method == "POST" and form.is_valid():
        entity = form.save(commit=False)
        entity.full_clean(exclude=["tenant"])
        entity.save()

        before, after = diff_fields(entity, fields=AUDITED_ENTITY_FIELDS, original=original)
        record_event(
            action=AuditAction.UPDATE,
            actor=current_user(request),
            obj=entity,
            before=before,
            after=after,
        )

        # Re-read so the row carries `registration_count`. Falling back to the
        # saved instance rather than letting `None` through: the count would be
        # wrong, but an empty `<tr>` swapped into the row's own id is worse —
        # the list would appear to lose the entity that was just corrected.
        row = entity_rows().filter(pk=entity.pk).first() or entity

        return oob(
            request,
            Fragment("tenancy/_fragments/entity_row.html", {"entity": row}),
            toast=Toast(
                _("%(name)s updated. Rebuild its calendar to apply the change.")
                % {"name": entity.name}
            ),
            triggers={"stacos:modal-close": True},
        )

    status = 422 if request.method == "POST" else 200
    return render(
        request,
        ENTITY_FORM_TEMPLATE,
        {
            "form": form,
            "entity": entity,
            "form_action": reverse("app:entity_edit", args=[entity.pk]),
            "form_target": f"#entity-row-{entity.pk}",
            "form_swap": "outerHTML",
            "modal_title": _("Edit entity"),
            "submit_label": _("Save changes"),
        },
        status=status,
    )


@require_permission("tenancy.entity.view")
def entity_detail(request: HttpRequest, pk: str) -> HttpResponse:
    entity = Entity.objects.select_related("tenant", "profile").filter(pk=pk).first()
    if entity is None:
        # 404, not 403: confirming that an entity exists in another tenant is
        # itself a disclosure.
        from django.http import Http404

        raise Http404

    context = {
        "entity": entity,
        "registrations": entity.registrations.filter(archived_at__isnull=True),
        "premises": entity.premises.filter(archived_at__isnull=True),
    }
    template = (
        "tenancy/_fragments/entity_detail_body.html"
        if getattr(request, "htmx", False)
        else "tenancy/entity_detail.html"
    )
    return render(request, template, context)


@require_permission("tenancy.registration.manage")
@require_http_methods(["GET", "POST"])
def registration_create(request: HttpRequest, pk: str) -> HttpResponse:
    """Add a registration to an entity, in a modal loaded on demand.

    Follows ``entity_create`` exactly, with one difference worth stating: the
    response re-renders the whole ``<tbody>`` rather than prepending a row. The
    registrations table shows an empty state while it has no rows, and prepending
    would leave "No registrations recorded" sitting underneath the registration
    that was just recorded. Re-rendering the body also keeps the model's own
    ordering, so an added row lands where a refresh would put it.

    The entity comes from the URL and is re-fetched through the scoped manager,
    so a registration cannot be attached to another tenant's entity by editing
    the address.
    """
    entity = Entity.objects.filter(pk=pk, archived_at__isnull=True).first()
    if entity is None:
        # 404 rather than 403, for the same reason as `entity_detail`.
        raise Http404

    form = RegistrationForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        registration = form.save(commit=False)
        registration.tenant = entity.tenant
        registration.entity = entity
        registration.full_clean(exclude=["tenant", "entity"])
        registration.save()

        record_event(action=AuditAction.CREATE, actor=current_user(request), obj=registration)

        return oob(
            request,
            Fragment(
                "tenancy/_fragments/registration_rows.html",
                {
                    "entity": entity,
                    "registrations": entity.registrations.filter(archived_at__isnull=True),
                },
            ),
            toast=Toast(
                _("%(type)s recorded.") % {"type": registration.type},
            ),
            triggers={"stacos:modal-close": True},
        )

    status = 422 if request.method == "POST" else 200
    return render(
        request,
        "tenancy/_fragments/registration_form_modal.html",
        {"form": form, "entity": entity},
        status=status,
    )


@require_permission("tenancy.premises.manage")
@require_http_methods(["GET", "POST"])
def premises_create(request: HttpRequest, pk: str) -> HttpResponse:
    """Add a premises to an entity, in a modal loaded on demand.

    Follows ``registration_create`` exactly, for the same reasons: the entity
    comes from the URL and is re-fetched through the scoped manager, so a
    premises cannot be attached to another tenant's entity by editing the
    address, and the response re-renders the whole ``<tbody>`` rather than
    prepending a row, so the empty state disappears with the first row
    instead of sitting underneath it.
    """
    entity = Entity.objects.filter(pk=pk, archived_at__isnull=True).first()
    if entity is None:
        # 404 rather than 403, for the same reason as `entity_detail`.
        raise Http404

    form = PremisesForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        premises = form.save(commit=False)
        premises.tenant = entity.tenant
        premises.entity = entity
        premises.full_clean(exclude=["tenant", "entity"])
        premises.save()

        record_event(action=AuditAction.CREATE, actor=current_user(request), obj=premises)

        return oob(
            request,
            Fragment(
                "tenancy/_fragments/premises_rows.html",
                {
                    "entity": entity,
                    "premises": entity.premises.filter(archived_at__isnull=True),
                },
            ),
            toast=Toast(_("%(name)s recorded.") % {"name": premises.name}),
            triggers={"stacos:modal-close": True},
        )

    status = 422 if request.method == "POST" else 200
    return render(
        request,
        "tenancy/_fragments/premises_form_modal.html",
        {"form": form, "entity": entity},
        status=status,
    )


@require_permission("tenancy.entity.archive")
@require_http_methods(["POST"])
def archive_entity(request: HttpRequest, pk: str) -> HttpResponse:
    """Archive an entity, updating several regions in one response.

    This is the out-of-band pattern the whole product uses: the row, the sidebar
    counter and a toast all change from a single request, with no refetch.
    """
    entity = Entity.objects.filter(pk=pk).first()
    if entity is None:
        from django.http import Http404

        raise Http404

    entity.archive(reason=request.POST.get("reason", ""))

    remaining = Entity.objects.filter(archived_at__isnull=True).count()
    return oob(
        request,
        main="",  # the row is removed
        also=[
            Fragment(
                "tenancy/_fragments/entity_count.html",
                {"entity_count": remaining},
                oob_target="entity-count",
            )
        ],
        toast=Toast(_("%(name)s archived.") % {"name": entity.name}),
    )
