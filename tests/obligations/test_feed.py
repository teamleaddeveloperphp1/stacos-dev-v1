"""
The read-only ``.ics`` calendar subscription feed.

The feed endpoint carries no Django session at all — the token in the URL is
the only credential — so its risk is not "does this view check a permission"
(the interactive subscribe/regenerate/revoke views already carry the
calendar's own ``@require_permission``) but the two ways a bearer-token
feature actually fails in practice: a forged or stale token must never
resolve to anything, and a valid token must never surface more than the one
user it belongs to can see.
"""

from __future__ import annotations

from datetime import date

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.obligations.feed import create_feed_token
from stacos.obligations.models import CalendarFeedToken
from stacos.obligations.services import materialise
from stacos.tenancy.models import Entity, EntityRegistration
from tests.obligations.test_views import signed_in  # noqa: F401

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 8, 12)


@pytest.fixture
def rival_calendar(rival_entity: Entity) -> Entity:
    """A fully materialised calendar belonging to somebody else entirely."""
    with platform_scope(reason="test-fixture"):
        EntityRegistration.objects.create(
            tenant=rival_entity.tenant, entity=rival_entity, type="PAN", value="AAACR1234C"
        )
        materialise(rival_entity, as_of=AS_OF, trigger="ONBOARDING")
    return rival_entity


def test_feed_lists_the_users_own_obligations(org_owner: User, materialised: Entity) -> None:
    _token, raw = create_feed_token(org_owner)

    response = Client().get(reverse("compliance:feed", args=[raw]))

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/calendar")
    body = response.content.decode()
    assert "BEGIN:VCALENDAR" in body
    assert "BEGIN:VEVENT" in body
    assert materialised.name in body


def test_feed_does_not_leak_another_tenants_obligations(
    org_owner: User, materialised: Entity, rival_owner: User, rival_calendar: Entity
) -> None:
    """Act as the rival's own subscription, fetch the shared feed endpoint."""
    _org_token, org_raw = create_feed_token(org_owner)
    _rival_token, rival_raw = create_feed_token(rival_owner)
    client = Client()

    rival_body = client.get(reverse("compliance:feed", args=[rival_raw])).content.decode()
    assert materialised.name not in rival_body
    assert rival_calendar.name in rival_body

    org_body = client.get(reverse("compliance:feed", args=[org_raw])).content.decode()
    assert rival_calendar.name not in org_body
    assert materialised.name in org_body


def test_a_forged_token_404s(org_owner: User, materialised: Entity) -> None:
    response = Client().get(reverse("compliance:feed", args=["not-a-real-token.at-all"]))
    assert response.status_code == 404


def test_a_revoked_token_stops_working(org_owner: User, materialised: Entity) -> None:
    token, raw = create_feed_token(org_owner)
    client = Client()
    assert client.get(reverse("compliance:feed", args=[raw])).status_code == 200

    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])

    assert client.get(reverse("compliance:feed", args=[raw])).status_code == 404


def test_regenerating_invalidates_the_old_link(org_owner: User, materialised: Entity) -> None:
    _first, first_raw = create_feed_token(org_owner)
    _second, second_raw = create_feed_token(org_owner)
    client = Client()

    assert client.get(reverse("compliance:feed", args=[first_raw])).status_code == 404
    assert client.get(reverse("compliance:feed", args=[second_raw])).status_code == 200


def test_subscribe_modal_reflects_whether_a_link_exists(
    signed_in: Client, org_owner: User
) -> None:
    response = signed_in.get(reverse("compliance:subscribe"), headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Generate calendar link" in response.content.decode()

    create_feed_token(org_owner)

    response = signed_in.get(reverse("compliance:subscribe"), headers={"HX-Request": "true"})
    assert "Regenerate link" in response.content.decode()


def test_creating_a_link_shows_it_exactly_once(signed_in: Client) -> None:
    response = signed_in.post(
        reverse("compliance:subscribe_create"), headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert "webcal://" in response.content.decode()
    assert CalendarFeedToken.objects.filter(revoked_at__isnull=True).count() == 1


def test_revoking_stops_the_feed(signed_in: Client, org_owner: User, materialised: Entity) -> None:
    create_response = signed_in.post(
        reverse("compliance:subscribe_create"), headers={"HX-Request": "true"}
    )
    body = create_response.content.decode()
    # Pull the raw token back out of the rendered webcal:// URL rather than
    # threading it through a second channel — this is exactly what a user
    # copying the link out of the modal does.
    feed_path = body.split('value="webcal://', 1)[1].split('"', 1)[0]
    raw = feed_path.rsplit("/feed/", 1)[1].removesuffix(".ics")

    client = Client()
    assert client.get(reverse("compliance:feed", args=[raw])).status_code == 200

    signed_in.post(reverse("compliance:subscribe_revoke"), headers={"HX-Request": "true"})

    assert client.get(reverse("compliance:feed", args=[raw])).status_code == 404
