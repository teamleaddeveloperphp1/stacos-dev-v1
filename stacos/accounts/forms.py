"""Authentication forms. Server-side validation is the only validation that counts."""

from __future__ import annotations

import re
import socket
import unicodedata
from typing import Any, cast

import phonenumbers
import structlog
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Field, Layout, Row, Submit
from django import forms
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import (
    password_validators_help_texts,
    validate_password,
)
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext_lazy as _

from stacos.accounts.models import User

logger = structlog.get_logger(__name__)

__all__ = [
    "DualOtpForm",
    "LoginForm",
    "RegistrationForm",
    "StepUpForm",
    "assert_real_email_domain",
    "normalise_phone",
]

# A password field with a show/hide toggle, in place of crispy's default
# bootstrap5/field.html. Referenced by path (not the field class) because
# crispy resolves layout templates through the standard template loader.
_MASKED_FIELD_TEMPLATE = "accounts/_password_field.html"

#: The same control, plus a live strength meter and the requirement checklist.
#: Only for a password being *chosen* — on a sign-in or a step-up the rules are
#: not the user's problem and rating a password they already have is noise.
_STRENGTH_FIELD_TEMPLATE = "accounts/_new_password_field.html"

#: The confirmation, with a live "these match" readout rather than a mismatch
#: discovered on submit.
_CONFIRM_FIELD_TEMPLATE = "accounts/_confirm_password_field.html"

#: RFC 5321's limit on an address. Django's EmailField defaults to 320, which
#: is the limit on a *path*, not on an address, and lets a value through that
#: no mail server will accept.
MAX_EMAIL_LENGTH = 254

#: A ceiling on what is hashed. PBKDF2 cost is linear in input length, so an
#: unbounded password field is a cheap way to make a server do expensive work;
#: 128 characters is far beyond any passphrase a person actually types.
MAX_PASSWORD_LENGTH = 128

#: What a person's name may be. Long enough for a full South Indian name written
#: out, short enough that the column cannot be used as free storage.
MAX_NAME_LENGTH = 100


def configured_min_password_length(default: int = 8) -> int:
    """The floor ``MinimumLengthValidator`` is actually configured with.

    Read rather than restated, so the number the sign-up screen advertises and
    the number the server enforces cannot drift. They did: the screen said ten
    because the form field said ten, and the validator list that would have
    rejected a nine-character password was never run at all.
    """
    for validator in settings.AUTH_PASSWORD_VALIDATORS:
        entry = cast("dict[str, Any]", validator)
        if str(entry.get("NAME", "")).endswith("MinimumLengthValidator"):
            return int(entry.get("OPTIONS", {}).get("min_length", default))
    return default


def normalise_phone(raw: str, *, default_region: str = "IN") -> str:
    """Parse a phone number into E.164.

    Stored in E.164 so international numbers need no schema change, and so the
    same number typed as ``98765 43210``, ``+91 98765 43210`` and
    ``09876543210`` resolves to one identity.
    """
    raw = (raw or "").strip()
    if not raw:
        raise forms.ValidationError(_("A mobile number is required."))
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        raise forms.ValidationError(_("That does not look like a valid phone number.")) from None
    if not phonenumbers.is_valid_number(parsed):
        raise forms.ValidationError(_("That phone number is not valid."))
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


#: Line types that can hold a WhatsApp account. A landline cannot, and telling
#: somebody that *before* we spend a message on it is the difference between a
#: corrected typo and a support ticket — ``PendingVerification.phone_unreachable``
#: is the same news delivered a minute later and a message more expensively.
_WHATSAPP_CAPABLE_TYPES = frozenset(
    {
        phonenumbers.PhoneNumberType.MOBILE,
        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
    }
)


