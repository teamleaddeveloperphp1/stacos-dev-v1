"""
The people in this organisation, and the invitations that have not landed yet.

Small on purpose. The interesting decisions live in
:mod:`stacos.tenancy.invitations`; this is the screen over them.

Every view here is gated on ``accounts.user.invite`` rather than on a view-only
permission. Listing who has access to a workspace is not sensitive to a member,
but this page is also where access is granted and withdrawn, and splitting it
into a readable half and a writable half would be two screens for one job.
"""

from __future__ import annotations

from typing import Any, cast

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, derive_fragment_template, is_fragment_request, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.tenancy.access import (
    ACCESS_MANAGER_PERMISSION,
    AccessError,
    default_categories_for,
    describe_reach,
    permission_choices,
    set_member_access,
    set_member_permissions,
)
from stacos.tenancy.forms import InviteColleagueForm, MemberAccessForm
from stacos.tenancy.invitations import (
    InvitationError,
    invite_colleague,
    revoke_invitation,
    send_invitation_email,
)
from stacos.tenancy.models import Membership, TenantInvitation

PAGE = "tenancy/team.html"
ACCESS_PAGE = "tenancy/member_access.html"


def _context(request: HttpRequest, form: InviteColleagueForm | None = None) -> dict[str, Any]:
    tenant = getattr(request, "tenant", None)
    scope = getattr(request, "access_scope", None)
    memberships = list(
        Membership.objects.filter(
            status__in=(Membership.Status.ACTIVE, Membership.Status.SUSPENDED)
        )
        # `select_related` and `prefetch_related`, with a query-count assertion
        # in the tests: a members list that issues a query per row — and the
        # reach column would issue two — is the ordinary way a small screen
        # becomes a slow one.
        .select_related("user", "role", "tenant")
        .prefetch_related("entities", "client_tenants")
        .order_by("role__rank", "user__full_name")
    )
    return {
        "tenant": tenant,
        "rows": [
            {"membership": membership, "reach": describe_reach(membership)}
            for membership in memberships
        ],
        "invitations": list(
            TenantInvitation.objects.filter(status=TenantInvitation.Status.SENT)
            .select_related("role", "invited_by")
            .order_by("-created_at")
        ),
        "form": form or InviteColleagueForm(tenant=tenant),
        "can_change_access": scope is not None and scope.has_permission(ACCESS_MANAGER_PERMISSION),
        "me": getattr(current_user(request), "pk", None),
    }


@require_permission("accounts.user.invite")
def team(request: HttpRequest) -> HttpResponse:
    template = derive_fragment_template(PAGE) if is_fragment_request(request) else PAGE
    return render(request, template, _context(request))


@require_permission("accounts.user.invite")
@require_http_methods(["POST"])
def invite(request: HttpRequest) -> HttpResponse:
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404

    form = InviteColleagueForm(request.POST, tenant=tenant)
    if not form.is_valid():
        return render(
            request,
            "tenancy/_fragments/team_body.html",
            _context(request, form),
            status=422,
        )

    try:
        role = form.cleaned_data["role"]
        invitation, raw = invite_colleague(
            tenant,
            email=form.cleaned_data["email"],
            role=role,
            inviter=current_user(request),
            grant=form.grant(categories=default_categories_for(role)),
        )
    except InvitationError as exc:
        form.add_error("email", str(exc))
        return render(
            request,
            "tenancy/_fragments/team_body.html",
            _context(request, form),
            status=422,
        )

    send_invitation_email(invitation, raw_token=raw, request=request)

    return oob(
        request,
        Fragment("tenancy/_fragments/team_body.html", _context(request)),
        toast=Toast(_("Invitation sent to %(email)s.") % {"email": invitation.email}),
    )


@require_permission("accounts.user.invite")
@require_http_methods(["POST"])
def invite_revoke(request: HttpRequest, pk: str) -> HttpResponse:
    """Withdraw an invitation. 404 on anything out of reach, as everywhere."""
    invitation = TenantInvitation.objects.filter(pk=pk, status=TenantInvitation.Status.SENT).first()
    if invitation is None:
        raise Http404

    revoke_invitation(invitation, actor=current_user(request))

    return oob(
        request,
        Fragment("tenancy/_fragments/team_body.html", _context(request)),
        toast=Toast(_("Invitation withdrawn.")),
    )


