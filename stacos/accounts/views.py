"""
Authentication views.

The flow, stated plainly because the ordering is the security property:

* **Register** → create the user (unverified) → dual OTP → both channels proven
  → session marked verified → into the app.
* **Sign in** → password or social → is this a recognised device? → if yes, in;
  if no, dual OTP first.
* **Sensitive action** → step-up → password plus one code, or a fresh dual OTP.

Social sign-in enters at the same point as a password sign-in from a new device.
Proving a Google identity proves neither the email nor the phone STACOS holds.
"""

from __future__ import annotations

import structlog
from django.contrib import messages
from django.contrib.auth import login, logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.accounts.devices import recognised_device, remember_device, revoke_all_devices
from stacos.accounts.forms import DualOtpForm, LoginForm, RegistrationForm, StepUpForm
from stacos.accounts.middleware import SESSION_PENDING_KEY, SESSION_VERIFIED_KEY
from stacos.accounts.models import PendingVerification, TrustedDevice, User, UserSession
from stacos.accounts.otp import resend_codes, start_verification, verify_codes
from stacos.accounts.stepup import mark_step_up_complete
from stacos.core.htmx import is_fragment_request
from stacos.core.permissions import public_view, require_permission
from stacos.core.typing import current_user

logger = structlog.get_logger(__name__)

SAFE_REDIRECT_DEFAULT = "/app/"

#: Django's own backend. Named explicitly wherever `login()` is called with a
#: user that did not come from `authenticate()`, since more than one backend is
#: configured and Django refuses to guess.
DJANGO_AUTH_BACKEND = "django.contrib.auth.backends.ModelBackend"


def _navigate(request: HttpRequest, target: str) -> HttpResponse:
    """Send the browser to ``target``, whether or not HTMX is driving.

    An ordinary 302 is invisible to HTMX: it follows the redirect itself, gets
    the destination page back as a perfectly successful response, and swaps it
    into whatever region the caller targeted. That is how signing out came to
    leave the sign-in form rendered inside a still-signed-in shell, with the
    session already gone behind it. ``HX-Redirect`` is the instruction the
    browser actually acts on, so the navigation really happens.

    The same idiom as ``tenancy.views.switch_tenant`` and
    ``AuthorizationExceptionMiddleware._redirect``.
    """
    if getattr(request, "htmx", False):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = target
        return response
    return redirect(target)


