"""
Answering a request from outside the tenant, without opening a door.

A request can be addressed to an email rather than to a user, because a practice
deals constantly with a bookkeeper at a client who will never have an account.
That was modelled and then abandoned: ``services.notify`` returns early for such
a recipient, deferring to a delivery path that never existed. The address was a
dead end — the person was told nothing and could answer nothing.

Closing it means adding an unauthenticated way to write to tenant data, which is
the kind of feature that goes wrong quietly. So the tests below are mostly about
what the link *cannot* do:

* it names one request, and reaches nothing else — not the sibling requests of
  the same entity, not the vault, not the calendar;
* it goes through the same scope and the same Row-Level Security policy as an
  employee, rather than around them;
* expired, revoked and forged are indistinguishable from outside;
* the raw token is never stored, so a database dump is not a set of live links.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from stacos.accounts.models import User
from stacos.core.models import AuditLog
from stacos.core.scope import platform_scope
from stacos.requests.models import (
    InformationRequest,
    RequestItem,
    RequestState,
    ResponderToken,
)
from stacos.requests.services import issue_responder_token, send_request
from stacos.tenancy.models import Entity, Tenant
from stacos.vault.models import Document
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

TODAY = date(2026, 8, 12)
OUTSIDER = "bookkeeper@client.example"


def _external_request(
    org: Tenant, entity: Entity, requester: User, *, title: str = "September papers"
) -> InformationRequest:
    """A sent request addressed to an email, not to a user."""
    with platform_scope(reason="test-fixture"):
        information_request = InformationRequest.objects.create(
            tenant=org,
            entity=entity,
            title=title,
            due_on=TODAY + timedelta(days=5),
            assigned_email=OUTSIDER,
            requested_by=requester,
        )
        RequestItem.objects.create(
            tenant=org,
            entity=entity,
            request=information_request,
            label="Turnover for the quarter",
            kind=RequestItem.Kind.DATA,
            ordinal=0,
            is_mandatory=True,
        )
        RequestItem.objects.create(
            tenant=org,
            entity=entity,
            request=information_request,
            label="Bank statement",
            kind=RequestItem.Kind.DOCUMENT,
            ordinal=1,
            is_mandatory=True,
        )
        send_request(information_request, actor=requester)
        return information_request


@pytest.fixture
def external(org: Tenant, entity_a: Entity, org_owner: User) -> InformationRequest:
    return _external_request(org, entity_a, org_owner)


@pytest.fixture
def link(external: InformationRequest, org_owner: User) -> tuple[ResponderToken, str]:
    with platform_scope(reason="test-fixture"):
        return issue_responder_token(external, actor=org_owner)


# ---------------------------------------------------------------------------
# The dead end closes
# ---------------------------------------------------------------------------


def test_the_firm_can_send_a_link_and_the_outsider_can_answer(
    client: Client, org_owner: User, external: InformationRequest
) -> None:
    """End to end, because each half passing alone is what let this stay broken."""
    signed_in = sign_in(client, org_owner, step_up=True)

    sent = signed_in.post(
        reverse("rfi:invite_responder", args=[external.pk]), headers={"HX-Request": "true"}
    )
    assert sent.status_code == 200
    assert mail.outbox, "no email went to the outside contact"
    assert mail.outbox[-1].to == [OUTSIDER]

    with platform_scope(reason="test"):
        token = ResponderToken.objects.get(request=external)

    # The link, as it arrives in the email.
    body = mail.outbox[-1].body
    assert "/respond/" in body
    raw = body.split("/respond/")[1].split("/")[0]

    visitor = Client()
    page = visitor.get(reverse("respond:responder", kwargs={"token": raw}))
    assert page.status_code == 200
    assert external.title in page.content.decode()

    with platform_scope(reason="test"):
        item = external.items.order_by("ordinal").first()
        assert item is not None

    answered = visitor.post(
        reverse("respond:responder_item", kwargs={"token": raw, "item_pk": item.pk}),
        {"value": "42,00,000"},
    )
    assert answered.status_code == 200

    with platform_scope(reason="test"):
        item.refresh_from_db()
        assert item.response_value == "42,00,000"
        token.refresh_from_db()
        assert token.use_count >= 1


def test_the_outsider_can_upload_a_file(
    client: Client, link: tuple[ResponderToken, str], external: InformationRequest
) -> None:
    _token, raw = link
    with platform_scope(reason="test"):
        document_item = external.items.order_by("ordinal").last()
        assert document_item is not None

    visitor = Client()
    response = visitor.post(
        reverse("respond:responder_upload", kwargs={"token": raw, "item_pk": document_item.pk}),
        {"files": SimpleUploadedFile("bank.pdf", b"statement", content_type="application/pdf")},
    )

    assert response.status_code == 200
    with platform_scope(reason="test"):
        document_item.refresh_from_db()
        assert document_item.document_count == 1
        assert Document.objects.filter(title="bank.pdf").exists()


def test_answering_everything_completes_the_request(
    link: tuple[ResponderToken, str], external: InformationRequest
) -> None:
    """The derivation does not care who answered."""
    _token, raw = link
    visitor = Client()

    with platform_scope(reason="test"):
        data_item, document_item = list(external.items.order_by("ordinal"))

    visitor.post(
        reverse("respond:responder_item", kwargs={"token": raw, "item_pk": data_item.pk}),
        {"value": "42,00,000"},
    )
    visitor.post(
        reverse("respond:responder_upload", kwargs={"token": raw, "item_pk": document_item.pk}),
        {"files": SimpleUploadedFile("bank.pdf", b"statement", content_type="application/pdf")},
    )

    with platform_scope(reason="test"):
        external.refresh_from_db()

    assert external.state == RequestState.ANSWERED


def test_the_outsiders_answer_is_audited_under_their_address(
    link: tuple[ResponderToken, str], external: InformationRequest
) -> None:
    """There is no ``User`` row, so ``actor`` is null — but "who supplied the
    bank statement the filing rests on" still has to have an answer."""
    _token, raw = link
    with platform_scope(reason="test"):
        item = external.items.order_by("ordinal").first()
        assert item is not None

    Client().post(
        reverse("respond:responder_item", kwargs={"token": raw, "item_pk": item.pk}),
        {"value": "42,00,000"},
    )

    with platform_scope(reason="test"):
        entries = AuditLog.objects.filter(
            object_type="rfi.InformationRequest", object_id=str(external.pk)
        )
        assert any(OUTSIDER in (entry.actor_label or "") for entry in entries), (
            "the audit trail does not say who answered"
        )


