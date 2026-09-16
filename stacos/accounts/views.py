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

from urllib.parse import quote

import structlog
from django.contrib import messages
from django.contrib.auth import login, logout
from django.db import IntegrityError, transaction
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
from stacos.core.htmx import navigate as _navigate
from stacos.core.permissions import public_view, require_permission
from stacos.core.rls import rls_bootstrap
from stacos.core.typing import current_user
from stacos.tenancy.models import Membership
from stacos.tenancy.scope_resolver import SESSION_TENANT_KEY
from stacos.tenancy.services import default_workspace_name, provision_tenant

logger = structlog.get_logger(__name__)

SAFE_REDIRECT_DEFAULT = "/app/"

#: Django's own backend. Named explicitly wherever `login()` is called with a
#: user that did not come from `authenticate()`, since more than one backend is
#: configured and Django refuses to guess.
DJANGO_AUTH_BACKEND = "django.contrib.auth.backends.ModelBackend"


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

    # An invitation link parks the invited address here, so somebody arriving
    # from one does not have to retype it — and, more usefully, cannot mistype it
    # into an account the invitation will not match.
    initial = {}
    invited_email = request.GET.get("email", "").strip()
    if invited_email:
        initial["email"] = invited_email

    form = RegistrationForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            user = _create_or_reclaim_user(form)
        except IntegrityError:
            # Two sign-ups for one address, in flight at the same time. The
            # form's own check cannot close this — it reads a moment before the
            # write — so the database constraint is the real guard and this is
            # how it reaches the user: as the same sentence they would have got
            # a second earlier, not as a 500.
            logger.warning("accounts.register_conflict", email=form.cleaned_data["email"])
            form.add_error("email", _("An account already exists for this email. Sign in instead."))
        else:
            verification, reason = _start_verification_safely(
                request, user, purpose=PendingVerification.Purpose.REGISTRATION
            )
            if verification is None:
                form.add_error(None, reason)
            else:
                request.session[SESSION_PENDING_KEY] = str(verification.id)
                return redirect(reverse("accounts:verify"))

    return render(request, "accounts/register.html", {"form": form, "next": _safe_next(request)})


@transaction.atomic
def _create_or_reclaim_user(form: RegistrationForm) -> User:
    """The account this sign-up is for — a new row, or an abandoned one reused.

    Reuse is the branch worth explaining. ``RegistrationForm.clean_email``
    allows an address that already has an account **only** when that account was
    never verified, which is the wreckage left by every sign-up that got as far
    as creating a user and no further: codes throttled, WhatsApp number
    mistyped, browser closed at the verification screen. Refusing it told those
    users "an account already exists — sign in instead", and signing in put them
    straight back at the same verification screen. There was no way out inside
    the product.

    It grants nothing. The account is unusable until somebody reads a code sent
    to that address, so whoever retypes the form here still has to control the
    inbox before any of it means anything — and rotating the security stamp
    invalidates whatever the abandoned attempt left lying around.

    The row is re-read ``FOR UPDATE`` and re-checked rather than trusted from
    the form: ``clean_email`` ran before this transaction opened, and a
    verification landing in between would otherwise let a live account be
    overwritten.
    """
    data = form.cleaned_data
    existing = form.reclaimable_user

    if existing is not None:
        locked = User.objects.select_for_update().filter(pk=existing.pk).first()
        if locked is not None and not locked.is_fully_verified:
            locked.first_name = data["first_name"]
            locked.last_name = data["last_name"]
            locked.phone_e164 = data["phone"]
            locked.set_password(data["password"])
            locked.rotate_security_stamp(save=False)
            locked.is_active = True
            locked.save()
            logger.info("accounts.register_reclaimed_unverified", user_id=str(locked.pk))
            return locked
        # Verified in the meantime: fall through to the create below, which the
        # unique constraint will refuse — handled by the caller as a conflict.

    return User.objects.create_user(
        email=data["email"],
        password=data["password"],
        first_name=data["first_name"],
        last_name=data["last_name"],
        phone_e164=data["phone"],
    )


def _start_verification_safely(
    request: HttpRequest, user: User, *, purpose: str
) -> tuple[PendingVerification | None, str]:
    """Send both codes, converting every failure into something a person can act on.

    ``start_verification`` reaches an SMTP server and a WhatsApp provider. Both
    are out of this process's control and both fail in production — a timeout, a
    bounced connection, a provider returning something unexpected — and an
    unhandled one here is a 500 rendered over a form the user has already
    filled in correctly, at both call sites that reach this: a fresh sign-up
    and a sign-in from a new device. The detail goes to the log, where it is
    actionable; the user gets a sentence and a form they can resubmit. For
    sign-up, the account also survives to be reclaimed by that resubmission —
    see ``_create_or_reclaim_user``. For sign-in, the password has already been
    checked, so nothing here should ever ask the user to retype it.
    """
    try:
        verification, decision = start_verification(
            purpose=purpose,
            email=user.email,
            phone_e164=user.phone_e164,
            user=user,
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.headers.get("User-Agent", ""),
            next_url=_safe_next(request),
        )
    except Exception:
        logger.exception("accounts.verification_dispatch_failed", user_id=str(user.pk))
        return None, _(
            "We could not send your verification codes just now. "
            "Please try again in a moment — your details have been kept."
        )
    if verification is None:
        return None, decision.reason
    return verification, ""


#: Where an invitation link parks itself while the invitee signs up or signs in.
SESSION_INVITATION_KEY = "stacos_pending_invitation"