def _safe_next(request: HttpRequest) -> str:
    """Only ever redirect within this site.

    An open redirect on a login flow is a phishing primitive: the attacker sends
    a genuine STACOS link that bounces the authenticated user to a lookalike.
    """
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return SAFE_REDIRECT_DEFAULT


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@public_view
@require_http_methods(["GET", "POST"])
def register(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect(SAFE_REDIRECT_DEFAULT)

    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = User.objects.create_user(
            email=form.cleaned_data["email"],
            password=form.cleaned_data["password"],
            full_name=form.cleaned_data["full_name"],
            phone_e164=form.cleaned_data["phone"],
        )
        verification, decision = start_verification(
            purpose=PendingVerification.Purpose.REGISTRATION,
            email=user.email,
            phone_e164=user.phone_e164,
            user=user,
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.headers.get("User-Agent", ""),
            next_url=_safe_next(request),
        )
        if verification is None:
            form.add_error(None, decision.reason)
        else:
            request.session[SESSION_PENDING_KEY] = str(verification.id)
            return redirect(reverse("accounts:verify"))

    return render(request, "accounts/register.html", {"form": form})


# ---------------------------------------------------------------------------
# Sign in
# ---------------------------------------------------------------------------


@public_view
@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect(_safe_next(request))

    form = LoginForm(request.POST or None, request=request)
    if request.method == "POST" and form.is_valid():
        user = form.user
        assert user is not None

        device = recognised_device(request, user)
        if device is not None and user.is_fully_verified:
            # Known browser, both channels already proven: straight in. This path
            # carries the large majority of sign-ins, which is what makes a
            # mandatory dual-OTP policy workable rather than exhausting.
            login(request, user)
            request.session[SESSION_VERIFIED_KEY] = True
            return redirect(_safe_next(request))

        verification, decision = start_verification(
            purpose=PendingVerification.Purpose.LOGIN_NEW_DEVICE,
            email=user.email,
            phone_e164=user.phone_e164,
            user=user,
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.headers.get("User-Agent", ""),
            next_url=_safe_next(request),
        )
        if verification is None:
            form.add_error(None, decision.reason)
        else:
            login(request, user)
            request.session[SESSION_VERIFIED_KEY] = False
            request.session[SESSION_PENDING_KEY] = str(verification.id)
            return redirect(reverse("accounts:verify"))

    return render(request, "accounts/login.html", {"form": form, "next": _safe_next(request)})


@public_view
@require_http_methods(["POST"])
def logout_view(request: HttpRequest) -> HttpResponse:
    """End the session and go to the sign-in screen, immediately.

    POST only: a sign-out reachable by GET is triggerable by any image tag on any
    page on the internet.
    """
    logout(request)
    return _navigate(request, reverse("accounts:login"))


# ---------------------------------------------------------------------------
# The dual-OTP screen
# ---------------------------------------------------------------------------


@public_view
@require_http_methods(["GET", "POST"])
def verify(request: HttpRequest) -> HttpResponse:
    """One screen, both codes, one submission, one rate limit."""
    verification = _load_pending(request)
    if verification is None:
        messages.info(request, _("Please sign in again to continue."))
        return redirect(reverse("accounts:login"))

    form = DualOtpForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        outcome = verify_codes(
            verification,
            email_code=form.cleaned_data["email_code"],
            phone_code=form.cleaned_data["phone_code"],
        )

        if outcome.ok:
            return _complete_verification(request, verification, form)

        if outcome.email_error:
            form.add_error("email_code", outcome.email_error)
        if outcome.phone_error:
            form.add_error("phone_code", outcome.phone_error)
        if outcome.error:
            form.add_error(None, outcome.error)
        if outcome.locked_out:
            request.session.pop(SESSION_PENDING_KEY, None)

    return render(
        request,
        "accounts/verify.html",
        {
            "form": form,
            "verification": verification,
            "masked_email": _mask_email(verification.email),
            "masked_phone": _mask_phone(verification.phone_e164),
        },
    )


def _complete_verification(
    request: HttpRequest,
    verification: PendingVerification,
    form: DualOtpForm,
) -> HttpResponse:
    """Both channels proven — establish the session and, optionally, trust the device."""
    user = verification.user
    if user is None:
        return redirect(reverse("accounts:login"))

    user.refresh_from_db()
    if not request.user.is_authenticated:
        # The backend must be named explicitly. Two are configured (Django's own
        # and allauth's), and this user came from the database rather than from
        # `authenticate()`, so it carries no `.backend` for Django to infer.
        login(request, user, backend=DJANGO_AUTH_BACKEND)

    request.session[SESSION_VERIFIED_KEY] = True
    request.session.pop(SESSION_PENDING_KEY, None)

    target = verification.next_url or SAFE_REDIRECT_DEFAULT
    response = redirect(target)

    if form.cleaned_data.get("remember_device"):
        remember_device(request, response, user)

    if verification.purpose == PendingVerification.Purpose.STEP_UP:
        mark_step_up_complete(request)

    return response


@public_view
@require_http_methods(["POST"])
def resend(request: HttpRequest) -> HttpResponse:
    verification = _load_pending(request)
    if verification is None:
        return redirect(reverse("accounts:login"))

    decision = resend_codes(verification)
    if decision.allowed:
        messages.success(request, _("New codes are on their way."))
    else:
        messages.warning(request, decision.reason)
    return redirect(reverse("accounts:verify"))


def _load_pending(request: HttpRequest) -> PendingVerification | None:
    pending_id = request.session.get(SESSION_PENDING_KEY)
    if not pending_id:
        return None
    return PendingVerification.objects.filter(
        pk=pending_id, status=PendingVerification.Status.PENDING
    ).first()


# ---------------------------------------------------------------------------
# Step-up
# ---------------------------------------------------------------------------


@public_view
@require_http_methods(["GET", "POST"])
def step_up(request: HttpRequest) -> HttpResponse:
    """Re-authenticate before a sensitive action."""
    if not request.user.is_authenticated:
        return redirect(reverse("accounts:login"))

    form = StepUpForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        mark_step_up_complete(request)
        return redirect(_safe_next(request))

    return render(
        request,
        "accounts/step_up.html",
        {"form": form, "next": _safe_next(request)},
    )


# ---------------------------------------------------------------------------
# Security settings
# ---------------------------------------------------------------------------


@require_permission("accounts.security.manage")
def security_settings(request: HttpRequest) -> HttpResponse:
    """Devices and sessions, with the controls to end them."""
    user = current_user(request)
    devices = TrustedDevice.objects.filter(user=user).order_by("-last_used_at")
    sessions = UserSession.objects.filter(user=user, ended_at__isnull=True).order_by(
        "-last_seen_at"
    )
    template = (
        "accounts/_fragments/security_body.html"
        if is_fragment_request(request)
        else "accounts/security.html"
    )
    return render(
        request,
        template,
        {
            "devices": devices,
            "sessions": sessions,
            "current_session_key": request.session.session_key,
        },
    )


@require_permission("accounts.security.manage")
@require_http_methods(["POST"])
def revoke_devices(request: HttpRequest) -> HttpResponse:
    """Sign out everywhere.

    Rotating the security stamp is what makes this immediate rather than
    eventual: it changes the session auth hash, so live sessions and issued JWTs
    stop validating on their next request.
    """
    count = revoke_all_devices(current_user(request), reason="user requested")
    messages.success(
        request,
        _("Signed out of %(count)d device(s). You will need to verify again here.")
        % {"count": count},
    )
    return _navigate(request, reverse("accounts:login"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask_email(email: str) -> str:
    """``priya@example.com`` -> ``p***a@example.com``."""
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"{local[:1]}***@{domain}"
    return f"{local[0]}{'*' * min(len(local) - 2, 5)}{local[-1]}@{domain}"


def _mask_phone(phone: str) -> str:
    """``+919876543210`` -> ``+91 ***** 43210``."""
    return f"{phone[:3]} ***** {phone[-5:]}" if len(phone) > 8 else phone
