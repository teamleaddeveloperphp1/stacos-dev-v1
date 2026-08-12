"""Authentication forms. Server-side validation is the only validation that counts."""

from __future__ import annotations

from typing import Any

import phonenumbers
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Submit
from django import forms
from django.contrib.auth import authenticate
from django.utils.translation import gettext_lazy as _

from stacos.accounts.models import User

__all__ = ["DualOtpForm", "LoginForm", "RegistrationForm", "StepUpForm"]


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


class _CrispyForm(forms.Form):
    submit_label = _("Continue")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.attrs = {"hx-disabled-elt": "find button[type=submit]"}
        self.helper.add_input(Submit("submit", self.submit_label, css_class="btn-primary w-100"))


class RegistrationForm(_CrispyForm):
    """Sign-up. Collects both channels up front, because both must be verified."""

    submit_label = _("Create account")

    full_name = forms.CharField(label=_("Full name"), max_length=200)
    email = forms.EmailField(label=_("Work email"))
    phone = forms.CharField(
        label=_("Mobile number"),
        max_length=20,
        help_text=_("We will text a verification code. Include the country code if outside India."),
    )
    password = forms.CharField(label=_("Password"), widget=forms.PasswordInput, min_length=10)

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].lower().strip()
        # Checked across all tenants: identity is global, so this is one of the
        # few legitimate unscoped reads.
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError(
                _("An account already exists for this email. Sign in instead.")
            )
        return email

    def clean_phone(self) -> str:
        phone = normalise_phone(self.cleaned_data["phone"])
        # Only *verified* numbers are unique — an unverified duplicate must not
        # be able to lock a real owner out of their own number.
        if User.objects.filter(phone_e164=phone, phone_verified=True).exists():
            raise forms.ValidationError(
                _("This mobile number is already verified on another account.")
            )
        return phone


class LoginForm(_CrispyForm):
    submit_label = _("Sign in")

    email = forms.EmailField(label=_("Email"))
    password = forms.CharField(label=_("Password"), widget=forms.PasswordInput)

    def __init__(self, *args: Any, request: Any = None, **kwargs: Any) -> None:
        self.request = request
        self.user: User | None = None
        super().__init__(*args, **kwargs)

    def clean(self) -> dict[str, Any]:
        super().clean()
        data = self.cleaned_data
        email = (data.get("email") or "").lower().strip()
        password = data.get("password")
        if email and password:
            user = authenticate(self.request, username=email, password=password)
            if user is None:
                # One message for both cases: naming which half was wrong tells
                # an attacker which addresses have accounts.
                raise forms.ValidationError(_("Email or password is incorrect."))
            if not user.is_active:
                raise forms.ValidationError(_("This account has been deactivated."))
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
        label=_("Code sent to your phone"),
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

    def clean_password(self) -> str:
        password = self.cleaned_data["password"]
        if self.user is None or not self.user.check_password(password):
            raise forms.ValidationError(_("That password is not correct."))
        return password