@public_view
def accept_invitation(request: HttpRequest, token: str) -> HttpResponse:
    """Join the organisation somebody invited you to.

    Three states arrive here and the flow has to handle all of them, because the
    common case is the one with no account:

    * **Signed in and verified** — join, and land inside the organisation.
    * **Signed in but not verified** — the verification gate has already sent
      them elsewhere; they come back here afterwards.
    * **Not signed in** — the token is parked on the session and they are sent to
      sign up, with the invited address prefilled. After the dual OTP they are
      returned to this URL and the first branch runs.

    Nothing here weakens verification. The membership is created by
    ``invitations.accept_invitation`` only once ``request.user`` is a real,
    fully verified user — an invitation is permission to join, not proof of
    identity, and treating a clicked link as both is how an invited address gets
    claimed by whoever the link was forwarded to.

    Lives under ``/auth/`` rather than ``/app/``, so the organisation gate does
    not redirect the very people this exists for — an invitee has no membership
    yet, by definition.
    """
    from stacos.tenancy.invitations import accept_invitation as bind_membership
    from stacos.tenancy.invitations import resolve_invitation
    from stacos.tenancy.scope_resolver import SESSION_TENANT_KEY

    invitation = resolve_invitation(token)
    if invitation is None:
        # Expired, withdrawn and forged are one answer. Distinguishing them tells
        # a guesser which invitations used to exist.
        return render(request, "accounts/invitation_invalid.html", status=404)

    if not request.user.is_authenticated:
        request.session[SESSION_INVITATION_KEY] = token
        target = reverse("accounts:register")
        return redirect(f"{target}?next={quote(request.path)}&email={quote(invitation.email)}")

    if not request.session.get(SESSION_VERIFIED_KEY):
        # The gate will bounce them to the OTP screen and back here. Reaching
        # this branch at all means they came in on a session that has not been
        # verified on this device.
        return redirect(f"{reverse('accounts:verify')}?next={quote(request.path)}")

    membership = bind_membership(invitation, user=request.user)

    request.session.pop(SESSION_INVITATION_KEY, None)
    request.session[SESSION_TENANT_KEY] = str(membership.tenant_id)
    request.session.modified = True

    messages.success(
        request,
        _("You have joined %(name)s.") % {"name": invitation.tenant.name},
    )
    return redirect(SAFE_REDIRECT_DEFAULT)


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

        verification, reason = _start_verification_safely(
            request, user, purpose=PendingVerification.Purpose.LOGIN_NEW_DEVICE
        )
        if verification is None:
            form.add_error(None, reason)
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

    # Not for somebody signing up on their way to accept a colleague's
    # invitation — `SESSION_INVITATION_KEY` is still parked at this point
    # (`accept_invitation` only pops it once the membership is actually
    # bound, on the request this one redirects to), and that invitation is
    # what puts them in an organisation, not a fresh one of their own.
    if verification.purpose == PendingVerification.Purpose.REGISTRATION and not request.session.get(
        SESSION_INVITATION_KEY
    ):
        _provision_signup_tenant(request, user, verification)

    target = verification.next_url or SAFE_REDIRECT_DEFAULT
    response = redirect(target)

    if form.cleaned_data.get("remember_device"):
        remember_device(request, response, user)

    if verification.purpose == PendingVerification.Purpose.STEP_UP:
        mark_step_up_complete(request)

    return response


def _provision_signup_tenant(
    request: HttpRequest, user: User, verification: PendingVerification
) -> None:
    """The workspace, created the moment both channels are proven.

    STACOS is one identity per user, decided at sign-up — there is no later
    "set up an organisation" step to fall back on, so this is the only place a
    fresh account's tenant is created. Guarded on an existing membership rather
    than on verification state, because that is the one check that stays correct
    even if this handler is ever reached twice for the same verified user.

    **The name.** Sign-up no longer asks for one: it collects a person, and the
    workspace is named after that person until somebody renames it
    (``tenancy.services.default_workspace_name``, and the prompt the dashboard's
    first-run card carries). ``verification.organisation_name`` is still read
    first, and is not dead code — a verification created by the previous
    sign-up form may still be sitting in a live session when this deploys, and
    throwing away a name the user typed two minutes ago would be a poor
    introduction.

    ``rls_bootstrap`` is load-bearing, not decorative: a member-less user has no
    tenant bound, so Row-Level Security restricts ``Membership`` to nothing at
    all here — ``objects_unscoped`` lifts only the ORM's own filter. Without the
    bootstrap the guard above always reads as "no membership yet" and a retried
    request provisions a second tenant. Same bootstrap
    ``scope_resolver._select_membership`` uses, for the same reason.

    Wrapped in its own transaction rather than relying on one already being
    open: unlike a request under ``/app/``, ``ScopeMiddleware`` opens none here
    — this request was still anonymous when it ran, ``login()`` having happened
    a moment ago inside this same view — and the bootstrap's setting is
    transaction-local.
    """
    with transaction.atomic():
        with rls_bootstrap():
            already_provisioned = Membership.objects_unscoped.filter(user=user).exists()
        if already_provisioned:
            return
        chosen_name = (verification.organisation_name or "").strip()
        tenant = provision_tenant(
            chosen_name or default_workspace_name(user),
            owner=user,
            reason="signup",
            name_provisional=not chosen_name,
        )
    request.session[SESSION_TENANT_KEY] = str(tenant.id)


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
        # `_navigate`, not `redirect`: an HTMX caller follows a 302 itself and
        # swaps the destination into whatever region it targeted, so the user
        # ends up with the page they asked for rendered inside a fragment slot —
        # or, when the destination is itself a fragment, with nothing at all.
        return _navigate(request, _safe_next(request))

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
