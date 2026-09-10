"""
Storing, linking and handing back documents.

The interesting behaviour is deduplication. Uploading the same bytes twice
produces one row and two links, which is what makes "where else does this
appear" answerable and stops a client's fourth copy of the same bank statement
counting against their storage.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from stacos.core.audit import record_event
from stacos.core.models import AuditAction
from stacos.vault.models import (
    Document,
    DocumentDownload,
    DocumentKind,
    DocumentLink,
    DocumentVersion,
    LinkTarget,
    OcrState,
    ScanState,
)

logger = structlog.get_logger(__name__)

__all__ = ["attach", "detach", "documents_for", "record_download", "store"]


def _queue_scan(document: Document) -> None:
    """Hand the new bytes to the scanner, after the row is durable.

    ``on_commit`` rather than a direct call: a worker is fast enough to pick the
    message up before this transaction commits, and then it queries for a
    document that does not exist yet and gives up. That race is rare on a laptop
    and routine under load, which is the worst combination.

    Imported here rather than at module scope because `tasks` imports the models
    this module also imports; at module scope that is a cycle.
    """
    from stacos.vault.tasks import scan_document

    tenant_id = str(document.tenant_id)
    document_id = str(document.pk)
    transaction.on_commit(
        lambda: scan_document.apply_async(
            kwargs={"tenant_id": tenant_id, "document_id": document_id}
        )
    )


#: Read in chunks so a 200 MB scanned assessment order does not become 200 MB of
#: resident memory per concurrent upload.
_HASH_CHUNK = 1024 * 1024


def _hash_upload(upload: UploadedFile) -> str:
    import hashlib

    digest = hashlib.sha256()
    for chunk in upload.chunks(_HASH_CHUNK):
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


@transaction.atomic
def store(
    *,
    tenant: Any,
    entity: Any,
    upload: UploadedFile,
    title: str = "",
    kind: str = DocumentKind.OTHER,
    classification: str = Document.Classification.GENERAL,
    source: str = Document.Source.UPLOAD,
    period_key: str = "",
    tags: list[str] | None = None,
    note: str = "",
    actor: Any = None,
) -> tuple[Document, bool]:
    """Store an upload. Returns ``(document, created)``.

    ``created`` is ``False`` when the bytes were already in this tenant's vault,
    in which case the existing row is returned and the caller links to it. That
    is the normal case for a client answering three requests with one statement,
    and treating it as a duplicate-key error would be user-hostile.
    """
    content_hash = _hash_upload(upload)

    existing = Document.objects.filter(
        tenant=tenant, content_hash=content_hash, archived_at__isnull=True
    ).first()
    if existing is not None:
        logger.info("vault.deduplicated", document_id=str(existing.pk), hash=content_hash[:12])
        return existing, False

    document = Document(
        tenant=tenant,
        entity=entity,
        title=(title or upload.name or "Untitled")[:250],
        kind=kind,
        classification=classification,
        source=source,
        original_filename=(upload.name or "")[:250],
        content_type=(getattr(upload, "content_type", "") or "")[:120],
        size_bytes=upload.size or 0,
        content_hash=content_hash,
        period_key=period_key,
        tags=tags or [],
        note=note,
        uploaded_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    document.file = upload
    document.save()
    _queue_scan(document)

    record_event(
        action=AuditAction.CREATE,
        actor=actor,
        obj=document,
        after={"title": document.title, "kind": kind, "size_bytes": document.size_bytes},
    )
    return document, True


@transaction.atomic
def attach(
    document: Document,
    *,
    target_type: str,
    target_id: UUID | str,
    evidence_key: str = "",
    actor: Any = None,
) -> DocumentLink:
    """Link a document to something. Idempotent."""
    link, created = DocumentLink.objects.get_or_create(
        document=document,
        target_type=target_type,
        target_id=target_id,
        evidence_key=evidence_key,
        defaults={
            "tenant_id": document.tenant_id,
            "entity_id": document.entity_id,
            "linked_by": actor if getattr(actor, "is_authenticated", False) else None,
        },
    )
    if created:
        _sync_denormalised_counts(target_type, target_id, actor=actor)
    return link


@transaction.atomic
def detach(link: DocumentLink, *, actor: Any = None) -> None:
    """Remove a link. The document itself is untouched."""
    target_type, target_id = link.target_type, link.target_id
    link.delete()
    _sync_denormalised_counts(target_type, target_id, actor=actor)


def _sync_denormalised_counts(
    target_type: str,
    target_id: UUID | str,
    *,
    actor: Any = None,
) -> None:
    """Keep the caller's cached attachment count honest — and tell it what happened.

    A request item renders its document count on every list row; counting links
    per row would be an N+1 on the busiest screen in the practice. The cost of
    the denormalisation is this function, and it is called from exactly the two
    places that can change the answer.

    Updating the count was all this did, and that was the bug behind "the status
    does not auto-complete for file uploads". A typed answer and a yes/no both go
    through ``requests.services.record_response``, which stamps the item and
    re-derives the request's state; a file went through here, which wrote a
    number and returned. A request made entirely of document items could
    therefore have every file supplied and stay at SENT indefinitely.

    So it hands off rather than deciding anything: the requests module owns what
    an answer means, and this only says that the attachments changed.
    """
    if target_type != LinkTarget.REQUEST_ITEM:
        return

    from stacos.requests.models import RequestItem
    from stacos.requests.services import record_document_response

    count = DocumentLink.objects.filter(target_type=target_type, target_id=target_id).count()
    RequestItem.objects.filter(pk=target_id).update(document_count=count)

    item = RequestItem.objects.filter(pk=target_id).select_related("request").first()
    if item is not None:
        record_document_response(item, actor=actor)


def documents_for(*, target_type: str, target_id: UUID | str) -> list[Document]:
    """Every document attached to one thing, in one query."""
    links = (
        DocumentLink.objects.filter(target_type=target_type, target_id=target_id)
        .select_related("document")
        .order_by("-created_at")
    )
    return [link.document for link in links if link.document.archived_at is None]


def record_download(document: Document, *, actor: Any, ip_address: str | None = None) -> None:
    """Log a download against the file, and to the tenant audit trail.

    Both, deliberately. The per-document log answers "who has seen this" cheaply
    years later; the audit log answers "what did this user do" without joining
    every module's own history table.
    """
    label = ""
    if actor is not None and getattr(actor, "is_authenticated", False):
        label = (getattr(actor, "audit_label", None) or str(actor))[:200]

    DocumentDownload.objects.create(
        tenant_id=document.tenant_id,
        entity_id=document.entity_id,
        document=document,
        downloaded_by=actor if getattr(actor, "is_authenticated", False) else None,
        actor_label=label,
        ip_address=ip_address,
    )
    record_event(
        action=AuditAction.DOWNLOAD,
        actor=actor,
        obj=document,
        context={"content_hash": document.content_hash[:16]},
    )


@transaction.atomic
def replace(
    document: Document,
    *,
    upload: UploadedFile,
    reason: str = "",
    actor: Any = None,
) -> DocumentVersion:
    """Replace a document's file, keeping the old bytes as a version.

    "The version that was filed" and "the version we have now" are different
    questions, and an assessment asks the first one.
    """
    next_version = document.versions.count() + 1
    archived = DocumentVersion.objects.create(
        tenant_id=document.tenant_id,
        document=document,
        version=next_version,
        file=document.file,
        content_hash=document.content_hash,
        size_bytes=document.size_bytes,
        replaced_by=actor if getattr(actor, "is_authenticated", False) else None,
        reason=reason,
    )

    document.file = upload
    document.content_hash = _hash_upload(upload)
    document.size_bytes = upload.size or 0
    document.original_filename = (upload.name or "")[:250]
    # New bytes are unscanned bytes. Carrying the old verdict forward would let
    # anyone with replace rights launder a file past the scanner, which is the
    # obvious way to attack a pipeline like this one.
    document.scan_state = ScanState.PENDING
    document.scan_result = ""
    document.scanned_at = None
    document.extracted_text = ""
    document.ocr_state = OcrState.PENDING
    document.save(
        update_fields=[
            "file",
            "content_hash",
            "size_bytes",
            "original_filename",
            "scan_state",
            "scan_result",
            "scanned_at",
            "extracted_text",
            "ocr_state",
            "updated_at",
        ]
    )
    _queue_scan(document)

    record_event(
        action=AuditAction.UPDATE,
        actor=actor,
        obj=document,
        before={"content_hash": archived.content_hash[:16]},
        after={"content_hash": document.content_hash[:16]},
        context={"reason": reason, "version": next_version},
    )
    return archived