# ---------------------------------------------------------------------------
# What the link cannot do
# ---------------------------------------------------------------------------


def test_the_link_reaches_no_other_request_of_the_same_entity(
    link: tuple[ResponderToken, str], org: Tenant, entity_a: Entity, org_owner: User
) -> None:
    """The scope is narrowed to the entity, so a sibling request is *readable* by
    the scope — which is exactly why the view checks the item belongs to this
    token's request as well."""
    _token, raw = link
    sibling = _external_request(org, entity_a, org_owner, title="A different ask")

    with platform_scope(reason="test"):
        other_item = sibling.items.order_by("ordinal").first()
        assert other_item is not None

    response = Client().post(
        reverse("respond:responder_item", kwargs={"token": raw, "item_pk": other_item.pk}),
        {"value": "forged"},
    )

    assert response.status_code == 404
    with platform_scope(reason="test"):
        other_item.refresh_from_db()
        assert other_item.response_value == ""


def test_the_link_reaches_no_other_tenant(
    link: tuple[ResponderToken, str], other_org: Tenant, rival_entity: Entity, rival_owner: User
) -> None:
    """The Row-Level Security policy, not just the view's own check."""
    _token, raw = link
    rival_request = _external_request(other_org, rival_entity, rival_owner, title="Rival ask")

    with platform_scope(reason="test"):
        rival_item = rival_request.items.order_by("ordinal").first()
        assert rival_item is not None

    response = Client().post(
        reverse("respond:responder_item", kwargs={"token": raw, "item_pk": rival_item.pk}),
        {"value": "forged"},
    )

    assert response.status_code == 404