def assert_whatsapp_capable(e164: str) -> None:
    """Raise unless ``e164`` is a line type that can carry WhatsApp.

    Deliberately permissive about the unknown: ``FIXED_LINE_OR_MOBILE`` is what
    the metadata reports for whole countries, and refusing those would lock out
    real users to catch a typo.
    """
    parsed = phonenumbers.parse(e164, None)
    if phonenumbers.number_type(parsed) not in _WHATSAPP_CAPABLE_TYPES:
        raise forms.ValidationError(
            _(
                "That looks like a landline. The second verification code goes by "
                "WhatsApp, so this has to be a mobile number."
            )
        )


#: The only "example.*" domains RFC 2606 actually reserves — permanently, by
#: name, for documentation and testing. This project's own test suite leans on
#: ``example.com`` throughout, exactly as the RFC intends, so these three stay
#: accepted unconditionally rather than behind the settings flag below: the
#: point of the check that follows is to catch the domains that are *not*
#: reserved for this, which is what a typo or a copy-pasted placeholder
#: actually produces.
_RFC2606_EXAMPLE_DOMAINS = frozenset({"example.com", "example.net", "example.org"})

#: "example.<anything else>" — the same placeholder instinct RFC 2606 was
#: written to name, aimed at a TLD it does not cover. Checked unconditionally,
#: not gated by ``EMAIL_DOMAIN_VERIFICATION_ENABLED``: the domain is fake by
#: construction, not merely unreachable today, so there is no network call to
#: skip and no reason to make this deterministic check depend on one.
_PLACEHOLDER_DOMAIN = re.compile(r"^example\.[a-z]{2,24}$")


def assert_real_email_domain(email: str) -> None:
    """Raise unless ``email``'s domain looks like somewhere mail can arrive.

    Django's own ``EmailField`` only checks *shape* — one ``@``, a label,
    a dot, a final label of letters — which is exactly why ``xyz@example.abc``
    sailed through it: every one of those is true of a domain that has never
    existed. Two checks, cheapest first:

    1. **Known placeholders** — see :data:`_PLACEHOLDER_DOMAIN`. Instant, and
       always applied: the domain is fake by construction.
    2. **DNS resolution**, gated by ``settings.EMAIL_DOMAIN_VERIFICATION_ENABLED``
       (off in tests — see ``config/settings/test.py`` — so the suite stays
       fast and offline). A domain with no ``A``/``AAAA`` record cannot
       receive the verification email this same request is about to send, so
       finding that out now saves a wasted send against the rate limit and a
       user staring at a code that will never arrive.

    Fails **open** on anything that is not a definite "this name does not
    exist" — a resolver timeout or an unreachable network must not turn into a
    signup nobody can complete, and this product already has one thing that
    can legitimately be unreachable (``PendingVerification.phone_unreachable``);
    a second, silent one would be worse than none.
    """
    domain = email.rpartition("@")[2].lower()

    if domain not in _RFC2606_EXAMPLE_DOMAINS and _PLACEHOLDER_DOMAIN.match(domain):
        raise forms.ValidationError(
            _("That looks like a placeholder address. Enter the email you actually use.")
        )

    if not getattr(settings, "EMAIL_DOMAIN_VERIFICATION_ENABLED", True):
        return

    if not _domain_resolves(domain):
        raise forms.ValidationError(
            _(
                "We could not find a mail server for that domain. "
                "Check it for a typo — the verification code has to reach it."
            )
        )


#: How long to wait for an answer before treating the resolver itself as the
#: problem rather than the domain. This runs inside a form's ``clean()`` on the
#: request thread, so it has to fail fast — a real mail-capable domain resolves
#: in milliseconds against any resolver worth using.
_DOMAIN_LOOKUP_TIMEOUT_SECONDS = 3.0


