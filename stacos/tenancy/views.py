"""
The application shell's own views.

These are the reference implementation of the dual-render convention every later
module copies: one view, two templates, a direct GET renders the page and an HTMX
request renders only the fragment.
"""

from __future__ import annotations

from typing import Any, cast

from django.contrib import messages
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods
from django.views.generic import ListView

from stacos.core.htmx import Fragment, HtmxFragmentMixin, Toast, oob
from stacos.core.permissions import RequirePermissionMixin, require_permission
from stacos.tenancy.models import Entity, Membership
from stacos.tenancy.scope_resolver import SESSION_TENANT_KEY


@require_permission("tenancy.entity.view")
def dashboard(request: HttpRequest) -> HttpResponse:
    """Compliance health at a glance.

    Currently a shell: the counters it will carry come from the obligation
    register, which is the next milestone. It exists now so the app shell,
    navigation and fragment convention are exercised end to end.
    """
    entities = Entity.objects.select_related("tenant").filter(archived_at__isnull=True)

    context = {
        "entity_count": entities.count(),
        "entities": entities[:6],
        "tenant": getattr(request, "tenant", None),
    }

    template = (
        "tenancy/_fragments/dashboard_body.html"
        if getattr(request, "htmx", False)
        else "tenancy/dashboard.html"
    )
    return render(request, template, context)


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
        queryset = (
            Entity.objects.select_related("tenant")
            .filter(archived_at__isnull=True)
            .annotate(
                registration_count=Count(
                    "registrations", filter=Q(registrations__archived_at__isnull=True)
                )
            )
            .order_by("name")
        )

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

        return cast("QuerySet[Entity]", queryset)

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
    """
    tenant_id = request.POST.get("tenant_id", "")
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
