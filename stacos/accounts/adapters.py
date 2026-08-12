"""
django-allauth adapters.

The one thing these exist to guarantee: **social sign-in satisfies neither OTP
channel**. Proving a Google, Microsoft or Apple identity proves that the provider
knows the user — not that this person controls the email address and the phone
number STACOS holds. So a social login lands in the *unverified* state and is
picked up by :class:`~stacos.accounts.middleware.VerificationGateMiddleware`
exactly like a password login from a new device.
"""

from __future__ import annotations

from typing import Any

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.http import HttpRequest

from stacos.accounts.middleware import SESSION_VERIFIED_KEY


class StacosAccountAdapter(DefaultAccountAdapter):
    """Password and email account behaviour."""

    def is_open_for_signup(self, request: HttpRequest) -> bool:
        return True

    def save_user(self, request: HttpRequest, user: Any, form: Any, commit: bool = True) -> Any:
        user = super().save_user(request, user, form, commit=False)
        user.email = (user.email or "").lower().strip()
        # Verification is STACOS's own dual-channel flow, never allauth's
        # single-channel email confirmation.
        user.email_verified = False
        user.phone_verified = False
        if commit:
            user.save()
        return user

    def get_login_redirect_url(self, request: HttpRequest) -> str:
        # The verification gate intercepts unverified sessions, so this is only
        # reached once both channels are satisfied.
        return "/app/"


class StacosSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Google / Microsoft / Apple behaviour."""

    def pre_social_login(self, request: HttpRequest, sociallogin: Any) -> None:
        """Force the dual-OTP gate for every social sign-in.

        Clearing the session flag here rather than trusting a default means a
        stale ``fully_verified`` from an earlier session cannot carry a social
        login straight into the application.
        """
        request.session[SESSION_VERIFIED_KEY] = False
        super().pre_social_login(request, sociallogin)

    def populate_user(self, request: HttpRequest, sociallogin: Any, data: dict[str, Any]) -> Any:
        user = super().populate_user(request, sociallogin, data)
        name = (data.get("name") or "").strip()
        if name and not user.full_name:
            user.full_name = name[:200]
        return user

    def save_user(self, request: HttpRequest, sociallogin: Any, form: Any = None) -> Any:
        user = super().save_user(request, sociallogin, form)
        # The provider asserts an email; STACOS still verifies it itself, along
        # with a phone number the provider knows nothing about.
        if user.email_verified or user.phone_verified:
            user.email_verified = False
            user.phone_verified = False
            user.save(update_fields=["email_verified", "phone_verified"])
        return user
