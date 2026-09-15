"""
The gate between a signed-in session and a working organisation.

STACOS is one identity per user, decided at sign-up: the organisation exists
before the session does, because ``accounts.views._complete_verification``
provisions it the moment both OTP channels are proven. That closes the state
this gate used to spend most of its logic on — "signed in, belongs to
nothing" — but two situations still reach ``/app/`` without a usable
workspace, and each gets a different answer:

* **No membership at all.** Rare now — a user created outside the ordinary
  sign-up path, or one whose only invitation has not been accepted — but not
  impossible, and there is no self-service fix inside the product for it any
  more: organisations are not created here. A screen that says so, plainly.
* **A suspended membership and nothing active.** A screen that says so too,
  on every page under ``/app/``, offering only sign-out. Never a route to fix
  it by creating something new — a suspension is a decision somebody made on
  purpose.
A tenant with no entity yet — the ordinary state for a few seconds after
sign-up — is *not* handled here. Redirecting every page under ``/app/`` to
"add an entity" would mean a query on every request to ask a question only
the dashboard actually needs answered, so ``tenancy.views.dashboard`` sends
that redirect itself, reusing the entity count it already has to fetch for
its own tiles rather than spending a second query on it.

The two situations below are taken **once, here**, rather than in a view,
because the requirement is that they hold for pages nobody has written yet —
a view-level fix covers the page it is written on and silently misses the
next one.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from stacos.core.htmx import navigate, page_url

logger = structlog.get_logger(__name__)

__all__ = ["APP_PREFIX", "OrganisationGateMiddleware"]

#: Everything behind this prefix is tenant-scoped and needs a working
#: organisation. Marketing, ``/auth/`` (sign-out included), ``/accounts/``,
#: ``/api/``, ``/admin/``, ``/static/`` and ``/media/`` all sit outside it and
#: are therefore reachable without one, which is what keeps sign-out from
#: becoming a trap.
APP_PREFIX = "/app/"


class OrganisationGateMiddleware:
    """Route a signed-in user with no usable workspace to somewhere useful.

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

        if getattr(request, "suspended_memberships", None):
            return self._suspended(request)

        if getattr(request, "tenant", None) is None:
            return self._no_organisation(request)

        return self.get_response(request)

    @staticmethod
    def _applies(request: HttpRequest) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return request.path.startswith(APP_PREFIX)

    @staticmethod
    def _no_organisation(request: HttpRequest) -> HttpResponse:
        """A member-less user, with no in-product fix left to offer.

        403, not a redirect: there is nowhere inside ``/app/`` for this session
        to usefully land, and pretending otherwise is worse than saying so.
        Renders outside the application shell, like :meth:`_suspended` — the
        shell assumes a tenant, and there is not one here.
        """
        logger.info("tenancy.no_organisation", user_id=str(request.user.pk), path=request.path)
        response = render(request, "tenancy/no_organisation.html", status=403)
        if getattr(request, "htmx", False):
            return navigate(request, page_url(request))
        return response

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
