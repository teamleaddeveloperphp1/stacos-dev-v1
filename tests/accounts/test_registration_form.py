"""
Sign-up validation and the abandoned-account reclaim path — below the HTTP
layer, so a form-level rule is exercised without paying for a full request
each time. The end-to-end journey (register → verify → land in the app) lives
in ``tests/test_registration_provisioning.py`` and ``tests/test_auth_flow.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse

from stacos.accounts.forms import RegistrationForm, configured_min_password_length
from stacos.accounts.models import User
from stacos.accounts.whatsapp import MemoryWhatsAppProvider

pytestmark = pytest.mark.django_db


def _valid_data(**overrides: str) -> dict[str, str]:
    data = {
        "first_name": "Asha",
        "last_name": "Rao",
        "email": "asha@example.com",
        "phone": "9876543210",
        "password": "a-genuinely-long-passphrase",
        "confirm_password": "a-genuinely-long-passphrase",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Field-level validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("first_name", ""),
        ("first_name", "   "),
        ("first_name", "123"),
        ("last_name", ""),
        ("email", ""),
        ("email", "not-an-email"),
        ("phone", ""),
        ("phone", "12345"),
        ("password", ""),
        ("confirm_password", ""),
    ],
)
def test_invalid_values_are_rejected(field: str, value: str) -> None:
    form = RegistrationForm(_valid_data(**{field: value}))
    assert not form.is_valid()
    assert field in form.errors


def test_whitespace_in_a_name_is_collapsed() -> None:
    form = RegistrationForm(_valid_data(first_name="  Asha   Rani  "))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["first_name"] == "Asha Rani"


def test_email_is_lowercased_and_stripped() -> None:
    form = RegistrationForm(_valid_data(email="  Asha@Example.COM "))
    assert form.is_valid(), form.errors
    assert form.cleaned_data["email"] == "asha@example.com"


# ---------------------------------------------------------------------------
# The email domain has to be real, not merely shaped like one
#
# `forms.EmailField` only checks shape — an "@", a label either side, a dotted
# final label — which is exactly why "xyz@example.abc" got through it: every
# one of those is true of a domain that has never existed. See
# `stacos.accounts.forms.assert_real_email_domain`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "xyz@example.abc",
        "xyz@Example.ABC",
        "someone@example.xyz",
        "someone@example.io",
        "someone@example.test",
    ],
)
def test_a_placeholder_example_domain_is_rejected(email: str) -> None:
    """ "example.<anything>" other than the three RFC 2606 actually reserves
    (example.com/.net/.org, see below) is the same placeholder instinct aimed
    at a TLD nobody reserved — exactly what produced the bug report this test
    guards.
    """
    form = RegistrationForm(_valid_data(email=email))
    assert not form.is_valid()
    assert "email" in form.errors
    assert "placeholder" in str(form.errors["email"]).lower()


@pytest.mark.parametrize("email", ["asha@example.com", "asha@example.net", "asha@example.org"])
def test_the_rfc2606_reserved_example_domains_are_still_accepted(email: str) -> None:
    """These three are permanently reserved for exactly this purpose — this
    project's own test suite relies on ``example.com`` throughout — so the
    placeholder check must not start rejecting them.
    """
    form = RegistrationForm(_valid_data(email=email))
    assert form.is_valid(), form.errors


def test_a_domain_with_no_dns_record_is_rejected_when_verification_is_enabled(
    settings: Any,
) -> None:
    """The check the placeholder list cannot cover on its own: a syntactically
    fine, non-"example"-shaped domain that simply does not exist.
    """
    settings.EMAIL_DOMAIN_VERIFICATION_ENABLED = True
    form = RegistrationForm(_valid_data(email="asha@this-domain-does-not-exist-stacos.invalid"))
    assert not form.is_valid()
    assert "email" in form.errors
    assert "mail server" in str(form.errors["email"]).lower()


def test_dns_verification_is_off_by_default_in_tests(settings: Any) -> None:
    """``config/settings/test.py`` turns this off so the suite stays fast and
    offline — a made-up domain that would fail a live lookup must still pass
    here, unless a test explicitly opts back in (as the test above does).
    """
    assert settings.EMAIL_DOMAIN_VERIFICATION_ENABLED is False
    form = RegistrationForm(_valid_data(email="asha@this-domain-does-not-exist-stacos.invalid"))
    assert form.is_valid(), form.errors


def test_a_domain_with_mx_but_no_website_is_accepted(settings: Any) -> None:
    """A domain that only publishes ``MX`` records (e.g. Google Workspace mail
    with no website behind the apex) answers DNS with ``EAI_NODATA``, not
    ``EAI_NONAME`` — the name exists, it just has no ``A``/``AAAA`` record.
    That is a real, mail-capable domain and must not be rejected as if the
    lookup had returned "no such name".
    """
    import socket as socket_module

    settings.EMAIL_DOMAIN_VERIFICATION_ENABLED = True
    nodata_error = socket_module.gaierror(
        socket_module.EAI_NODATA, "No address associated with hostname"
    )
    with patch("socket.getaddrinfo", side_effect=nodata_error):
        form = RegistrationForm(_valid_data(email="asha@mail-only-domain.example"))
        assert form.is_valid(), form.errors


def test_a_resolver_failure_fails_open_not_closed(settings: Any) -> None:
    """A timeout or an unreachable resolver is a problem with the check, not
    proof the domain is fake — this must never turn into a sign-up nobody can
    complete because of a flaky network.
    """

    settings.EMAIL_DOMAIN_VERIFICATION_ENABLED = True
    with patch("socket.getaddrinfo", side_effect=TimeoutError("boom")):
        form = RegistrationForm(_valid_data())
    assert form.is_valid(), form.errors


def test_a_landline_style_number_is_rejected() -> None:
    """The second channel is WhatsApp — a number that cannot carry it is
    refused before a message is ever sent, not discovered afterwards as
    ``phone_unreachable``.
    """
    form = RegistrationForm(_valid_data(phone="044-12345678"))
    assert not form.is_valid()
    assert "phone" in form.errors


def test_a_duplicate_verified_email_is_refused() -> None:
    User.objects.create_user(
        email="asha@example.com",
        password="whatever-1234",
        email_verified=True,
        phone_verified=True,
    )
    form = RegistrationForm(_valid_data())
    assert not form.is_valid()
    assert "email" in form.errors
    assert "Sign in instead" in str(form.errors["email"])


def test_an_unverified_duplicate_email_is_allowed_through_for_reclaim() -> None:
    """The abandoned-signup case: a row exists, nobody has ever proven control
    of the address. See ``accounts.views._create_or_reclaim_user``.
    """
    abandoned = User.objects.create_user(email="asha@example.com", password="whatever-1234")
    form = RegistrationForm(_valid_data())
    assert form.is_valid(), form.errors
    assert form.reclaimable_user == abandoned


def test_a_duplicate_verified_phone_is_refused() -> None:
    User.objects.create_user(
        email="other@example.com",
        password="whatever-1234",
        phone_e164="+919876543210",
        phone_verified=True,
    )
    form = RegistrationForm(_valid_data())
    assert not form.is_valid()
    assert "phone" in form.errors


def test_an_unverified_duplicate_phone_does_not_block_a_different_email() -> None:
    User.objects.create_user(
        email="other@example.com",
        password="whatever-1234",
        phone_e164="+919876543210",
        phone_verified=False,
    )
    form = RegistrationForm(_valid_data())
    assert form.is_valid(), form.errors


# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------


def test_a_password_shorter_than_the_configured_minimum_is_rejected() -> None:
    short = "a" * (configured_min_password_length() - 1)
    form = RegistrationForm(_valid_data(password=short, confirm_password=short))
    assert not form.is_valid()
    assert "password" in form.errors


def test_a_purely_numeric_password_is_rejected() -> None:
    numeric = "1234567890123"
    form = RegistrationForm(_valid_data(password=numeric, confirm_password=numeric))
    assert not form.is_valid()
    assert "password" in form.errors


def test_a_common_password_is_rejected_even_when_long_enough() -> None:
    form = RegistrationForm(_valid_data(password="password123", confirm_password="password123"))
    assert not form.is_valid()
    assert "password" in form.errors


def test_a_password_matching_the_users_own_email_is_rejected() -> None:
    """``UserAttributeSimilarityValidator`` needs a candidate user to compare
    against — this is what proves the candidate is actually built and passed.
    """
    form = RegistrationForm(
        _valid_data(
            email="ashafounder2026@example.com",
            password="ashafounder2026",
            confirm_password="ashafounder2026",
        )
    )
    assert not form.is_valid()
    assert "password" in form.errors


def test_mismatched_confirmation_is_rejected_without_touching_the_database() -> None:
    form = RegistrationForm(
        _valid_data(password="a-genuinely-long-passphrase", confirm_password="different-one-too")
    )
    assert not form.is_valid()
    assert "confirm_password" in form.errors


def test_a_strong_password_passes() -> None:
    form = RegistrationForm(_valid_data())
    assert form.is_valid(), form.errors


# ---------------------------------------------------------------------------
# The reclaim path, over HTTP
# ---------------------------------------------------------------------------


def test_registering_over_an_abandoned_account_reuses_the_row(client: Client) -> None:
    abandoned = User.objects.create_user(email="asha@example.com", password="whatever-1234")
    original_stamp = abandoned.security_stamp

    response = client.post(reverse("accounts:register"), _valid_data())
    assert response.status_code == 302

    abandoned.refresh_from_db()
    assert User.objects.filter(email="asha@example.com").count() == 1
    assert abandoned.first_name == "Asha"
    assert abandoned.check_password("a-genuinely-long-passphrase")
    # Whatever the abandoned attempt left behind (a trusted device, a live
    # session) must not carry over to whoever just proved control of the
    # address by completing this form.
    assert abandoned.security_stamp != original_stamp


def test_registering_over_a_fully_verified_account_is_refused(client: Client) -> None:
    User.objects.create_user(
        email="asha@example.com",
        password="whatever-1234",
        email_verified=True,
        phone_verified=True,
    )
    mail.outbox.clear()
    MemoryWhatsAppProvider.clear()

    response = client.post(reverse("accounts:register"), _valid_data())

    assert response.status_code == 200
    assert "Sign in instead" in response.content.decode()
    assert User.objects.filter(email="asha@example.com").count() == 1
    assert not mail.outbox, "a verification code went out for a sign-up that was refused"
