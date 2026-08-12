"""
The combined email + phone verification flow.

The rule from the product brief, implemented literally: **one screen, one
verification call, one rate limit**. A user submits both codes together; the
attempt counts once; neither channel can be satisfied and then deferred.

The two channels are **email and WhatsApp**. WhatsApp rather than SMS because for
Indian businesses it is the channel people actually read — and because a WhatsApp
authentication template renders with a copy-code button, which removes the
transcription error that makes six-digit codes annoying on a phone.

Per-field errors *are* shown ("the code we sent on WhatsApp is incorrect"). The
alternative — an opaque "one of these is wrong" — buys almost nothing against an
attacker who is already rate-limited to five attempts, and costs a great deal for
a legitimate user retyping two codes.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256

import structlog
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from stacos.accounts.models import PendingVerification, User
from stacos.accounts.throttle import (
    ThrottleDecision,
    check_otp_send_allowed,
    record_message_spend,
    record_otp_send,
)
from stacos.accounts.whatsapp import WhatsAppMessage, get_whatsapp_provider
from stacos.core.audit import record_event
from stacos.core.models import AuditAction

logger = structlog.get_logger(__name__)

__all__ = [
    "VerificationOutcome",
    "generate_code",
    "hash_code",
    "resend_codes",
    "start_verification",
    "verify_codes",
]

_TEMPLATE_FOR_PURPOSE: dict[str, str] = {
    PendingVerification.Purpose.REGISTRATION: "otp_registration",
    PendingVerification.Purpose.LOGIN_NEW_DEVICE: "otp_login",
    PendingVerification.Purpose.STEP_UP: "otp_step_up",
    PendingVerification.Purpose.CHANNEL_CHANGE: "otp_login",
}


def generate_code() -> str:
    """A cryptographically random numeric code of the configured length."""
    length = settings.STACOS_OTP["CODE_LENGTH"]
    return "".join(secrets.choice("0123456789") for _ in range(length))


def hash_code(code: str, *, salt: str) -> str:
    """HMAC the code with a server-held pepper.

    Salted per verification so two concurrent attempts for the same identity do
    not produce comparable hashes, and peppered so a database disclosure alone
    does not permit offline recovery of live codes.
    """
    pepper = settings.STACOS_OTP["PEPPER"].encode()
    return hmac.new(pepper, f"{salt}:{code}".encode(), sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    ok: bool
    verification: PendingVerification | None = None
    email_error: str = ""
    phone_error: str = ""
    error: str = ""
    locked_out: bool = False


# ---------------------------------------------------------------------------
# Starting a verification
# ---------------------------------------------------------------------------


@transaction.atomic
def start_verification(
    *,
    purpose: str,
    email: str,
    phone_e164: str,
    user: User | None = None,
    ip_address: str | None = None,
    user_agent: str = "",
    device_fingerprint: str = "",
    next_url: str = "",
) -> tuple[PendingVerification | None, ThrottleDecision]:
    """Create a pending verification and dispatch both codes.

    Returns ``(None, decision)`` when throttled, so the caller can show the
    reason and the retry time rather than silently doing nothing.
    """
    email = email.lower().strip()

    decision = check_otp_send_allowed(email=email, phone_e164=phone_e164, ip_address=ip_address)
    if not decision:
        return None, decision

    # Abandon any earlier attempt for this identity, so a user who restarts the
    # flow is never confused about which code is live.
    PendingVerification.objects.filter(
        email=email,
        status=PendingVerification.Status.PENDING,
    ).update(status=PendingVerification.Status.ABANDONED)

    ttl = settings.STACOS_OTP["CODE_TTL_SECONDS"]
    verification = PendingVerification(
        purpose=purpose,
        user=user,
        email=email,
        phone_e164=phone_e164,
        ip_address=ip_address,
        user_agent=user_agent[:512],
        device_fingerprint=device_fingerprint,
        next_url=next_url[:500],
        expires_at=timezone.now() + timedelta(seconds=ttl),
    )
    verification.save()

    _dispatch_codes(verification)
    record_otp_send(email=email, phone_e164=phone_e164, ip_address=ip_address)

    record_event(
        action=AuditAction.OTP_SENT,
        actor=user,
        obj=verification,
        context={"purpose": purpose, "channels": ["email", "whatsapp"]},
    )
    return verification, decision


def resend_codes(verification: PendingVerification) -> ThrottleDecision:
    """Issue fresh codes for an existing verification, subject to backoff."""
    allowed_at = verification.next_resend_allowed_at()
    if allowed_at and timezone.now() < allowed_at:
        wait = int((allowed_at - timezone.now()).total_seconds())
        return ThrottleDecision(
            False,
            f"Please wait {wait} seconds before requesting new codes.",
            retry_after_seconds=wait,
        )

    decision = check_otp_send_allowed(
        email=verification.email,
        phone_e164=verification.phone_e164,
        ip_address=verification.ip_address,
    )
    if not decision:
        return decision

    ttl = settings.STACOS_OTP["CODE_TTL_SECONDS"]
    verification.expires_at = timezone.now() + timedelta(seconds=ttl)
    verification.resend_count += 1
    # Resending replaces both codes: leaving one live would mean two valid
    # phone codes at once, which quietly doubles the guess space.
    verification.email_satisfied = False
    verification.phone_satisfied = False
    verification.save(
        update_fields=["expires_at", "resend_count", "email_satisfied", "phone_satisfied"]
    )

    _dispatch_codes(verification)
    record_otp_send(
        email=verification.email,
        phone_e164=verification.phone_e164,
        ip_address=verification.ip_address,
    )
    return ThrottleDecision(True)


def _dispatch_codes(verification: PendingVerification) -> None:
    """Generate, store and send both codes."""
    salt = str(verification.id)
    minutes = settings.STACOS_OTP["CODE_TTL_SECONDS"] // 60

    email_code = generate_code()
    phone_code = generate_code()

    verification.email_code_hash = hash_code(email_code, salt=salt)
    verification.phone_code_hash = hash_code(phone_code, salt=salt)
    verification.last_sent_at = timezone.now()
    verification.save(update_fields=["email_code_hash", "phone_code_hash", "last_sent_at"])

    send_mail(
        subject=f"{email_code} is your STACOS verification code",
        message=(
            f"Your STACOS verification code is {email_code}.\n\n"
            f"It expires in {minutes} minutes. If you did not request it, ignore this email.\n\n"
            f"You will also need the code we sent on WhatsApp — both are required."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[verification.email],
        fail_silently=False,
    )

    provider = get_whatsapp_provider()
    result = provider.send(
        WhatsAppMessage(
            to_e164=verification.phone_e164,
            template_key=_TEMPLATE_FOR_PURPOSE.get(verification.purpose, "otp_login"),
            params={"code": phone_code, "minutes": str(minutes)},
        )
    )
    record_message_spend(
        provider=result.provider,
        cost_units=result.cost_units,
        failed=not result.accepted,
    )

    if result.not_on_whatsapp:
        # Distinct from a delivery failure: the number has no WhatsApp account,
        # so retrying will never work. Recorded so the view can say so plainly
        # rather than leaving the user waiting for a message that is not coming.
        logger.warning("otp.recipient_not_on_whatsapp", verification_id=str(verification.id))
    elif not result.accepted:
        logger.warning("otp.whatsapp_send_failed", error=result.error)


# ---------------------------------------------------------------------------
# Verifying
# ---------------------------------------------------------------------------


@transaction.atomic
def verify_codes(
    verification: PendingVerification,
    *,
    email_code: str,
    phone_code: str,
) -> VerificationOutcome:
    """Check both codes in a single attempt.

    Submitting the form once costs one attempt regardless of how many codes were
    wrong — so an attacker gains nothing by probing one channel at a time.
    """
    verification = PendingVerification.objects.select_for_update().get(pk=verification.pk)

    if verification.status != PendingVerification.Status.PENDING:
        return VerificationOutcome(False, error="This verification has already been used.")

    if verification.is_expired:
        verification.status = PendingVerification.Status.EXPIRED
        verification.save(update_fields=["status"])
        return VerificationOutcome(
            False, error="These codes have expired. Request new ones to continue."
        )

    if verification.attempts_remaining <= 0:
        verification.status = PendingVerification.Status.ABANDONED
        verification.save(update_fields=["status"])
        return VerificationOutcome(
            False,
            error="Too many incorrect attempts. Please start again.",
            locked_out=True,
        )

    verification.attempts += 1

    salt = str(verification.id)
    email_ok = hmac.compare_digest(
        verification.email_code_hash, hash_code(email_code.strip(), salt=salt)
    )
    phone_ok = hmac.compare_digest(
        verification.phone_code_hash, hash_code(phone_code.strip(), salt=salt)
    )

    if not (email_ok and phone_ok):
        verification.save(update_fields=["attempts"])
        record_event(
            action=AuditAction.OTP_FAILED,
            actor=verification.user,
            obj=verification,
            context={"email_ok": email_ok, "phone_ok": phone_ok, "attempts": verification.attempts},
        )
        remaining = verification.attempts_remaining
        if remaining <= 0:
            # Out of attempts: say so plainly rather than leaving the user
            # retyping codes that can no longer be accepted.
            verification.status = PendingVerification.Status.ABANDONED
            verification.save(update_fields=["status"])
            return VerificationOutcome(
                False,
                verification=verification,
                error="Too many incorrect attempts. Please start again.",
                locked_out=True,
            )

        # Naming which channel was wrong buys an attacker almost nothing against
        # a five-attempt limit, and saves a legitimate user retyping both codes.
        plural = "" if remaining == 1 else "s"
        return VerificationOutcome(
            False,
            verification=verification,
            email_error="" if email_ok else "This code does not match the one we emailed you.",
            phone_error="" if phone_ok else "This code does not match the one we sent on WhatsApp.",
            error=f"{remaining} attempt{plural} remaining.",
            locked_out=False,
        )

    verification.email_satisfied = True
    verification.phone_satisfied = True
    verification.status = PendingVerification.Status.VERIFIED
    verification.consumed_at = timezone.now()
    verification.save(
        update_fields=["attempts", "email_satisfied", "phone_satisfied", "status", "consumed_at"]
    )

    if verification.user_id:
        User.objects.filter(pk=verification.user_id).update(
            email_verified=True, phone_verified=True
        )

    record_event(
        action=AuditAction.OTP_VERIFIED,
        actor=verification.user,
        obj=verification,
        context={"purpose": verification.purpose},
    )
    return VerificationOutcome(True, verification=verification)
