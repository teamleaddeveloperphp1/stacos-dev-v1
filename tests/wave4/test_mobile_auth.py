"""
Mobile sign-in: password, then always a dual-OTP challenge — there is no
trusted-device cookie on a phone. Not previously covered at all; added
alongside the failure-handling fix in ``MobileLoginView.post`` (a provider
outage used to reach the client as a bare 500 with no JSON body to parse).
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
from django.core import mail
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from stacos.accounts.models import User
from stacos.accounts.whatsapp import MemoryWhatsAppProvider

pytestmark = pytest.mark.django_db

CODE_RE = re.compile(r"\b(\d{6})\b")


@pytest.fixture
def verified_user() -> User:
    return User.objects.create_user(
        email="mobile@example.com",
        password="a-genuinely-long-passphrase",
        email_verified=True,
        phone_verified=True,
        phone_e164="+919876543299",
    )


def test_valid_credentials_issue_a_verification_challenge(verified_user: User) -> None:
    response = APIClient().post(
        reverse("api:auth_login"),
        {"email": "mobile@example.com", "password": "a-genuinely-long-passphrase"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert "verification_id" in response.data


def test_the_wrong_password_is_a_401_with_no_hint_which_field_was_wrong(
    verified_user: User,
) -> None:
    response = APIClient().post(
        reverse("api:auth_login"),
        {"email": "mobile@example.com", "password": "definitely-wrong"},
    )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert "incorrect" in response.data["detail"]


def test_both_codes_together_issue_tokens(verified_user: User) -> None:
    client = APIClient()
    login = client.post(
        reverse("api:auth_login"),
        {"email": "mobile@example.com", "password": "a-genuinely-long-passphrase"},
    )
    verification_id = login.data["verification_id"]

    email_code = CODE_RE.search(mail.outbox[-1].body)
    assert email_code
    phone_code = MemoryWhatsAppProvider.outbox[-1].params.get("code")
    assert phone_code

    response = client.post(
        reverse("api:auth_verify"),
        {
            "verification_id": verification_id,
            "email_code": email_code.group(1),
            "phone_code": phone_code,
        },
    )
    assert response.status_code == status.HTTP_200_OK
    assert "access" in response.data
    assert "refresh" in response.data


def test_a_provider_outage_is_a_clean_json_error_not_a_bare_500(verified_user: User) -> None:
    """The password has already been checked at this point — a failure
    sending the codes must read as "try again shortly", not as an
    unhandled-exception page a mobile client cannot parse into anything.
    """
    with patch("stacos.api.views.start_verification", side_effect=ConnectionError("boom")):
        response = APIClient().post(
            reverse("api:auth_login"),
            {"email": "mobile@example.com", "password": "a-genuinely-long-passphrase"},
        )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "detail" in response.data
    assert "ConnectionError" not in str(response.data)


def test_an_unknown_verification_id_is_a_clean_400(verified_user: User) -> None:
    response = APIClient().post(
        reverse("api:auth_verify"),
        {
            "verification_id": "00000000-0000-0000-0000-000000000000",
            "email_code": "123456",
            "phone_code": "123456",
        },
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
