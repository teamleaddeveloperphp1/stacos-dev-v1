"""
Identity.

A **User is global**: one email, one phone, one login, however many tenants they
belong to. A CA who is a partner at their firm, a director of their own company,
and a consultant to three clients is *one* row here — never duplicated per
organisation. Everything cross-tenant in STACOS depends on that.

Consequently nothing in this module is tenant-scoped, and ``core.checks`` lists
these models as deliberately global.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, ClassVar, cast

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.timezone import now as tz_now
from django.utils.translation import gettext_lazy as _

from stacos.core.ids import uuid7

__all__ = [
    "MessageSpendLedger",
    "PendingVerification",
    "TrustedDevice",
    "User",
    "UserSession",
]


class UserManager(BaseUserManager):
    """Email is the identifier; there is no separate username."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra: Any) -> User:
        if not email:
            raise ValueError("An email address is required.")
        email = self.normalize_email(email).lower()
        user = cast("User", self.model(email=email, **extra))
        if password:
            user.set_password(password)
        else:
            # Social-only accounts have no password to check against. An
            # unusable one is not the same as a blank one.
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        # A superuser created from the command line has proven control of the
        # console, not of an email address or phone. Marking both verified keeps
        # them out of the dual-OTP gate, which has no console flow.
        extra.setdefault("email_verified", True)
        extra.setdefault("phone_verified", True)
        if extra["is_staff"] is not True or extra["is_superuser"] is not True:
            raise ValueError("Superuser must have is_staff=True and is_superuser=True.")
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """A single global identity.

    ``is_staff`` / ``is_superuser`` here mean **STACOS platform staff**, not
    seniority inside a customer's organisation. A client's managing director has
    neither. Tenant-side authority comes from :class:`~stacos.tenancy.Membership`
    and the permission registry.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    email = models.EmailField(_("email address"), unique=True, db_index=True)
    email_verified = models.BooleanField(default=False)

    # E.164, so international numbers work without a schema change. Uniqueness
    # is enforced only among *verified* numbers — see Meta.constraints.
    #
    # This is the **WhatsApp channel**, not a general contact number: it is where
    # the second verification code goes, and `CLAUDE.md` rule 5 makes reaching it
    # a hard requirement for sign-up. `mobile_e164` below is the number a person
    # is called on, which is frequently a different one.
    phone_e164 = models.CharField(_("WhatsApp number"), max_length=20, blank=True, db_index=True)
    phone_verified = models.BooleanField(default=False)

    #: An ordinary contact number. Never used as an authentication channel, so it
    #: carries no verified flag and no uniqueness constraint — two people at one
    #: business sharing a landline is normal and must not be an error.
    mobile_e164 = models.CharField(_("mobile number"), max_length=20, blank=True)

    # Held separately because a list, a salutation and a sort order all need the
    # halves, and splitting a single string on whitespace gets Indian names wrong
    # often enough to be insulting. `full_name` remains the canonical display
    # string and is derived from the two in `save()` — it is what `__str__`,
    # `audit_label`, `initials` and every notification template already read, and
    # re-deriving it at each of those call sites would be churn for no gain.
    first_name = models.CharField(_("first name"), max_length=100, blank=True)
    last_name = models.CharField(_("last name"), max_length=100, blank=True)

    full_name = models.CharField(max_length=200, blank=True)
    display_name = models.CharField(max_length=80, blank=True)
    avatar_url = models.URLField(blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(
        default=False,
        help_text=_("STACOS platform staff. Unrelated to a customer's own hierarchy."),
    )

    # Rotating this invalidates every session and JWT for the user. Rotated on
    # password change, role change, and "sign out everywhere" — which is how
    # forced sign-out on a permission change actually takes effect.
    security_stamp = models.UUIDField(default=uuid7, editable=False)

    locale = models.CharField(max_length=12, default="en-in")
    # NB: this attribute shadows the `timezone` module inside the class body,
    # hence the explicit `tz_now` alias for the default below.
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")

    date_joined = models.DateTimeField(default=tz_now)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        constraints = [
            # Two people may share an unverified phone number — Indian
            # proprietorships and family businesses routinely do, and a hard
            # global unique would let anyone squat on a number they do not own
            # by registering and never verifying. Uniqueness applies once a
            # number is proven.
            models.UniqueConstraint(
                fields=["phone_e164"],
                condition=Q(phone_verified=True) & ~Q(phone_e164=""),
                name="user_verified_phone_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["phone_e164"], name="user_phone_idx"),
            models.Index(fields=["-last_seen_at"], name="user_last_seen_idx"),
        ]

    def __str__(self) -> str:
        return self.display_name or self.full_name or self.email

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.email = self.email.lower().strip()

        # `full_name` follows the halves when they are set, and is left alone
        # when they are not — a user created before this field existed, or by a
        # fixture that passes only `full_name`, keeps the name it has.
        derived = " ".join(part for part in (self.first_name, self.last_name) if part).strip()
        if derived:
            self.full_name = derived
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and (
                "first_name" in update_fields or "last_name" in update_fields
            ):
                kwargs["update_fields"] = [*update_fields, "full_name"]

        super().save(*args, **kwargs)

    # -- Identity helpers ---------------------------------------------------

    @property
    def audit_label(self) -> str:
        return f"{self} <{self.email}>"

    @property
    def short_name(self) -> str:
        return self.display_name or self.full_name.split(" ")[0] or self.email.split("@")[0]

    @property
    def initials(self) -> str:
        parts = [p for p in (self.full_name or self.email).replace("@", " ").split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    @property
    def is_fully_verified(self) -> bool:
        """Both channels proven. Required before any tenant data is reachable."""
        return self.email_verified and self.phone_verified

    # -- Session invalidation ----------------------------------------------

    def get_session_auth_hash(self) -> str:
        """Include the security stamp, so rotating it signs the user out everywhere.

        Django's default derives this from the password alone, which means a
        permission change cannot invalidate a live session. Adding the stamp is
        what makes "forced sign-out on role change" real rather than aspirational.
        """
        key_salt = "stacos.accounts.User.get_session_auth_hash"
        return salted_hmac(
            key_salt,
            f"{self.password}:{self.security_stamp}",
            algorithm="sha256",
        ).hexdigest()

    def rotate_security_stamp(self, *, save: bool = True) -> None:
        self.security_stamp = uuid7()
        if save:
            self.save(update_fields=["security_stamp"])


# ---------------------------------------------------------------------------
# Dual-channel verification
# ---------------------------------------------------------------------------


class PendingVerification(models.Model):
    """State for one combined email + phone verification attempt.

    STACOS requires **both** an email code and a WhatsApp code, satisfied
    *together* as a single step, to complete registration and to complete
    sign-in on an unrecognised device. That is one screen, one submission, one
    rate limit — which is why no off-the-shelf package covers it and this model
    exists.

    Social sign-in proves the provider identity and satisfies **neither** channel.
    """

    class Purpose(models.TextChoices):
        REGISTRATION = "REGISTRATION", _("Registration")
        LOGIN_NEW_DEVICE = "LOGIN_NEW_DEVICE", _("Sign-in from a new device")
        STEP_UP = "STEP_UP", _("Re-authentication for a sensitive action")
        CHANNEL_CHANGE = "CHANNEL_CHANGE", _("Changing email or phone")

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        VERIFIED = "VERIFIED", _("Verified")
        EXPIRED = "EXPIRED", _("Expired")
        ABANDONED = "ABANDONED", _("Abandoned")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)

    # Null during registration, when no account exists yet.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="verifications",
    )

    email = models.EmailField()
    phone_e164 = models.CharField(max_length=20)

    # HMAC-SHA256 with a server pepper. A slow hash would be theatre here: a
    # six-digit code has 10^6 possibilities, so what protects it is the attempt
    # limit and the ten-minute expiry, not the cost of one comparison.
    email_code_hash = models.CharField(max_length=64)
    phone_code_hash = models.CharField(max_length=64)

    email_satisfied = models.BooleanField(default=False)
    phone_satisfied = models.BooleanField(default=False)

    #: The number has no WhatsApp account, so the code was never delivered and a
    #: resend never will be. Distinct from a delivery failure, and stored rather
    #: than only logged because the *user* is the one who needs to know: without
    #: this they sit on the verification screen waiting for a message that is not
    #: coming, and then contact support.
    #:
    #: WhatsApp is currently a hard requirement for sign-up. There is deliberately
    #: no SMS fallback: it would need DLT template registration, which takes weeks,
    #: and shipping a half-working second channel is worse than one that plainly
    #: says what it needs. ``WhatsAppResult.not_on_whatsapp`` keeps that decision
    #: reversible — adding the fallback later changes no caller.
    phone_unreachable = models.BooleanField(default=False)

    # One counter for the pair. Submitting the form once is one attempt, even
    # though it carries two codes.
    attempts = models.PositiveSmallIntegerField(default=0)
    resend_count = models.PositiveSmallIntegerField(default=0)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    device_fingerprint = models.CharField(max_length=64, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    next_url = models.CharField(max_length=500, blank=True)

    #: Unused — sign-up no longer collects an organisation name; that happens in
    #: the onboarding wizard afterwards instead. Left in place rather than
    #: migrated away in the same change that stopped writing to it.
    organisation_name = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()

    class Meta:
        indexes = [
            models.Index(fields=["email", "-created_at"], name="pv_email_idx"),
            models.Index(fields=["phone_e164", "-created_at"], name="pv_phone_idx"),
            models.Index(fields=["status", "expires_at"], name="pv_status_expiry_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.purpose} for {self.email} ({self.status})"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_complete(self) -> bool:
        return self.email_satisfied and self.phone_satisfied

    @property
    def attempts_remaining(self) -> int:
        return max(0, settings.STACOS_OTP["MAX_ATTEMPTS"] - self.attempts)

    def next_resend_allowed_at(self) -> datetime | None:
        """Exponential backoff between resends, to keep SMS cost and abuse down."""
        if self.last_sent_at is None:
            return None
        backoff = settings.STACOS_OTP["RESEND_BACKOFF_SECONDS"]
        delay = backoff[min(self.resend_count, len(backoff) - 1)]
        return self.last_sent_at + timedelta(seconds=delay)


class TrustedDevice(models.Model):
    """A browser the user has already proven both channels from.

    Without this, mandatory dual OTP would mean two codes on every sign-in, every
    day — which in a market with genuinely unreliable SMS delivery would be a
    support queue rather than a security control. Trusted devices are what make
    the policy workable: the codes are demanded on first use of a device and then
    not again for thirty days, and the user can see and revoke each one.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trusted_devices"
    )

    # Only the hash is stored: a database disclosure must not yield working
    # device tokens.
    secret_hash = models.CharField(max_length=64, db_index=True)

    label = models.CharField(max_length=120, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    ip_first_seen = models.GenericIPAddressField(null=True, blank=True)
    ip_last_seen = models.GenericIPAddressField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=120, blank=True)

    objects = models.Manager()

    class Meta:
        indexes = [models.Index(fields=["user", "-last_used_at"], name="td_user_used_idx")]

    def __str__(self) -> str:
        return self.label or f"Device {str(self.id)[:8]}"

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None and timezone.now() < self.expires_at


