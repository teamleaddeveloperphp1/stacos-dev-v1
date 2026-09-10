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

from typing import Any

from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, derive_fragment_template, is_fragment_request, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.tenancy.forms import InviteColleagueForm
from stacos.tenancy.invitations import (
    InvitationError,
    invite_colleague,
    revoke_invitation,
    send_invitation_email,
)
from stacos.tenancy.models import Membership, TenantInvitation

PAGE = "tenancy/team.html"


def _context(request: HttpRequest, form: InviteColleagueForm | None = None) -> dict[str, Any]:
    tenant = getattr(request, "tenant", None)
    return {
        "tenant": tenant,
        # `select_related` on both, with a query-count assertion in the tests:
        # a members list that issues a query per row is the ordinary way a small
        # screen becomes a slow one.
        "memberships": list(
            Membership.objects.filter(
                status__in=(Membership.Status.ACTIVE, Membership.Status.SUSPENDED)
            )
            .select_related("user", "role")
            .order_by("role__rank", "user__full_name")
        ),
        "invitations": list(
            TenantInvitation.objects.filter(status=TenantInvitation.Status.SENT)
            .select_related("role", "invited_by")
            .order_by("-created_at")
        ),
        "form": form or InviteColleagueForm(tenant=tenant),
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
        invitation, raw = invite_colleague(
            tenant,
            email=form.cleaned_data["email"],
            role=form.cleaned_data["role"],
            inviter=current_user(request),
            message=form.cleaned_data.get("message", ""),
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
