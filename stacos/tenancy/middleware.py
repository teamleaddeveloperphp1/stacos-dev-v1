"""
The gate between a signed-in session and an organisation.

A user can be perfectly authenticated, fully verified, and belong to nothing —
they have just signed up without naming an organisation, their invitation has
not been accepted, or their membership has been suspended. Every page under
``/app/`` needs a permission that only a member holds, so before this existed the
first thing the product said to a brand-new customer was that they were not
allowed in.

The decision is taken **once, here**, rather than in the dashboard view, because
the requirement is that it holds for pages nobody has written yet. A view-level
fix covers the page it is written on and silently misses the next one.

Two situations, deliberately answered differently:

* **No membership at all** → the setup flow. This is somebody who needs an
  organisation and has no route to one.
* **A suspended membership and nothing active** → a screen that says so, on
  every page under ``/app/`` *including the setup flow*. Sending them to setup
  would invite them to create a fresh organisation to escape a revocation
  somebody made on purpose, which quietly defeats the suspension. The scope
  resolver refuses them ``tenancy.onboarding.start`` for the same reason, so
  this screen is the readable face of a real boundary rather than the boundary
  itself.

An invitation that has not been accepted is *not* a suspension. Such a user has
no active membership either, and they go to setup like anybody else — accepting
the invitation is not the only thing they might reasonably want to do.

What this does *not* do is widen anything. The scope a member-less user resolves
to is unchanged — one permission, ``tenancy.onboarding.start``, and no tenant
bound — so every scoped query still returns nothing and every object URL still
404s. This middleware only decides which screen they are looking at while that
remains true.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse

from stacos.core.htmx import navigate, page_url

logger = structlog.get_logger(__name__)

__all__ = ["APP_PREFIX", "SETUP_PREFIX", "OrganisationGateMiddleware"]

#: Everything behind this prefix is tenant-scoped and needs an organisation.
#: Marketing, ``/auth/`` (sign-out included), ``/accounts/``, ``/api/``,
#: ``/admin/``, ``/static/`` and ``/media/`` all sit outside it and are therefore
#: reachable without one, which is what keeps sign-out from becoming a trap.
APP_PREFIX = "/app/"

#: The one carve-out inside ``/app/``: the flow being redirected *to*. Without
#: this the redirect is a loop. It does not apply to a suspended user — see
#: :meth:`OrganisationGateMiddleware.__call__`.
SETUP_PREFIX = "/app/start/"


class OrganisationGateMiddleware:
    """Route a signed-in user who belongs to nothing to somewhere useful.

    Listed after ``ScopeMiddleware``, so ``request.tenant`` and
    ``request.inactive_memberships`` are already resolved, and before
    ``AuthorizationExceptionMiddleware``, so it pre-empts the 403 rather than
    rewriting it afterwards. Also below ``HtmxMiddleware``, so ``request.htmx``
    exists and :func:`navigate` can issue ``HX-Redirect`` — a plain 302 here is
    followed by HTMX and swapped invisibly, which reads to the user as a click
    that did nothing.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not self._applies(request):
            return self.get_response(request)

        # Checked before the carve-out, not after. The exemption exists so that
        # somebody with no organisation can reach the flow that gives them one;
        # letting a suspended user through it would hand them exactly the escape
        # hatch the suspension was meant to close.
        if getattr(request, "suspended_memberships", None):
            return self._suspended(request)

        if request.path.startswith(SETUP_PREFIX):
            return self.get_response(request)

        logger.info(
            "tenancy.no_organisation",
            user_id=str(request.user.pk),
            path=request.path,
        )
        return navigate(request, reverse("onboarding:identity"))

    @staticmethod
    def _applies(request: HttpRequest) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        if not request.path.startswith(APP_PREFIX):
            return False
        # `tenant` is only set once a scope has been resolved. It is absent on
        # the paths ScopeMiddleware skips, and those are all outside /app/ — but
        # `getattr` rather than an attribute access, because a middleware that
        # raises AttributeError on an unexpected path is a 500 on every page.
        return getattr(request, "tenant", None) is None

    @staticmethod
    def _suspended(request: HttpRequest) -> HttpResponse:
        """The screen for somebody whose access was taken away on purpose.

        403 rather than a redirect: this is a refusal, and it is the one case
        where saying so plainly is the correct answer. It renders outside the
        application shell — the shell's tenant switcher assumes a tenant — and
        offers sign-out, so it is somewhere a person can leave from.
        """
        memberships = request.suspended_memberships  # type: ignore[attr-defined]
        logger.info(
            "tenancy.membership_suspended",
            user_id=str(request.user.pk),
            path=request.path,
            tenants=[str(m.tenant_id) for m in memberships],
        )
        response = render(
            request,
            "tenancy/suspended.html",
            {"memberships": memberships},
            status=403,
        )
        # HTMX would swap a 403 body into #main, leaving the refusal inside a
        # shell the user is no longer entitled to. Send the browser to the page
        # it is already on: the full navigation arrives back here and renders
        # this screen as a whole document, which is the only way it reads
        # correctly.
        if getattr(request, "htmx", False):
            return navigate(request, page_url(request))
        return response
