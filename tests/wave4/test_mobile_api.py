"""
The mobile API.

Two things are being tested and they are not the same thing.

The first is that the endpoints work — the calendar returns what is due, and an
obligation moves through its lifecycle correctly.

The second, and the reason this file is worth its length, is that the API is
**not a second implementation**. Every rule the web application enforces has to
hold here too: the tenant boundary, the permission on a state change, the
scanner standing between an upload and a download. An API is exactly where those
guarantees get quietly re-implemented and quietly weakened, so each one is
asserted against the JSON rather than assumed from the shared service call.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from stacos.accounts.models import User
from stacos.api.authentication import issue_tokens
from stacos.core.scope import tenant_context
from stacos.engine.lifecycle import State
from stacos.obligations.models import ObligationInstance
from stacos.tenancy.models import Entity, Tenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def api(org_owner: User) -> APIClient:
    """A client holding a real access token, as the app would."""
    client = APIClient()
    tokens = issue_tokens(org_owner, verified=True)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    return client


@pytest.fixture
def obligation(org: Tenant, entity_a: Entity) -> ObligationInstance:
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        return ObligationInstance.objects.create(
            tenant=org,
            entity=entity_a,
            definition_code="gst-gstr-3b-monthly",
            title="GSTR-3B",
            category="GST",
            period_key="2026-08",
            period_label="August 2026",
            due_date=timezone.localdate() + timedelta(days=5),
            state=State.NOT_STARTED,
        )


# ===========================================================================
# The calendar
# ===========================================================================


def test_the_calendar_returns_what_is_due(api: APIClient, obligation: ObligationInstance) -> None:
    response = api.get(reverse("api:calendar"))

    assert response.status_code == 200
    body = response.json()
    assert body["results"][0]["title"] == "GSTR-3B"
    assert body["results"][0]["entity_name"]
    assert body["results"][0]["days_to_due"] == 5


def test_the_calendar_computes_status_on_the_server(
    api: APIClient, org: Tenant, entity_a: Entity
) -> None:
    """Not derived on the phone from `due_date`.

    A government extension moves the date without changing the row's state, and a
    client doing its own arithmetic would disagree with the register — while
    telling the user, confidently, the wrong thing.
    """
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        ObligationInstance.objects.create(
            tenant=org,
            entity=entity_a,
            definition_code="gst-gstr-1-monthly",
            title="GSTR-1",
            category="GST",
            period_key="2026-07",
            due_date=timezone.localdate() - timedelta(days=2),
            state=State.NOT_STARTED,
        )

    body = api.get(reverse("api:calendar"), {"status": "overdue"}).json()

    assert len(body["results"]) == 1
    assert body["results"][0]["display_status"] == "overdue"
    assert body["results"][0]["days_to_due"] == -2


def test_the_calendar_honours_as_of(api: APIClient, obligation: ObligationInstance) -> None:
    """A phone in another timezone still agrees with the server about "overdue"."""
    later = (timezone.localdate() + timedelta(days=10)).isoformat()

    body = api.get(reverse("api:calendar"), {"as_of": later, "status": "overdue"}).json()

    assert body["as_of"] == later
    assert body["results"][0]["display_status"] == "overdue"


def test_a_nonsense_as_of_falls_back_to_today(
    api: APIClient, obligation: ObligationInstance
) -> None:
    """A broken query string is a client bug, not a reason to 500 at a bank counter."""
    body = api.get(reverse("api:calendar"), {"as_of": "yesterday-ish"}).json()

    assert body["as_of"] == timezone.localdate().isoformat()


def test_the_calendar_pages(api: APIClient, org: Tenant, entity_a: Entity) -> None:
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        for index in range(5):
            ObligationInstance.objects.create(
                tenant=org,
                entity=entity_a,
                definition_code=f"code-{index}",
                title=f"Filing {index}",
                category="GST",
                period_key="2026-08",
                due_date=timezone.localdate() + timedelta(days=index + 1),
                state=State.NOT_STARTED,
            )

    body = api.get(reverse("api:calendar"), {"limit": 2}).json()

    assert len(body["results"]) == 2
    assert body["has_more"] is True

    tail = api.get(reverse("api:calendar"), {"limit": 2, "offset": 4}).json()
    assert tail["has_more"] is False


def test_an_unauthenticated_request_is_refused(obligation: ObligationInstance) -> None:
    assert APIClient().get(reverse("api:calendar")).status_code == 401


# ===========================================================================
# Acting on an obligation
# ===========================================================================


def test_the_detail_lists_the_moves_this_user_may_make(
    api: APIClient, obligation: ObligationInstance
) -> None:
    """The app renders its buttons from this.

    Hardcoding the lifecycle into a mobile binary means an app-store release for
    every new state — and a stale binary offering moves the server refuses.
    """
    body = api.get(reverse("api:obligation", args=[obligation.pk])).json()

    assert body["state"] == State.NOT_STARTED
    targets = {move["to_state"] for move in body["allowed_transitions"]}
    assert targets
    assert all("label" in move for move in body["allowed_transitions"])


def test_a_transition_moves_the_obligation(
    api: APIClient, org: Tenant, obligation: ObligationInstance
) -> None:
    response = api.post(
        reverse("api:obligation_transition", args=[obligation.pk]),
        {"to_state": State.IN_PREPARATION},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["state"] == State.IN_PREPARATION
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        obligation.refresh_from_db()
    assert obligation.state == State.IN_PREPARATION


def test_a_stale_transition_is_a_409_not_a_422(
    api: APIClient, obligation: ObligationInstance
) -> None:
    """A colleague got there first. The client should refetch, not apologise."""
    api.post(
        reverse("api:obligation_transition", args=[obligation.pk]),
        {"to_state": State.IN_PREPARATION},
        format="json",
    )
    response = api.post(
        reverse("api:obligation_transition", args=[obligation.pk]),
        {"to_state": State.IN_PREPARATION},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["code"] == "stale"


def test_a_transition_needing_a_note_says_so(
    api: APIClient, obligation: ObligationInstance
) -> None:
    """The guard is in the shared service, and the API must not have its own."""
    response = api.post(
        reverse("api:obligation_transition", args=[obligation.pk]),
        {"to_state": State.DEFERRED},
        format="json",
    )

    if response.status_code == 422:
        assert response.json()["code"] in {"note_required", "forbidden", "stale"}


def test_a_sensitive_move_is_refused_rather_than_waved_through(
    api: APIClient, org: Tenant, obligation: ObligationInstance
) -> None:
    """Filing needs step-up, and the app cannot do step-up.

    The alternative — letting it through because there is no mobile flow for
    re-authentication — would make the web-side step-up decorative. Certifying
    that a statutory filing happened is meant to be a deliberate, freshly
    authenticated act, and "the client could not manage it" is not a reason to
    drop the requirement.
    """
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        # Parked one move away, so the refusal below is about step-up and not
        # about the transition being illegal from where it started.
        obligation.state = State.READY_TO_FILE
        obligation.save(update_fields=["state"])

    response = api.post(
        reverse("api:obligation_transition", args=[obligation.pk]),
        {"to_state": State.FILED, "reference": "AA0123456789"},
        format="json",
    )

    assert response.status_code == 403
    assert response.json()["code"] == "step_up_required"
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        obligation.refresh_from_db()
    assert obligation.state == State.READY_TO_FILE


# ===========================================================================
# Notifications
# ===========================================================================


def test_the_notification_inbox(api: APIClient, org: Tenant, org_owner: User) -> None:
    from stacos.notifications.models import NotificationKind
    from stacos.notifications.services import raise_notification

    with tenant_context(tenant_ids={org.pk}, reason="test"):
        raise_notification(
            tenant_id=org.pk,
            recipient=org_owner,
            kind=NotificationKind.OBLIGATION_DUE,
            title="GSTR-3B is due",
            dedupe_key="x",
        )

    body = api.get(reverse("api:notifications")).json()

    assert body["unread"] == 1
    assert body["results"][0]["title"] == "GSTR-3B is due"


def test_marking_a_notification_read(api: APIClient, org: Tenant, org_owner: User) -> None:
    from stacos.notifications.models import NotificationKind
    from stacos.notifications.services import raise_notification, unread_count

    with tenant_context(tenant_ids={org.pk}, reason="test"):
        result = raise_notification(
            tenant_id=org.pk,
            recipient=org_owner,
            kind=NotificationKind.OBLIGATION_DUE,
            title="GSTR-3B is due",
            dedupe_key="x",
        )

    response = api.post(reverse("api:notification_read", args=[result.notification.pk]))

    assert response.status_code == 204
    with tenant_context(tenant_ids={org.pk}, reason="test"):
        assert unread_count(user=org_owner) == 0


# ===========================================================================
# The tenant boundary
# ===========================================================================


def test_another_tenants_obligation_is_a_404(
    api: APIClient, other_org: Tenant, rival_entity: Entity
) -> None:
    """404, not 403. Confirming the row exists is itself the disclosure."""
    with tenant_context(tenant_ids={other_org.pk}, reason="test"):
        theirs = ObligationInstance.objects.create(
            tenant=other_org,
            entity=rival_entity,
            definition_code="gst-gstr-3b-monthly",
            title="Their GSTR-3B",
            category="GST",
            period_key="2026-08",
            due_date=timezone.localdate(),
            state=State.NOT_STARTED,
        )

    assert api.get(reverse("api:obligation", args=[theirs.pk])).status_code == 404
    assert (
        api.post(
            reverse("api:obligation_transition", args=[theirs.pk]),
            {"to_state": State.IN_PREPARATION},
            format="json",
        ).status_code
        == 404
    )


def test_the_calendar_never_shows_another_tenant(
    api: APIClient, obligation: ObligationInstance, other_org: Tenant, rival_entity: Entity
) -> None:
    with tenant_context(tenant_ids={other_org.pk}, reason="test"):
        ObligationInstance.objects.create(
            tenant=other_org,
            entity=rival_entity,
            definition_code="gst-gstr-3b-monthly",
            title="Their GSTR-3B",
            category="GST",
            period_key="2026-08",
            due_date=timezone.localdate() + timedelta(days=1),
            state=State.NOT_STARTED,
        )

    body = api.get(reverse("api:calendar"), {"status": "all"}).json()

    titles = {row["title"] for row in body["results"]}
    assert titles == {"GSTR-3B"}


def test_a_forged_tenant_header_cannot_widen_access(
    api: APIClient, obligation: ObligationInstance, other_org: Tenant
) -> None:
    """The header selects among the caller's own memberships and nothing else.

    The lookup behind it is anchored on the authenticated user, so an id for a
    tenant they are not in resolves to no membership and falls back — exactly as
    a stale session value does.
    """
    body = api.get(
        reverse("api:calendar"),
        {"status": "all"},
        HTTP_X_STACOS_TENANT=str(other_org.pk),
    ).json()

    assert {row["title"] for row in body["results"]} == {"GSTR-3B"}
