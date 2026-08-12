"""
The vault: deduplication, linking, scanning and the download gate.

The download view is the only route by which bytes leave the system, so most of
this file is about it.
"""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.models import AuditAction, AuditLog
from stacos.core.scope import platform_scope
from stacos.tenancy.models import Entity, Tenant
from stacos.vault.models import Document, DocumentDownload, DocumentLink, LinkTarget
from stacos.vault.services import attach, detach, documents_for, record_download, replace, store
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db


def _upload(
    name: str = "statement.pdf", content: bytes = b"bank statement bytes"
) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="application/pdf")


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    """Signed in with step-up fresh.

    The tracker's sensitive actions — recording a response, closing a notice —
    demand re-authentication, so a fixture without it would test the redirect
    rather than the action.
    """
    return sign_in(client, org_owner, step_up=True)


# ===========================================================================
# Deduplication
# ===========================================================================


def test_the_same_bytes_are_stored_once(org: Tenant, entity_a: Entity) -> None:
    """One file, two links — not two files.

    A client answering three requests with one bank statement should store it
    once. That is not only storage economy: it is what makes "where else does
    this document appear" answerable at all.
    """
    with platform_scope(reason="test"):
        first, created_first = store(tenant=org, entity=entity_a, upload=_upload(), actor=None)
        second, created_second = store(tenant=org, entity=entity_a, upload=_upload(), actor=None)

    assert created_first
    assert not created_second
    assert first.pk == second.pk