def _domain_resolves(domain: str) -> bool:
    """Best-effort DNS check: does anything answer for this domain at all.

    A plain ``getaddrinfo`` rather than a proper MX lookup, deliberately — MX
    needs a resolver library this project does not otherwise depend on, and an
    ``A``/``AAAA`` record is what the overwhelming majority of real domains
    publish at their apex regardless of where their mail actually routes. The
    distinction this function exists to draw is "exists" versus "typo'd into
    nonexistence", not a precise deliverability guarantee — SMTP delivery is
    what will actually prove that, the moment the code is sent.

    ``getaddrinfo`` raises ``gaierror`` for two DNS outcomes that must not be
    treated alike: ``EAI_NONAME`` ("no such name") is a real typo, but
    ``EAI_NODATA`` means the name exists and answered, it just has no
    ``A``/``AAAA`` record — the normal shape of a domain that only publishes
    ``MX`` records (e.g. Google Workspace mail with no website behind the
    apex). Only ``EAI_NONAME`` is treated as "this domain does not exist";
    everything else fails open, same as the generic ``OSError`` branch below.

    The timeout is applied via ``socket.setdefaulttimeout`` and restored in a
    ``finally``, rather than threaded through as an argument: ``getaddrinfo``
    has no timeout parameter of its own, and this is the standard-library idiom
    for bounding it. Not thread-safe against another thread changing the same
    process-global default mid-call, which is an acceptable trade against
    adding a dependency for one bounded DNS lookup.
    """
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_DOMAIN_LOOKUP_TIMEOUT_SECONDS)
    try:
        socket.getaddrinfo(domain, None)
    except socket.gaierror as exc:
        if exc.errno == socket.EAI_NONAME:
            return False
        logger.warning("accounts.email_domain_lookup_failed", domain=domain, errno=exc.errno)
        return True
    except OSError:
        logger.warning("accounts.email_domain_lookup_failed", domain=domain)
        return True
    finally:
        socket.setdefaulttimeout(previous)
    return True


