"""Creating a second, separate organisation from the tenant switcher.

Before this existed, ``onboarding.views.finish`` always reused
``request.tenant`` when one was bound — correct for "add a business to the
organisation I am already in", and silently wrong for somebody who wanted a
second, unrelated organisation: a signed-in user always has a tenant bound, so
the wizard folded every second run into the first organisation with no way to
say otherwise. These tests pin the fix: an explicit ``?new_org=1`` entry point
that a signed-in user can still reach, which provisions a real second tenant.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.tenancy.models import Entity, Membership, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def newcomer(db) -> User:
    return User.objects.create_user(
        email="founder@example.com", password="correct-horse-battery", full_name="Asha Founder"
    )


@pytest.fixture
def signed_in(client: Client, newcomer: User) -> Client:
    return sign_in(client, newcomer)


def _complete_wizard(client: Client, *, name: str) -> None:
    client.post(
        reverse("onboarding:profile"),
        {"name": name, "entity_type": "PVT_LTD", "registered_office_state": "IN-KA"},
    )
    client.post(reverse("onboarding:finish"))


def test_a_second_run_without_the_flag_adds_a_business_to_the_same_organisation(
    signed_in: Client,
) -> None:
    """The existing, still-correct behaviour: plain "add a business"."""
    _complete_wizard(signed_in, name="Aa Business")
    _complete_wizard(signed_in, name="Bb Business")

    with platform_scope(reason="test"):
        assert Tenant.objects.count() == 1
        assert Membership.objects.count() == 1
        tenant = Tenant.objects.get()
        assert set(Entity.objects.filter(tenant=tenant).values_list("name", flat=True)) == {
            "Aa Business",
            "Bb Business",
        }


def test_new_org_creates_a_genuinely_separate_organisation(signed_in: Client) -> None:
    """The fix: "Create a new organisation" must not fold into the current one."""
    _complete_wizard(signed_in, name="Aa Business")

    # The session is now pointed at "Aa Business" — the exact condition that
    # made the old, flagless `finish` silently reuse it.
    response = signed_in.get(reverse("onboarding:identity"), {"new_org": "1"})
    assert response.status_code == 200

    _complete_wizard(signed_in, name="Cc Business")

    with platform_scope(reason="test"):
        assert Tenant.objects.count() == 2
        assert Membership.objects.filter(user__email="founder@example.com").count() == 2

        aa = Entity.objects.get(name="Aa Business")
        cc = Entity.objects.get(name="Cc Business")
        assert aa.tenant_id != cc.tenant_id


def test_new_org_flag_does_not_leak_into_a_later_plain_run(signed_in: Client) -> None:
    """Starting a new organisation must not turn every *later* run into one too."""
    signed_in.get(reverse("onboarding:identity"), {"new_org": "1"})
    _complete_wizard(signed_in, name="Dd Business")

    # No `new_org` this time — should fold into whichever org is now current.
    _complete_wizard(signed_in, name="Ee Business")

    with platform_scope(reason="test"):
        assert Tenant.objects.count() == 1
        dd = Entity.objects.get(name="Dd Business")
        ee = Entity.objects.get(name="Ee Business")
        assert dd.tenant_id == ee.tenant_id


def test_the_switcher_offers_only_new_organisation_once_a_user_belongs_to_one(
    signed_in: Client,
) -> None:
    """ "Add a business here" is not offered from the switcher any more —
    only "Create a new organisation" is. The plain-run codepath it used to
    link to still exists (see the tests above), just not from here.
    """
    _complete_wizard(signed_in, name="Aa Business")

    page = signed_in.get(reverse("app:dashboard"))
    assert b"Add a business here" not in page.content
    assert b"Create a new organisation" in page.content
    assert b"Set up an organisation" not in page.content