def test_the_link_grants_only_three_permissions(link: tuple[ResponderToken, str]) -> None:
    """Asserted on the scope, not on a status code.

    Every test above would still pass if the holder were handed every permission
    in the registry, because none of them tries the thing that would then work.
    """
    from stacos.requests.services import responder_scope

    token, _raw = link
    with platform_scope(reason="test"):
        row = ResponderToken.objects.select_related("request").get(pk=token.pk)

    with responder_scope(row) as scope:
        assert scope.permissions == frozenset(
            {"rfi.request.view", "rfi.request.respond", "vault.document.upload"}
        )
        assert scope.entity_ids == frozenset({row.request.entity_id})
        assert scope.readable_tenant_ids == frozenset({row.request.tenant_id})


def test_the_app_itself_stays_shut_to_the_link_holder(
    link: tuple[ResponderToken, str], external: InformationRequest
) -> None:
    """Visiting the link does not create a session, so nothing under /app/ opens."""
    _token, raw = link
    visitor = Client()
    visitor.get(reverse("respond:responder", kwargs={"token": raw}))

    for path in ("/app/", "/app/requests/", "/app/documents/"):
        response = visitor.get(path)
        assert response.status_code in {302, 404}, f"{path} was reachable"
        if response.status_code == 302:
            assert "/auth/login" in response["Location"], f"{path} did not require signing in"


# ---------------------------------------------------------------------------
# The token itself
# ---------------------------------------------------------------------------


def test_the_raw_token_is_never_stored(link: tuple[ResponderToken, str]) -> None:
    """A database dump must not be a set of working links."""
    token, raw = link

    with platform_scope(reason="test"):
        token.refresh_from_db()

    assert token.token_hash != raw
    assert raw not in token.token_hash
    assert len(token.token_hash) == 64


def test_an_expired_link_is_refused(link: tuple[ResponderToken, str]) -> None:
    token, raw = link
    with platform_scope(reason="test"):
        ResponderToken.objects.filter(pk=token.pk).update(
            expires_at=timezone.now() - timedelta(days=1)
        )

    assert Client().get(reverse("respond:responder", kwargs={"token": raw})).status_code == 404


def test_a_revoked_link_is_refused(link: tuple[ResponderToken, str]) -> None:
    token, raw = link
    with platform_scope(reason="test"):
        ResponderToken.objects.filter(pk=token.pk).update(
            status=ResponderToken.Status.REVOKED, revoked_at=timezone.now()
        )

    assert Client().get(reverse("respond:responder", kwargs={"token": raw})).status_code == 404


def test_a_forged_link_is_refused_the_same_way(link: tuple[ResponderToken, str]) -> None:
    """Expired, revoked and never-existed are one answer. Distinguishing them
    tells a guesser which links used to be real."""
    response = Client().get(
        reverse("respond:responder", kwargs={"token": "not-a-real-token-at-all"})
    )

    assert response.status_code == 404


def test_reissuing_revokes_the_previous_link(
    external: InformationRequest, org_owner: User
) -> None:
    """Otherwise "send it again" quietly leaves two working keys in two inboxes."""
    with platform_scope(reason="test"):
        _first, first_raw = issue_responder_token(external, actor=org_owner)
        _second, second_raw = issue_responder_token(external, actor=org_owner)

    visitor = Client()
    assert visitor.get(reverse("respond:responder", kwargs={"token": first_raw})).status_code == 404
    assert (
        visitor.get(reverse("respond:responder", kwargs={"token": second_raw})).status_code == 200
    )


def test_issuing_a_link_is_audited(external: InformationRequest, org_owner: User) -> None:
    with platform_scope(reason="test"):
        issue_responder_token(external, actor=org_owner)

        entries = AuditLog.objects.filter(
            object_type="rfi.InformationRequest", object_id=str(external.pk)
        )
        assert any(OUTSIDER in str(entry.after or {}) for entry in entries), (
            "minting a bearer credential left no audit row"
        )
