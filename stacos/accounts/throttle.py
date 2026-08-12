"""
Rate limiting and cost control for verification codes.

A six-digit code has a million possibilities, so what actually protects it is the
attempt limit — not the hash. And because every SMS costs money, an unthrottled
send endpoint is a way to spend someone else's budget as well as a way to spam a
phone number.

Four independent limits, because each closes a different hole:

* **per identity, per hour and per day** — stops one target being hammered;
* **per IP, per hour** — stops one attacker enumerating many targets;
* **exponential resend backoff** — stops the "resend" button becoming a flood;
* **a global daily spend cap** — the backstop that fails *closed* if the other
  three are somehow evaded, so a bug cannot become an unbounded bill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import structlog
from django.conf import settings
from django.core.cache import cache
from django.db.models import F
from django.utils import timezone

logger = structlog.get_logger(__name__)

__all__ = [
    "ThrottleDecision",
    "check_otp_send_allowed",
    "record_otp_send",
    "record_sms_spend",
]


@dataclass(frozen=True, slots=True)
class ThrottleDecision:
    allowed: bool
    reason: str = ""
    retry_after_seconds: int = 0

    def __bool__(self) -> bool:
        return self.allowed


def _key(*parts: str) -> str:
    return "stacos:otp:" + ":".join(parts)


def check_otp_send_allowed(
    *,
    email: str,
    phone_e164: str,
    ip_address: str | None,
) -> ThrottleDecision:
    """Decide whether another verification code may be sent right now."""
    limits = settings.STACOS_OTP

    for identity in (email.lower(), phone_e164):
        if not identity:
            continue

        hourly = cache.get(_key("id", identity, "h"), 0)
        if hourly >= limits["MAX_PER_IDENTITY_PER_HOUR"]:
            return ThrottleDecision(
                False,
                "Too many verification codes requested for this account in the last hour.",
                retry_after_seconds=3600,
            )

        daily = cache.get(_key("id", identity, "d"), 0)
        if daily >= limits["MAX_PER_IDENTITY_PER_DAY"]:
            return ThrottleDecision(
                False,
                "The daily limit for verification codes has been reached. "
                "Please try again tomorrow or contact support.",
                retry_after_seconds=86400,
            )

    if ip_address:
        ip_hourly = cache.get(_key("ip", ip_address, "h"), 0)
        if ip_hourly >= limits["MAX_PER_IP_PER_HOUR"]:
            return ThrottleDecision(
                False,
                "Too many verification requests from this network.",
                retry_after_seconds=3600,
            )

    if not _within_daily_spend_cap():
        # Fails closed, loudly. A silent downgrade to "email only" would weaken
        # the security model without anyone noticing.
        logger.error("sms.daily_spend_cap_reached")
        return ThrottleDecision(
            False,
            "Verification is temporarily unavailable. Our team has been notified.",
            retry_after_seconds=3600,
        )

    return ThrottleDecision(True)


def record_otp_send(*, email: str, phone_e164: str, ip_address: str | None) -> None:
    """Count a send against every applicable limit."""
    for identity in (email.lower(), phone_e164):
        if identity:
            _increment(_key("id", identity, "h"), 3600)
            _increment(_key("id", identity, "d"), 86400)
    if ip_address:
        _increment(_key("ip", ip_address, "h"), 3600)


def _increment(key: str, ttl: int) -> int:
    """Increment a counter, creating it with a TTL if absent.

    ``cache.add`` then ``cache.incr`` is deliberate: ``add`` is atomic and only
    sets the TTL on first write, so a burst cannot keep extending the window.
    """
    cache.add(key, 0, ttl)
    try:
        return cache.incr(key)
    except ValueError:  # key expired between add and incr
        cache.set(key, 1, ttl)
        return 1


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------


def _within_daily_spend_cap() -> bool:
    cap = getattr(settings, "SMS_DAILY_SPEND_CAP_UNITS", 0)
    if not cap:
        return True
    spent = cache.get(_key("spend", str(timezone.localdate())), 0)
    return spent < cap


def record_sms_spend(*, provider: str, cost_units: Decimal, failed: bool = False) -> None:
    """Record one message against today's spend, in cache and in the ledger.

    The cache counter is the fast path the throttle reads; the ledger row is the
    durable record finance reconciles against the provider's invoice.
    """
    from stacos.accounts.models import SmsSpendLedger

    today: date = timezone.localdate()
    _increment_by(_key("spend", str(today)), int(cost_units) or 1, ttl=90000)

    ledger, _ = SmsSpendLedger.objects.get_or_create(day=today, provider=provider)
    SmsSpendLedger.objects.filter(pk=ledger.pk).update(
        messages_sent=F("messages_sent") + 1,
        cost_units=F("cost_units") + cost_units,
        failures=F("failures") + (1 if failed else 0),
    )


def _increment_by(key: str, amount: int, *, ttl: int) -> None:
    cache.add(key, 0, ttl)
    try:
        cache.incr(key, amount)
    except ValueError:
        cache.set(key, amount, ttl)


def seconds_until(moment: datetime | None) -> int:
    if moment is None:
        return 0
    delta = moment - timezone.now()
    return max(0, int(delta / timedelta(seconds=1)))