class UserSession(models.Model):
    """A signed-in session, surfaced so the user can end one remotely."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sessions"
    )
    session_key = models.CharField(max_length=40, db_index=True)
    trusted_device = models.ForeignKey(
        TrustedDevice, on_delete=models.SET_NULL, null=True, blank=True, related_name="sessions"
    )

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    login_method = models.CharField(max_length=32, blank=True)  # password | google | apple | ...

    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_reason = models.CharField(max_length=64, blank=True)

    objects = models.Manager()

    class Meta:
        indexes = [models.Index(fields=["user", "-last_seen_at"], name="us_user_seen_idx")]

    def __str__(self) -> str:
        return f"{self.user} from {self.ip_address or 'unknown'}"


class MessageSpendLedger(models.Model):
    """Daily outbound message volume and cost, per channel and provider.

    Verification messaging is both a real expense and a real fraud vector: an
    attacker who can make the platform send unlimited WhatsApp authentication
    messages costs money directly. The daily cap reads from here and **fails
    closed** — refusing to send with a clear message rather than quietly
    degrading to a weaker single-channel flow.

    ``channel`` exists so this stays the right table if a fallback is ever added
    for numbers that turn out not to be on WhatsApp.
    """

    class Channel(models.TextChoices):
        WHATSAPP = "WHATSAPP", _("WhatsApp")
        SMS = "SMS", _("SMS")
        EMAIL = "EMAIL", _("Email")

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    day = models.DateField(db_index=True)
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.WHATSAPP)
    provider = models.CharField(max_length=32)
    messages_sent = models.PositiveIntegerField(default=0)
    cost_units = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    failures = models.PositiveIntegerField(default=0)
    #: Numbers with no WhatsApp account. Tracked separately from failures because
    #: it is a different problem needing a different remedy.
    unreachable = models.PositiveIntegerField(default=0)

    objects = models.Manager()

    class Meta:
        verbose_name = _("message spend entry")
        verbose_name_plural = _("message spend entries")
        constraints = [
            models.UniqueConstraint(
                fields=["day", "channel", "provider"],
                name="message_spend_day_channel_provider_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.day} {self.channel}/{self.provider}: {self.messages_sent} messages"