def test_different_bytes_are_different_documents(org: Tenant, entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        first, _ = store(tenant=org, entity=entity_a, upload=_upload(content=b"one"))
        second, _ = store(tenant=org, entity=entity_a, upload=_upload(content=b"two"))

    assert first.pk != second.pk
    assert first.content_hash != second.content_hash


def test_the_same_bytes_in_two_tenants_are_two_documents(
    org: Tenant, other_org: Tenant, entity_a: Entity, rival_entity: Entity
) -> None:
    """Deduplication stops at the tenant boundary, deliberately.

    Sharing one row between two customers would mean one customer's archival or
    export decision reaching into another's vault.
    """
    with platform_scope(reason="test"):
        mine, _ = store(tenant=org, entity=entity_a, upload=_upload())
        theirs, _ = store(tenant=other_org, entity=rival_entity, upload=_upload())

    assert mine.pk != theirs.pk
    assert mine.content_hash == theirs.content_hash


# ===========================================================================
# Linking
# ===========================================================================


def test_a_document_can_be_attached_to_several_things(org: Tenant, entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        document, _ = store(tenant=org, entity=entity_a, upload=_upload())
        attach(document, target_type=LinkTarget.OBLIGATION, target_id=entity_a.pk)
        attach(document, target_type=LinkTarget.NOTICE, target_id=entity_a.pk)

        assert DocumentLink.objects.filter(document=document).count() == 2


def test_attaching_twice_is_idempotent(org: Tenant, entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        document, _ = store(tenant=org, entity=entity_a, upload=_upload())
        attach(document, target_type=LinkTarget.OBLIGATION, target_id=entity_a.pk)
        attach(document, target_type=LinkTarget.OBLIGATION, target_id=entity_a.pk)

        assert DocumentLink.objects.filter(document=document).count() == 1


def test_detaching_leaves_the_document(org: Tenant, entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        document, _ = store(tenant=org, entity=entity_a, upload=_upload())
        link = attach(document, target_type=LinkTarget.OBLIGATION, target_id=entity_a.pk)
        detach(link)

        assert not DocumentLink.objects.filter(pk=link.pk).exists()
        assert Document.objects.filter(pk=document.pk).exists()


def test_documents_for_ignores_archived(org: Tenant, entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        document, _ = store(tenant=org, entity=entity_a, upload=_upload())
        attach(document, target_type=LinkTarget.OBLIGATION, target_id=entity_a.pk)
        assert len(documents_for(target_type=LinkTarget.OBLIGATION, target_id=entity_a.pk)) == 1

        document.archive(reason="Superseded")
        assert documents_for(target_type=LinkTarget.OBLIGATION, target_id=entity_a.pk) == []


# ===========================================================================
# Versions
# ===========================================================================


def test_replacing_a_document_keeps_the_old_bytes(org: Tenant, entity_a: Entity) -> None:
    """ "The version that was filed" is a different question from "what we have now"."""
    with platform_scope(reason="test"):
        document, _ = store(tenant=org, entity=entity_a, upload=_upload(content=b"first draft"))
        original_hash = document.content_hash

        version = replace(document, upload=_upload(content=b"final"), reason="Corrected figures")
        document.refresh_from_db()

    assert version.content_hash == original_hash
    assert document.content_hash != original_hash
    assert version.reason == "Corrected figures"
    # A replaced file has to be re-scanned; the old clearance said nothing about
    # the new bytes.
    assert not document.is_scanned


# ===========================================================================
# The download gate
# ===========================================================================


def test_an_unscanned_document_cannot_be_downloaded(
    signed_in: Client, org: Tenant, entity_a: Entity
) -> None:
    """A compliance product that distributes malware has one incident and no customers."""
    with platform_scope(reason="test"):
        document, _ = store(tenant=org, entity=entity_a, upload=_upload())

    response = signed_in.get(reverse("vault:download", args=[document.pk]))
    assert response.status_code == 409


def test_a_scanned_document_downloads_and_is_logged(
    signed_in: Client, org: Tenant, entity_a: Entity
) -> None:
    with platform_scope(reason="test"):
        document, _ = store(tenant=org, entity=entity_a, upload=_upload())
        document.is_scanned = True
        document.save(update_fields=["is_scanned"])

    response = signed_in.get(reverse("vault:download", args=[document.pk]))
    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"

    with platform_scope(reason="test"):
        assert DocumentDownload.objects.filter(document=document).count() == 1
        assert AuditLog.objects.filter(
            action=AuditAction.DOWNLOAD, object_id=str(document.pk)
        ).exists()


def test_downloading_records_who_and_when(org: Tenant, entity_a: Entity, org_owner: User) -> None:
    with platform_scope(reason="test"):
        document, _ = store(tenant=org, entity=entity_a, upload=_upload())
        record_download(document, actor=org_owner, ip_address="203.0.113.9")

        entry = DocumentDownload.objects.get(document=document)
        assert entry.downloaded_by_id == org_owner.pk
        assert entry.ip_address == "203.0.113.9"
        assert entry.actor_label


# ===========================================================================
# Upload validation
# ===========================================================================


def test_an_executable_is_refused(signed_in: Client, entity_a: Entity) -> None:
    """An allowlist, because the set of dangerous extensions grows and the set of
    things a client sends an accountant does not."""
    response = signed_in.post(
        reverse("vault:upload"),
        {
            "entity": str(entity_a.pk),
            "kind": "EVIDENCE",
            "classification": "GENERAL",
            "files": SimpleUploadedFile("payload.exe", b"MZ", content_type="application/exe"),
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    assert b"not a file type we accept" in response.content


def test_uploading_attaches_when_a_target_is_given(signed_in: Client, entity_a: Entity) -> None:
    response = signed_in.post(
        reverse("vault:upload"),
        {
            "entity": str(entity_a.pk),
            "kind": "EVIDENCE",
            "classification": "GENERAL",
            "target_type": LinkTarget.OBLIGATION,
            "target_id": str(entity_a.pk),
            "files": _upload(),
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200

    with platform_scope(reason="test"):
        assert (
            DocumentLink.objects.filter(
                target_type=LinkTarget.OBLIGATION, target_id=entity_a.pk
            ).count()
            == 1
        )


def test_the_list_renders_both_ways(signed_in: Client, org: Tenant, entity_a: Entity) -> None:
    with platform_scope(reason="test"):
        store(tenant=org, entity=entity_a, upload=_upload())

    page = signed_in.get(reverse("vault:list"))
    fragment = signed_in.get(reverse("vault:list"), headers={"HX-Request": "true"})

    assert page.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b"statement.pdf" in fragment.content or b"Documents" in fragment.content
