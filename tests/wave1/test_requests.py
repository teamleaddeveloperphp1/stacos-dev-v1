"""
Information requests: the list, the guards, and the state derivation.

The property worth defending is that a request's state is *derived* from its
items. A request that says "answered" while an item is outstanding is a request
somebody will act on wrongly.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.requests.models import InformationRequest, RequestItem, RequestState
from stacos.requests.services import (
    RequestError,
    close_request,
    due_for_reminder,
    record_response,
    reject_response,
    send_request,
)
from stacos.tenancy.models import Entity, Tenant
from stacos.vault.models import LinkTarget
from stacos.vault.services import attach, store
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

TODAY = date(2026, 8, 12)


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    """Signed in with step-up fresh.

    The tracker's sensitive actions — recording a response, closing a notice —
    demand re-authentication, so a fixture without it would test the redirect
    rather than the action.
    """
    return sign_in(client, org_owner, step_up=True)


@pytest.fixture
def a_request(org: Tenant, entity_a: Entity, org_owner: User) -> InformationRequest:
    with platform_scope(reason="test-fixture"):
        information_request = InformationRequest.objects.create(
            tenant=org,
            entity=entity_a,
            title="September GST working papers",
            due_on=TODAY + timedelta(days=5),
            assigned_to=org_owner,
        )
        for ordinal, (label, kind) in enumerate(
            [
                ("Sales register", RequestItem.Kind.DOCUMENT),
                ("Purchase register", RequestItem.Kind.DOCUMENT),
                ("Did you make any exempt supplies?", RequestItem.Kind.CONFIRMATION),
                ("Bank statement (optional)", RequestItem.Kind.DOCUMENT),
            ]
        ):
            RequestItem.objects.create(
                tenant=org,
                entity=entity_a,
                request=information_request,
                label=label,
                kind=kind,
                ordinal=ordinal,
                is_mandatory=ordinal < 3,
            )
        return information_request


# ===========================================================================
# Sending
# ===========================================================================


def test_an_empty_request_cannot_be_sent(org: Tenant, entity_a: Entity, org_owner: User) -> None:
    """ "Please send me the things" with no list is not a request anyone can action."""
    with platform_scope(reason="test"):
        empty = InformationRequest.objects.create(
            tenant=org, entity=entity_a, title="Stuff", assigned_to=org_owner
        )
        with pytest.raises(RequestError):
            send_request(empty)


def test_a_request_addressed_to_nobody_cannot_be_sent(org: Tenant, entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        orphan = InformationRequest.objects.create(tenant=org, entity=entity_a, title="Stuff")
        RequestItem.objects.create(tenant=org, entity=entity_a, request=orphan, label="Something")
        with pytest.raises(RequestError):
            send_request(orphan)


def test_sending_stamps_the_time(a_request: InformationRequest) -> None:
    with platform_scope(reason="test"):
        send_request(a_request)
        a_request.refresh_from_db()

    assert a_request.state == RequestState.SENT
    assert a_request.sent_at is not None


# ===========================================================================
# State is derived from the items
# ===========================================================================


def test_answering_some_items_makes_it_partially_answered(
    a_request: InformationRequest, org: Tenant, entity_a: Entity
) -> None:
    with platform_scope(reason="test"):
        send_request(a_request)
        item = a_request.items.get(label="Did you make any exempt supplies?")
        record_response(item, value="NO")
        a_request.refresh_from_db()

    assert a_request.state == RequestState.PARTIALLY_ANSWERED


def test_answering_every_mandatory_item_makes_it_answered(
    a_request: InformationRequest, org: Tenant, entity_a: Entity
) -> None:
    """The optional item is deliberately left outstanding.

    An optional item nobody supplied must not hold a filing open, which is how a
    request sits at 3/4 forever.
    """
    with platform_scope(reason="test"):
        send_request(a_request)

        for item in a_request.items.filter(kind=RequestItem.Kind.DOCUMENT, is_mandatory=True):
            document, _ = store(
                tenant=org,
                entity=entity_a,
                upload=SimpleUploadedFile(f"{item.label}.pdf", item.label.encode()),
            )
            attach(document, target_type=LinkTarget.REQUEST_ITEM, target_id=item.pk)

        confirmation = a_request.items.get(kind=RequestItem.Kind.CONFIRMATION)
        record_response(confirmation, value="NO")
        a_request.refresh_from_db()

    assert a_request.state == RequestState.ANSWERED
    assert a_request.answered_at is not None


def test_attaching_a_document_answers_a_document_item(
    a_request: InformationRequest, org: Tenant, entity_a: Entity
) -> None:
    """The denormalised count is what makes the list cheap; this proves it is kept true."""
    with platform_scope(reason="test"):
        item = a_request.items.filter(kind=RequestItem.Kind.DOCUMENT).first()
        assert item is not None
        assert not item.is_answered

        document, _ = store(
            tenant=org, entity=entity_a, upload=SimpleUploadedFile("sales.pdf", b"rows")
        )
        attach(document, target_type=LinkTarget.REQUEST_ITEM, target_id=item.pk)
        item.refresh_from_db()

    assert item.document_count == 1
    assert item.is_answered


def test_rejecting_makes_the_item_unanswered_again(
    a_request: InformationRequest,
) -> None:
    with platform_scope(reason="test"):
        send_request(a_request)
        item = a_request.items.get(kind=RequestItem.Kind.CONFIRMATION)
        record_response(item, value="YES")
        assert item.is_answered

        reject_response(item, reason="You said yes but the ledger shows none.")
        item.refresh_from_db()
        a_request.refresh_from_db()

    assert not item.is_answered
    assert a_request.state == RequestState.CLARIFICATION_NEEDED


def test_a_rejection_needs_a_reason(a_request: InformationRequest) -> None:
    with platform_scope(reason="test"):
        item = a_request.items.first()
        assert item is not None
        with pytest.raises(RequestError):
            reject_response(item, reason="   ")


def test_answering_again_clears_the_rejection(a_request: InformationRequest) -> None:
    with platform_scope(reason="test"):
        send_request(a_request)
        item = a_request.items.get(kind=RequestItem.Kind.CONFIRMATION)
        record_response(item, value="YES")
        reject_response(item, reason="Check the ledger.")
        record_response(item, value="NO")
        item.refresh_from_db()

    assert item.rejection_reason == ""
    assert item.is_answered


def test_closing_records_what_was_outstanding(a_request: InformationRequest) -> None:
    """Closing an incomplete request is legitimate, and the gap is recorded."""
    with platform_scope(reason="test"):
        send_request(a_request)
        close_request(a_request, note="Got the figures by phone.")
        a_request.refresh_from_db()
        event = a_request.events.filter(kind="STATE_CHANGED").first()

    assert a_request.state == RequestState.CLOSED
    assert event is not None
    assert event.context["outstanding"], "the outstanding items were not recorded"


def test_a_closed_request_cannot_be_answered(a_request: InformationRequest) -> None:
    with platform_scope(reason="test"):
        send_request(a_request)
        close_request(a_request)
        item = a_request.items.get(kind=RequestItem.Kind.CONFIRMATION)
        with pytest.raises(RequestError):
            record_response(item, value="YES")


# ===========================================================================
# Reminders
# ===========================================================================


def test_reminders_escalate_rather_than_drip(a_request: InformationRequest) -> None:
    """One day before, on the day, then every third day afterwards.

    A constant drip becomes background noise; a client who is late should hear
    about it more often, not at the same rate.
    """
    with platform_scope(reason="test"):
        send_request(a_request)
        due = a_request.due_on
        assert due is not None

        assert a_request in due_for_reminder(as_of=due - timedelta(days=1))
        assert a_request in due_for_reminder(as_of=due)
        assert a_request not in due_for_reminder(as_of=due - timedelta(days=4))
        assert a_request in due_for_reminder(as_of=due + timedelta(days=3))
        assert a_request not in due_for_reminder(as_of=due + timedelta(days=4))


# ===========================================================================
# Over HTTP
# ===========================================================================


def test_the_list_renders_both_ways(signed_in: Client, a_request: InformationRequest) -> None:
    page = signed_in.get(reverse("rfi:list"))
    fragment = signed_in.get(reverse("rfi:list"), headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert a_request.title.encode() in fragment.content


def test_the_list_has_a_bounded_query_count(
    signed_in: Client,
    a_request: InformationRequest,
    org: Tenant,
    entity_a: Entity,
    django_assert_max_num_queries: object,
) -> None:
    """Progress is read from prefetched items, not counted per row."""
    with platform_scope(reason="test"):
        for index in range(20):
            extra = InformationRequest.objects.create(
                tenant=org, entity=entity_a, title=f"Request {index}"
            )
            RequestItem.objects.create(
                tenant=org, entity=entity_a, request=extra, label="Something"
            )

    signed_in.get(reverse("rfi:list"), {"status": "all"})

    with django_assert_max_num_queries(14):  # type: ignore[operator]
        response = signed_in.get(reverse("rfi:list"), {"status": "all"})
    assert response.status_code == 200


def test_creating_a_request_splits_the_lines_into_items(
    signed_in: Client, entity_a: Entity, org_owner: User
) -> None:
    response = signed_in.post(
        reverse("rfi:create"),
        {
            "entity": str(entity_a.pk),
            "title": "October working papers",
            "items_text": "Sales register\nPurchase register\nAny exempt supplies?",
            "priority": "NORMAL",
            "assigned_to": str(org_owner.pk),
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200, response.content[:400]

    with platform_scope(reason="test"):
        created = InformationRequest.objects.get(title="October working papers")
        labels = list(created.items.order_by("ordinal").values_list("label", "kind"))

    assert labels == [
        ("Sales register", "DOCUMENT"),
        ("Purchase register", "DOCUMENT"),
        # A trailing question mark means a question, not a document.
        ("Any exempt supplies?", "CONFIRMATION"),
    ]


def test_detail_renders_and_accepts_an_answer(
    signed_in: Client, a_request: InformationRequest
) -> None:
    with platform_scope(reason="test"):
        send_request(a_request)
        item = a_request.items.get(kind=RequestItem.Kind.CONFIRMATION)

    page = signed_in.get(reverse("rfi:detail", args=[a_request.pk]))
    assert page.status_code == 200

    response = signed_in.post(
        reverse("rfi:item_respond", args=[a_request.pk, item.pk]),
        {"value": "NO"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert b"request-panel" in response.content

    with platform_scope(reason="test"):
        item.refresh_from_db()
    assert item.response_value == "NO"
