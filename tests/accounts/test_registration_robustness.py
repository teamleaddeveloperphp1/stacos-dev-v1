"""
Registration and sign-in against a world that fails: a provider outage, a
race between two identical submissions, an account reclaimed mid-flight. None
of these are exotic — a slow phone on a factory-floor connection produces the
double-submit every time, and an OTP provider genuinely does time out — so
each is a scenario, not a mock-the-internals exercise.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core import mail
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse

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


def test_a_provider_outage_produces_a_plain_english_error_not_a_500(client: Client) -> None:
    """Both ``send_mail`` and the WhatsApp provider are outside this
    process's control and both fail in production. The account created a
    moment earlier must survive to be reclaimed by a retry — see
    ``_create_or_reclaim_user`` — rather than being left as an orphan nobody
    can sign up over.
    """
    with patch("stacos.accounts.views.start_verification", side_effect=ConnectionError("boom")):
        response = client.post(reverse("accounts:register"), _valid_data())

    assert response.status_code == 200
    body = response.content.decode()
    assert "could not send your verification codes" in body
    assert "ConnectionError" not in body
    assert "Traceback" not in body

    # The account is there, unverified, ready to be reclaimed by a retry.
    user = User.objects.get(email="asha@example.com")
    assert not user.is_fully_verified

    # A second attempt, with the provider healthy again, must succeed rather
    # than bouncing off "an account already exists".
    response = client.post(reverse("accounts:register"), _valid_data())
    assert response.status_code == 302
    assert User.objects.filter(email="asha@example.com").count() == 1


def test_two_identical_submissions_in_flight_together_do_not_both_succeed(
    client: Client,
) -> None:
    """The form's own duplicate check reads a moment before the write, so a
    genuine race — two requests for the same address landing together — has
    to be caught by the database constraint instead. This simulates that race
    by making the reclaim-or-create step raise the constraint violation
    directly, which is what actually happens when a second transaction's
    insert loses the race against the first's commit.
    """
    with patch(
        "stacos.accounts.views._create_or_reclaim_user",
        side_effect=IntegrityError("duplicate key value violates unique constraint"),
    ):
        response = client.post(reverse("accounts:register"), _valid_data())

    assert response.status_code == 200
    body = response.content.decode()
    assert "already exists" in body
    assert "IntegrityError" not in body
    assert "constraint" not in body
    assert not mail.outbox, "no codes should go out for a submission that lost the race"


def test_an_authenticated_user_who_resubmits_signup_is_bounced_to_the_app(
    client: Client,
) -> None:
    """The GET-based half of the double-submit guard: someone who already
    completed sign-up (perhaps in another tab) and lands back on the
    register screen is sent onward rather than shown a form that would only
    fail with "already exists".
    """
    user = User.objects.create_user(
        email="asha@example.com",
        password="whatever-1234",
        email_verified=True,
        phone_verified=True,
    )
    client.force_login(user)

    response = client.get(reverse("accounts:register"))
    assert response.status_code == 302
    assert response["Location"] == "/app/"


def test_login_verification_failure_leaves_the_password_check_intact(client: Client) -> None:
    """A provider outage during the new-device OTP step must not leave a
    half-authenticated session — the password was correct, so the user is
    told to try the *code*, not asked to retype a password they already got
    right.
    """
    User.objects.create_user(
        email="asha@example.com",
        password="a-genuinely-long-passphrase",
        email_verified=True,
        phone_verified=True,
        phone_e164="+919876543210",
    )

    with patch("stacos.accounts.views.start_verification", side_effect=TimeoutError("boom")):
        response = client.post(
            reverse("accounts:login"),
            {"email": "asha@example.com", "password": "a-genuinely-long-passphrase"},
        )

    assert response.status_code == 200
    assert "could not send your verification codes" in response.content.decode()
    assert not client.session.get("_auth_user_id"), "login must not partially succeed"


def test_resending_after_a_dispatch_failure_still_works(client: Client) -> None:
    """A transient failure on the first send must not strand the account —
    the ordinary resend path has to still work once the provider recovers.
    """
    with patch("stacos.accounts.views.start_verification", side_effect=OSError("boom")):
        client.post(reverse("accounts:register"), _valid_data())

    assert User.objects.filter(email="asha@example.com").exists()
    mail.outbox.clear()
    MemoryWhatsAppProvider.clear()

    response = client.post(reverse("accounts:register"), _valid_data())
    assert response.status_code == 302
    assert mail.outbox, "the retry should have actually sent a code"