class PersonNameField(forms.CharField):
    """One half of a person's name.

    ``CharField`` already strips, which catches a field holding nothing but
    spaces. What it does not catch is ``"..."`` or ``"12"`` — values that pass
    "required" and then appear on an audit entry and in a salutation. Requiring
    one letter is the lightest rule that rejects those without having an opinion
    about which scripts, particles, hyphens or apostrophes a real name may use.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_length", MAX_NAME_LENGTH)
        super().__init__(**kwargs)

    def clean(self, value: Any) -> str:
        name = str(super().clean(value) or "")
        # Collapse runs of whitespace: "Asha   Rao" pasted from a PDF is the
        # same name as "Asha Rao", and storing both makes them different people
        # to every list, sort and search in the product.
        name = " ".join(name.split())
        if name and not any(unicodedata.category(ch).startswith("L") for ch in name):
            raise forms.ValidationError(_("Please enter a name."))
        return name


class _CrispyForm(forms.Form):
    submit_label = _("Continue")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.attrs = {"hx-disabled-elt": "find button[type=submit]"}
        self.helper.add_input(
            Submit(
                "submit",
                self.submit_label,
                css_class="btn-primary w-100",
                # These forms post normally — `layouts/auth.html` has no
                # `hx-boost`, deliberately, because an authentication response
                # sets cookies and changes who the session is. `hx-disabled-elt`
                # would therefore never fire, so the double-submit guard is
                # `data-submit-guard` in `assets/js/app.js`, which works on a
                # plain form.
                **{"data-submit-guard": self.submit_label},
            )
        )


class RegistrationForm(_CrispyForm):
    """Sign-up. Creates a **person**, not an organisation.

    Six fields, and every one of them is about the human being typing: who they
    are, where to reach them, and what they will sign in with. There is
    deliberately no organisation name here any more. It was the one field on
    this screen that asked about the business rather than the person, and it
    made a decision — "what is your company called" — at the single moment a
    user knows least about what the product will do with the answer.

    The workspace is still created the instant both channels are proven
    (``accounts.views._provision_signup_tenant``); it is simply named after the
    person until somebody says otherwise. See
    ``stacos.tenancy.services.default_workspace_name``.

    ``phone`` is the WhatsApp channel the second verification code goes to and
    is a hard requirement (``CLAUDE.md`` rule 5).
    """

    submit_label = _("Create account")

    first_name = PersonNameField(label=_("First name"))
    last_name = PersonNameField(label=_("Last name"))
    email = forms.EmailField(
        label=_("Work email"),
        max_length=MAX_EMAIL_LENGTH,
        help_text=_("You sign in with this, and one of the two codes goes here."),
    )
    phone = forms.CharField(
        label=_("WhatsApp number"),
        max_length=20,
        help_text=_(
            "We send your verification code on WhatsApp, so this must be a number "
            "with WhatsApp on it. Include the country code if outside India."
        ),
    )
    password = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput,
        max_length=MAX_PASSWORD_LENGTH,
    )
    confirm_password = forms.CharField(
        label=_("Confirm password"),
        widget=forms.PasswordInput,
        max_length=MAX_PASSWORD_LENGTH,
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        # `minlength` as a widget attribute rather than `min_length` on the
        # field: the field version adds a second, differently worded length
        # error in front of the validator's, and the validator is the one that
        # has to be right. The form posts with `novalidate`, so this is what the
        # strength meter reads, not something the browser enforces.
        self.fields["password"].widget.attrs["minlength"] = configured_min_password_length()

        #: Set by :meth:`clean_email` when the address belongs to an account
        #: that was started and never verified. The view updates that row
        #: instead of creating a second one — see its docstring for why that is
        #: safe and why it has to happen at all.
        self.reclaimable_user: User | None = None

        # The rules, from the configured validators rather than restated here.
        # A rule the screen promises and the server does not enforce (or the
        # reverse) is worse than no rule shown at all.
        self.fields["password"].help_text = " ".join(
            str(text) for text in password_validators_help_texts()
        )

        # Declaration order already matches, but stating the layout pins it: a
        # field added later cannot silently land in the middle of the sequence.
        self.helper.layout = Layout(
            Row(Column("first_name"), Column("last_name")),
            "email",
            "phone",
            Field("password", template=_STRENGTH_FIELD_TEMPLATE),
            Field("confirm_password", template=_CONFIRM_FIELD_TEMPLATE),
        )

    # -- Field-level -------------------------------------------------------

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip().lower()
        # Shape alone (what `forms.EmailField` already checked) accepts any
        # domain that merely looks like one — see `assert_real_email_domain`.
        # Before the duplicate lookup below: there is no point telling someone
        # a fake address is already taken.
        assert_real_email_domain(email)
        # Checked across all tenants: identity is global, so this is one of the
        # few legitimate unscoped reads.
        existing = User.objects.filter(email=email).first()
        if existing is None:
            return email

        if existing.is_fully_verified:
            raise forms.ValidationError(
                _("An account already exists for this email. Sign in instead.")
            )

        # An account that was started and never verified. Nobody has ever proven
        # control of this address, so nothing here belongs to anyone yet — and
        # refusing would strand the very common case this closes: the codes were
        # throttled, or the WhatsApp number had a typo, so sign-up created a row
        # and then failed. Before this, that user could neither sign up again
        # ("an account already exists") nor get past verification, with no way
        # out of it inside the product.
        #
        # It grants nothing: whoever re-registers still has to read a code sent
        # to this address before the account becomes usable.
        self.reclaimable_user = existing
        return email

    def clean_phone(self) -> str:
        phone = normalise_phone(self.cleaned_data["phone"])
        assert_whatsapp_capable(phone)
        # Only *verified* numbers are unique — an unverified duplicate must not
        # be able to lock a real owner out of their own number.
        duplicates = User.objects.filter(phone_e164=phone, phone_verified=True)
        if self.reclaimable_user is not None:
            duplicates = duplicates.exclude(pk=self.reclaimable_user.pk)
        if duplicates.exists():
            raise forms.ValidationError(
                _("This WhatsApp number is already verified on another account.")
            )
        return phone

    # -- Cross-field -------------------------------------------------------

    def clean(self) -> dict[str, Any]:
        data = super().clean() or self.cleaned_data
        password = data.get("password")
        confirmation = data.get("confirm_password")

        if password and confirmation and password != confirmation:
            self.add_error("confirm_password", _("The two passwords do not match."))

        if password:
            # Django's configured validators, run against an unsaved user so
            # `UserAttributeSimilarityValidator` can actually do its job — a
            # password that is the person's own surname or the local part of
            # their email is the single most common weak choice, and without the
            # instance that check has nothing to compare against.
            candidate = User(
                email=data.get("email", ""),
                first_name=data.get("first_name", ""),
                last_name=data.get("last_name", ""),
            )
            try:
                validate_password(password, user=candidate)
            except DjangoValidationError as exc:
                self.add_error("password", list(exc.messages))

        return data


class LoginForm(_CrispyForm):
    submit_label = _("Sign in")

    email = forms.EmailField(label=_("Email"), max_length=MAX_EMAIL_LENGTH)
    password = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput,
        max_length=MAX_PASSWORD_LENGTH,
    )

    def __init__(self, *args: Any, request: Any = None, **kwargs: Any) -> None:
        self.request = request
        self.user: User | None = None
        super().__init__(*args, **kwargs)
        self.helper.layout = Layout("email", Field("password", template=_MASKED_FIELD_TEMPLATE))

    def clean_email(self) -> str:
        return self.cleaned_data["email"].strip().lower()

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        email = data.get("email") or ""
        password = data.get("password")
        if email and password:
            user = authenticate(self.request, username=email, password=password)
            if user is None:
                # One message for both cases: naming which half was wrong tells
                # an attacker which addresses have accounts.
                raise forms.ValidationError(_("Email or password is incorrect."))
            if not user.is_active:
                # Deliberately distinct from the line above. A deactivated
                # account is not a guessing oracle — the person supplied the
                # correct password, so they already know the account exists —
                # and "email or password is incorrect" would send them round the
                # password-reset loop forever.
                raise forms.ValidationError(
                    _("This account has been deactivated. Please contact your administrator.")
                )
            self.user = user
        return data


class DualOtpForm(_CrispyForm):
    """**One screen, both codes, one submission.**

    This is the literal shape the product requires. Splitting it into two steps
    would let one channel be satisfied and the other deferred, which is exactly
    what the policy exists to prevent.
    """

    submit_label = _("Verify and continue")

    email_code = forms.CharField(
        label=_("Code sent to your email"),
        max_length=8,
        widget=forms.TextInput(
            attrs={
                "inputmode": "numeric",
                "autocomplete": "one-time-code",
                "pattern": "[0-9]*",
                "autofocus": "autofocus",
            }
        ),
    )
    phone_code = forms.CharField(
        label=_("Code sent on WhatsApp"),
        max_length=8,
        widget=forms.TextInput(
            attrs={"inputmode": "numeric", "autocomplete": "one-time-code", "pattern": "[0-9]*"}
        ),
    )
    remember_device = forms.BooleanField(
        label=_("Trust this device for 30 days"),
        required=False,
        initial=True,
        help_text=_("You will not be asked for codes again on this browser."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper.layout = Layout("email_code", "phone_code", "remember_device")
        self.helper.add_input(Submit("submit", self.submit_label, css_class="btn-primary w-100"))

    def clean_email_code(self) -> str:
        return self.cleaned_data["email_code"].strip().replace(" ", "")

    def clean_phone_code(self) -> str:
        return self.cleaned_data["phone_code"].strip().replace(" ", "")


class StepUpForm(_CrispyForm):
    """Re-authentication before a sensitive action."""

    submit_label = _("Confirm identity")

    password = forms.CharField(label=_("Your password"), widget=forms.PasswordInput)

    def __init__(self, *args: Any, user: User | None = None, **kwargs: Any) -> None:
        self.user = user
        super().__init__(*args, **kwargs)
        self.helper.layout = Layout(Field("password", template=_MASKED_FIELD_TEMPLATE))

    def clean_password(self) -> str:
        password = self.cleaned_data["password"]
        if self.user is None or not self.user.check_password(password):
            raise forms.ValidationError(_("That password is not correct."))
        return password
