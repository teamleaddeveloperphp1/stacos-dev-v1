"""
The two gates that stand between a signed-in session and tenant data.

:class:`SecurityStampMiddleware` — Django validates a session against a hash of
the password, so a *permission* change cannot invalidate a live session. STACOS
adds a rotating ``security_stamp`` to that hash, and this middleware is what
turns a rotation into an immediate sign-out everywhere.

:class:`VerificationGateMiddleware` — an authenticated session is not
sufficient. Until both the email OTP and the phone OTP have been satisfied
together, the session is redirected to the verification screen and cannot reach
``/app``. Social sign-in lands here too: proving a Google identity proves neither
channel.
"""

from __future__ import annotations

from collections.abc import Callable

import structlog
from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

logger = structlog.get_logger(__name__)

__all__ = ["SecurityStampMiddleware", "VerificationGateMiddleware"]

SESSION_STAMP_KEY = "stacos_security_stamp"
SESSION_VERIFIED_KEY = "stacos_fully_verified"
SESSION_PENDING_KEY = "stacos_pending_verification_id"

#: Paths reachable while a session is authenticated but not yet verified.
#: Deliberately tiny — the verification screen, sign-out, and static assets.
VERIFICATION_EXEMPT_PREFIXES = (
    "/auth/",
    "/accounts/",
    "/static/",
    "/media/",
    "/healthz",
    "/__debug__/",
    "/api/",  # the API has its own gate; see stacos.api.authentication
)


class SecurityStampMiddleware:
    """Sign the user out when their security stamp has been rotated.

    Rotation happens on password change, role change, and "sign out everywhere".
    Without this, a revoked role would remain effective until the session expired
    — which for a compliance product is the difference between revocation and a
    suggestion.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        user = getattr(request, "user", None)

        if user is not None and user.is_authenticated:
            stored = request.session.get(SESSION_STAMP_KEY)
            current = str(user.security_stamp)

            if stored is None:
                request.session[SESSION_STAMP_KEY] = current
            elif stored != current:
                logger.info("auth.forced_signout", user_id=str(user.pk), reason="security_stamp")
                logout(request)
                response = redirect(reverse("accounts:login"))
                response["X-Stacos-Signout-Reason"] = "credentials-changed"
                return response

        return self.get_response(request)


class VerificationGateMiddleware:
    """Hold an authenticated-but-unverified session at the verification screen.

    The check is on the *session*, not only on the user: a user whose email and
    phone are already verified still passes through here when signing in from an
    unrecognised device, because device recognition is resolved during login and
    recorded on the session.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # A user who has proven both channels and arrives on a recognised device
        # is marked verified during login. Reaching here without that flag means
        # one of the two is missing, whatever the User row says.
        if self._should_check(request) and not request.session.get(SESSION_VERIFIED_KEY):
            return self._redirect_to_verification(request)

        return self.get_response(request)

    @staticmethod
    def _should_check(request: HttpRequest) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        if user.is_superuser and request.path.startswith("/admin/"):
            # Django admin has its own login and no place to render the dual-OTP
            # screen; platform staff reach it only from an allowlisted network.
            return False
        return not request.path.startswith(VERIFICATION_EXEMPT_PREFIXES)

    @staticmethod
    def _redirect_to_verification(request: HttpRequest) -> HttpResponse:
        target = reverse("accounts:verify")
        next_url = request.get_full_path()

        # An HTMX request cannot follow a 302 into a full page — the fragment
        # would be swapped into #main and the user would see a login form inside
        # their dashboard. HX-Redirect makes the browser navigate properly.
        if getattr(request, "htmx", False):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = f"{target}?next={next_url}"
            return response

        return redirect(f"{target}?next={next_url}")