def _member_or_404(pk: str) -> Membership:
    """Through the scoped manager — a member of another workspace is a 404."""
    membership = (
        Membership.objects.filter(
            pk=pk, status__in=(Membership.Status.ACTIVE, Membership.Status.SUSPENDED)
        )
        .select_related("user", "role", "tenant")
        .first()
    )
    if membership is None:
        raise Http404
    return cast("Membership", membership)


def _member_context(
    request: HttpRequest,
    membership: Membership,
    *,
    form: MemberAccessForm | None = None,
    access_error: str = "",
    permissions_error: str = "",
) -> dict[str, Any]:
    scope = getattr(request, "access_scope", None)
    actor_permissions = scope.permissions if scope is not None else frozenset()
    return {
        "membership": membership,
        "form": form or MemberAccessForm(membership=membership),
        "reach": describe_reach(membership),
        "permission_groups": permission_choices(membership, actor_permissions),
        "customised": bool(membership.extra_permissions or membership.revoked_permissions),
        "access_error": access_error,
        "permissions_error": permissions_error,
        "is_self": membership.user_id == getattr(current_user(request), "pk", None),
    }


def _member_page(
    request: HttpRequest, context: dict[str, Any], *, status: int = 200, toast: str = ""
) -> HttpResponse:
    """The member page, whole or as its body — and after a save, the body with a toast."""
    if toast and is_fragment_request(request):
        return oob(
            request,
            Fragment(derive_fragment_template(ACCESS_PAGE), context),
            toast=Toast(toast),
        )
    template = (
        derive_fragment_template(ACCESS_PAGE) if is_fragment_request(request) else ACCESS_PAGE
    )
    return render(request, template, context, status=status)


def _name(membership: Membership) -> str:
    return membership.user.full_name or membership.user.email


@require_permission(ACCESS_MANAGER_PERMISSION)
@require_http_methods(["GET", "POST"])
def member_access(request: HttpRequest, pk: str) -> HttpResponse:
    """One team member: their role and reach, and their individual permissions.

    A page of its own rather than a modal on the people list: it is the screen an
    administrator is answerable for, and a deep link to "Priya's access" is worth
    having. Renders both ways, like every page. A POST here saves the role and
    reach; the permission checklist posts to :func:`member_permissions`.
    """
    membership = _member_or_404(pk)
    if request.method != "POST":
        return _member_page(request, _member_context(request, membership))

    form = MemberAccessForm(request.POST, membership=membership)
    if not form.is_valid():
        return _member_page(request, _member_context(request, membership, form=form), status=422)
    try:
        set_member_access(
            membership,
            role=form.cleaned_data["role"],
            grant=form.grant(categories=tuple(membership.categories)),
            actor=current_user(request),
        )
    except AccessError as exc:
        context = _member_context(request, membership, form=form, access_error=str(exc))
        return _member_page(request, context, status=422)

    if not is_fragment_request(request):
        return redirect("app:team_member_access", pk=membership.pk)
    membership = _member_or_404(pk)
    return _member_page(
        request,
        _member_context(request, membership),
        toast=_("Role and reach updated for %(name)s.") % {"name": _name(membership)},
    )


@require_permission(ACCESS_MANAGER_PERMISSION)
@require_http_methods(["POST"])
def member_permissions(request: HttpRequest, pk: str) -> HttpResponse:
    """Save the ticked permissions — the role's, plus or minus individual ones."""
    membership = _member_or_404(pk)
    scope = getattr(request, "access_scope", None)
    try:
        set_member_permissions(
            membership,
            granted=request.POST.getlist("permissions"),
            actor=current_user(request),
            actor_permissions=scope.permissions if scope is not None else frozenset(),
        )
    except AccessError as exc:
        context = _member_context(request, membership, permissions_error=str(exc))
        return _member_page(request, context, status=422)

    if not is_fragment_request(request):
        return redirect("app:team_member_access", pk=membership.pk)
    membership = _member_or_404(pk)
    return _member_page(
        request,
        _member_context(request, membership),
        toast=_("Permissions updated for %(name)s.") % {"name": _name(membership)},
    )
