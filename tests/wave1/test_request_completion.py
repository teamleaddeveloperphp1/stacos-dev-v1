"""
A request completes itself, whatever shape the answers arrive in.

The state is derived from the items — that much was already true and already
tested. What was not true is that every kind of answer reached the derivation.

A typed answer and a yes/no both go through ``record_response``, which stamps the
item and calls ``refresh_state``. A **file** arrived through the vault, which
updated ``document_count`` and returned. So the same request completed itself or
did not depending on what it happened to be asking for, and one made entirely of
document items could have every file supplied and sit at ``SENT`` for ever.

Three more things are asserted here, each of which was missing rather than wrong:

* every action reaches the tamper-proof audit trail, not just the module's own
  timeline — including the transition the product makes on its own, which is the
  one nobody can attest to from memory;
* the person who raised the request is told when it is done, once, and not
  before;
* one tenant cannot see, answer, reject or close another tenant's request — as a
  **404**, because a 403 would confirm the row exists.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.models import AuditLog
from stacos.core.scope import platform_scope
from stacos.notifications.models import Notification, NotificationKind
from stacos.requests.models import (
    InformationRequest,
    RequestEvent,
    RequestItem,
    RequestState,
)
from stacos.requests.services import record_response, reject_response, send_request
from stacos.tenancy.models import Entity, Tenant
from stacos.vault.models import LinkTarget
from stacos.vault.services import attach, detach, store
from stacos.vault.models import DocumentLink
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

TODAY = date(2026, 8, 12)
HTMX = {"HX-Request": "true"}


def _make_request(
    org: Tenant,
    entity: Entity,
    requester: User,
    kinds: list[str],
) -> InformationRequest:
    """A sent request whose items are all mandatory and of the given kinds."""
    with platform_scope(reason="test-fixture"):
        information_request = InformationRequest.objects.create(
            tenant=org,
            entity=entity,
            title="September working papers",
            due_on=TODAY + timedelta(days=5),
            assigned_to=requester,
            requested_by=requester,
        )
        for ordinal, kind in enumerate(kinds):
            RequestItem.objects.create(
                tenant=org,
                entity=entity,
                request=information_request,
                label=f"Item {ordinal + 1}",
                kind=kind,
                ordinal=ordinal,
                is_mandatory=True,
            )
        send_request(information_request, actor=requester)
        return information_request


def _upload_to(item: RequestItem, entity: Entity, name: str = "statement.pdf") -> None:
    """Attach a file with content unique to its name.

    The vault is content-addressed and deduplicates: two uploads with identical
    bytes are one document and the second `attach` is a no-op, which is correct
    and would make "they sent a replacement" test nothing at all.
    """
    with platform_scope(reason="test"):
        document, _ = store(
            tenant=entity.tenant,
            entity=entity,
            upload=SimpleUploadedFile(
                name, f"bytes of {name}".encode(), content_type="application/pdf"
            ),
            title=name,
        )
        attach(document, target_type=LinkTarget.REQUEST_ITEM, target_id=item.pk)


# ===========================================================================
# Auto-completion, by every answer type
# ===========================================================================


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (RequestItem.Kind.DATA, "1,42,000"),
        (RequestItem.Kind.CONFIRMATION, "YES"),
    ],
)
def test_a_typed_or_yes_no_answer_completes_the_request(
    org: Tenant, entity_a: Entity, org_owner: User, kind: str, value: str
) -> None:
    information_request = _make_request(org, entity_a, org_owner, [kind])

    with platform_scope(reason="test"):
        item = information_request.items.get()
        record_response(item, value=value, actor=org_owner)
        information_request.refresh_from_db()

    assert information_request.state == RequestState.ANSWERED
    assert information_request.answered_at is not None


def test_a_file_completes_the_request_too(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """The bug. A document item was answered by the vault, which never told the
    requests module — so the request stayed at SENT with every file supplied."""
    information_request = _make_request(org, entity_a, org_owner, [RequestItem.Kind.DOCUMENT])

    with platform_scope(reason="test"):
        item = information_request.items.get()
    _upload_to(item, entity_a)

    with platform_scope(reason="test"):
        information_request.refresh_from_db()
        item.refresh_from_db()

    assert information_request.state == RequestState.ANSWERED, (
        "an all-documents request never completed itself"
    )
    assert item.responded_at is not None, "the item was never stamped as answered"


def test_a_mixture_of_all_three_completes_only_on_the_last_one(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """The case the parametrised tests above cannot catch: the paths agreeing.

    Each kind completing a single-item request proves each path works. This
    proves they count towards the same total.
    """
    information_request = _make_request(
        org,
        entity_a,
        org_owner,
        [RequestItem.Kind.DATA, RequestItem.Kind.CONFIRMATION, RequestItem.Kind.DOCUMENT],
    )

    with platform_scope(reason="test"):
        data, confirmation, document = list(information_request.items.order_by("ordinal"))

        record_response(data, value="1,42,000", actor=org_owner)
        information_request.refresh_from_db()
        assert information_request.state == RequestState.PARTIALLY_ANSWERED

        record_response(confirmation, value="YES", actor=org_owner)
        information_request.refresh_from_db()
        assert information_request.state == RequestState.PARTIALLY_ANSWERED

    _upload_to(document, entity_a)

    with platform_scope(reason="test"):
        information_request.refresh_from_db()

    assert information_request.state == RequestState.ANSWERED


def test_removing_the_only_file_reopens_the_request(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """The same operation in reverse.

    A request that stayed "answered" after its evidence was detached would be
    read as done by the next person to look at it.
    """
    information_request = _make_request(org, entity_a, org_owner, [RequestItem.Kind.DOCUMENT])
    with platform_scope(reason="test"):
        item = information_request.items.get()
    _upload_to(item, entity_a)

    with platform_scope(reason="test"):
        information_request.refresh_from_db()
        assert information_request.state == RequestState.ANSWERED

        link = DocumentLink.objects.get(
            target_type=LinkTarget.REQUEST_ITEM, target_id=item.pk
        )
        detach(link)
        information_request.refresh_from_db()

    assert information_request.state == RequestState.SENT


def test_a_replacement_file_clears_a_rejection(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """``is_answered`` is false while a rejection stands, and only the response
    paths cleared it — so a document item, once sent back, could never be
    answered again however many files arrived."""
    information_request = _make_request(org, entity_a, org_owner, [RequestItem.Kind.DOCUMENT])
    with platform_scope(reason="test"):
        item = information_request.items.get()
    _upload_to(item, entity_a, name="wrong.pdf")

    with platform_scope(reason="test"):
        item.refresh_from_db()
        reject_response(item, reason="That is the wrong month.", actor=org_owner)
        item.refresh_from_db()
        assert not item.is_answered

    _upload_to(item, entity_a, name="right.pdf")

    with platform_scope(reason="test"):
        item.refresh_from_db()
        information_request.refresh_from_db()

    assert item.rejection_reason == ""
    assert information_request.state == RequestState.ANSWERED


# ===========================================================================
# The audit trail
# ===========================================================================


def _audit_for(information_request: InformationRequest) -> list[AuditLog]:
    return list(
        AuditLog.objects.filter(
            object_type="rfi.InformationRequest", object_id=str(information_request.pk)
        )
    )


def test_every_action_reaches_the_tamper_proof_trail(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """`RequestEvent` is the timeline this module draws; `AuditLog` is the record
    a business shows a regulator, and a database trigger denies UPDATE and DELETE
    on it. Responding and rejecting reached only the first."""
    information_request = _make_request(
        org, entity_a, org_owner, [RequestItem.Kind.DATA, RequestItem.Kind.DATA]
    )

    with platform_scope(reason="test"):
        first, second = list(information_request.items.order_by("ordinal"))
        record_response(first, value="one", actor=org_owner)
        reject_response(first, reason="Wrong period.", actor=org_owner)
        record_response(first, value="one again", actor=org_owner)
        record_response(second, value="two", actor=org_owner)

        entries = _audit_for(information_request)

    actions = [str(entry.after or {}) for entry in entries]
    assert any("responded" in action for action in actions), "an answer left no audit row"
    assert any("rejected" in action for action in actions), "a rejection left no audit row"
    assert any(RequestState.ANSWERED in action for action in actions), (
        "the automatic transition left no audit row — the one nobody can attest to"
    )


def test_the_derived_transition_is_marked_as_derived(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """So a reader can tell what the product decided from what a person did."""
    information_request = _make_request(org, entity_a, org_owner, [RequestItem.Kind.DATA])

    with platform_scope(reason="test"):
        record_response(information_request.items.get(), value="x", actor=org_owner)
        entries = _audit_for(information_request)

    assert any((entry.context or {}).get("derived") == "true" for entry in entries)


def test_the_module_timeline_still_records_everything(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """The audit trail is additional to the timeline, not a replacement for it."""
    information_request = _make_request(org, entity_a, org_owner, [RequestItem.Kind.DOCUMENT])
    with platform_scope(reason="test"):
        item = information_request.items.get()
    _upload_to(item, entity_a)

    with platform_scope(reason="test"):
        kinds = set(information_request.events.values_list("kind", flat=True))

    assert RequestEvent.Kind.RESPONDED in kinds, "a file arriving left no timeline entry"
    assert RequestEvent.Kind.STATE_CHANGED in kinds


# ===========================================================================
# Telling the person who asked
# ===========================================================================


def _answered_notifications(information_request: InformationRequest) -> list[Notification]:
    return list(
        Notification.objects.filter(
            kind=NotificationKind.REQUEST_ANSWERED, subject_id=information_request.pk
        )
    )


def test_the_requester_is_told_when_it_is_finished(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    information_request = _make_request(org, entity_a, org_owner, [RequestItem.Kind.DATA])

    with platform_scope(reason="test"):
        record_response(information_request.items.get(), value="x", actor=org_owner)
        notifications = _answered_notifications(information_request)

    assert len(notifications) == 1
    assert notifications[0].recipient_id == org_owner.pk


def test_a_partial_answer_does_not_notify(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """Two items, one answered. Nobody has finished anything."""
    information_request = _make_request(
        org, entity_a, org_owner, [RequestItem.Kind.DATA, RequestItem.Kind.DATA]
    )

    with platform_scope(reason="test"):
        first = information_request.items.order_by("ordinal").first()
        assert first is not None
        record_response(first, value="x", actor=org_owner)

        assert _answered_notifications(information_request) == []


def test_it_notifies_once_per_request_not_once_per_answer(
    org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """Rejecting and re-answering walks back through ANSWERED a second time.

    The dedupe key is what stops that becoming a second message about the same
    fact.
    """
    information_request = _make_request(org, entity_a, org_owner, [RequestItem.Kind.DATA])

    with platform_scope(reason="test"):
        item = information_request.items.get()
        record_response(item, value="x", actor=org_owner)
        reject_response(item, reason="Not that one.", actor=org_owner)
        record_response(item, value="y", actor=org_owner)

        assert len(_answered_notifications(information_request)) == 1


# ===========================================================================
# Isolation — 404, not 403
# ===========================================================================


@pytest.fixture
def a_rival_request(org: Tenant, entity_a: Entity, org_owner: User) -> InformationRequest:
    return _make_request(
        org, entity_a, org_owner, [RequestItem.Kind.DATA, RequestItem.Kind.DOCUMENT]
    )


@pytest.fixture
def hostile(client: Client, rival_owner: User) -> Client:
    """Signed in as a completely unrelated customer."""
    return sign_in(client, rival_owner, step_up=True)


def test_another_tenant_cannot_view_the_request(
    hostile: Client, a_rival_request: InformationRequest
) -> None:
    response = hostile.get(reverse("rfi:detail", args=[a_rival_request.pk]))

    assert response.status_code == 404, (
        "403 would confirm the request exists; 404 says nothing either way"
    )


def test_another_tenant_cannot_answer_an_item(
    hostile: Client, a_rival_request: InformationRequest
) -> None:
    with platform_scope(reason="test"):
        item = a_rival_request.items.order_by("ordinal").first()
        assert item is not None

    response = hostile.post(
        reverse("rfi:item_respond", args=[a_rival_request.pk, item.pk]),
        {"value": "forged"},
        headers=HTMX,
    )

    assert response.status_code == 404
    with platform_scope(reason="test"):
        item.refresh_from_db()
        assert item.response_value == "", "a foreign tenant wrote an answer"


def test_another_tenant_cannot_reject_an_item(
    hostile: Client, a_rival_request: InformationRequest
) -> None:
    with platform_scope(reason="test"):
        item = a_rival_request.items.order_by("ordinal").first()
        assert item is not None

    response = hostile.post(
        reverse("rfi:item_reject", args=[a_rival_request.pk, item.pk]),
        {"reason": "no"},
        headers=HTMX,
    )

    assert response.status_code == 404
    with platform_scope(reason="test"):
        item.refresh_from_db()
        assert item.rejection_reason == ""


def test_another_tenant_cannot_close_the_request(
    hostile: Client, a_rival_request: InformationRequest
) -> None:
    response = hostile.post(reverse("rfi:close", args=[a_rival_request.pk]), headers=HTMX)

    assert response.status_code == 404
    with platform_scope(reason="test"):
        a_rival_request.refresh_from_db()
        assert a_rival_request.state != RequestState.CLOSED


def test_another_tenant_cannot_issue_a_responder_link(
    hostile: Client, a_rival_request: InformationRequest
) -> None:
    """A link is a bearer credential. Minting one for somebody else's request
    would be the most valuable thing on this list to get wrong."""
    response = hostile.post(
        reverse("rfi:invite_responder", args=[a_rival_request.pk]), headers=HTMX
    )

    assert response.status_code == 404
