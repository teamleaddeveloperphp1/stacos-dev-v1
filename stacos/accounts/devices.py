"""
Trusted devices.

Mandatory dual OTP on every sign-in would be unusable: two codes a day, in a
market where SMS delivery is genuinely unreliable, produces abandoned logins and
a support queue. Device trust is what makes the policy workable — both codes on
first use of a browser, then not again for thirty days, with every device listed
and individually revocable.

The cookie carries ``<device_id>.<secret>``; only a hash of the secret is stored,
so a database disclosure does not yield working device tokens.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import timedelta
from hashlib import sha256

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from stacos.accounts.models import TrustedDevice, User

__all__ = [
    "forget_device",
    "recognised_device",
    "remember_device",
    "revoke_all_devices",
]

COOKIE = getattr(settings, "TRUSTED_DEVICE_COOKIE", "stacos_td")


def _hash_secret(secret: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), secret.encode(), sha256).hexdigest()


def recognised_device(request: HttpRequest, user: User) -> TrustedDevice | None:
    """Return the trusted device backing this request, if any.

    Constant-time comparison, and the device must belong to *this* user — a
    stolen cookie from another account must not shortcut the OTP gate.
    """
    raw = request.COOKIES.get(COOKIE)
    if not raw or "." not in raw:
        return None

    device_id, _, secret = raw.partition(".")
    try:
        device = TrustedDevice.objects.get(pk=device_id, user=user)
    except (TrustedDevice.DoesNotExist, ValueError, TypeError):
        return None

    if not device.is_valid:
        return None
    if not hmac.compare_digest(device.secret_hash, _hash_secret(secret)):
        return None

    device.last_used_at = timezone.now()
    device.ip_last_seen = request.META.get("REMOTE_ADDR")
    device.save(update_fields=["last_used_at", "ip_last_seen"])
    return device


def remember_device(
    request: HttpRequest,
    response: HttpResponse,
    user: User,
    *,
    label: str = "",
) -> TrustedDevice:
    """Trust this browser for the configured window and set the cookie."""
    secret = secrets.token_urlsafe(32)
    days = getattr(settings, "TRUSTED_DEVICE_DAYS", 30)

    device = TrustedDevice.objects.create(
        user=user,
        secret_hash=_hash_secret(secret),
        label=label or _describe(request),
        user_agent=request.headers.get("User-Agent", "")[:512],
        ip_first_seen=request.META.get("REMOTE_ADDR"),
        ip_last_seen=request.META.get("REMOTE_ADDR"),
        expires_at=timezone.now() + timedelta(days=days),
    )

    response.set_cookie(
        COOKIE,
        f"{device.id}.{secret}",
        max_age=days * 86400,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
    )
    return device


def forget_device(response: HttpResponse, device: TrustedDevice, *, reason: str = "") -> None:
    device.revoked_at = timezone.now()
    device.revoked_reason = reason[:120]
    device.save(update_fields=["revoked_at", "revoked_reason"])
    response.delete_cookie(COOKIE)


def revoke_all_devices(user: User, *, reason: str = "user requested") -> int:
    """Revoke every trusted device and rotate the security stamp.

    Rotating the stamp is what makes this immediate: it changes the session auth
    hash, so existing sessions and issued JWTs stop validating rather than
    remaining live until they expire.
    """
    count = TrustedDevice.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=timezone.now(), revoked_reason=reason[:120]
    )
    user.rotate_security_stamp()
    return count


def _describe(request: HttpRequest) -> str:
    """A short, human-recognisable device label for the security settings list."""
    agent = request.headers.get("User-Agent", "")
    platform = next(
        (p for p in ("Windows", "Mac", "Linux", "Android", "iPhone", "iPad") if p in agent),
        "Unknown device",
    )
    browser = next(
        (b for b in ("Edg", "Chrome", "Firefox", "Safari") if b in agent),
        "browser",
    )
    return f"{platform} · {'Edge' if browser == 'Edg' else browser}"
