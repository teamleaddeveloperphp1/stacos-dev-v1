"""
The pipeline that runs behind an upload.

An upload returns as soon as the bytes are stored, because a user waiting thirty
seconds for OCR will stop using the feature. Everything after that happens here:

    store() → scan_document → [clean] → extract_text

Scanning gates the download and OCR does not, which is why they are separate
tasks on separate queues. A hundred-page scan occupying the OCR pool for four
minutes must not delay the scan that is standing between a partner and a file
they need now.

**Both are recovered by a sweep rather than trusted to the broker.** `acks_late`
guarantees at-least-once, which is the wrong end of the problem: the failure that
actually strands a document is a message that was never enqueued (a crash between
commit and publish) or one that failed its retries. A document stuck at PENDING is
invisible — it looks like a slow queue — so `sweep_pending` goes looking for them.
That is the difference between a pipeline and a pipeline that works.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from stacos.core.scope import platform_scope
from stacos.core.tasks import TenantTask
from stacos.vault.models import Document, OcrState, ScanState
from stacos.vault.ocr import get_extractor
from stacos.vault.scanning import ScanError, get_scanner

logger = structlog.get_logger(__name__)

__all__ = ["extract_text", "scan_document", "sweep_pending"]

#: Narrow on purpose. These tasks read a file and write two fields; they have no
#: business doing anything else, and a task's permissions are the blast radius of
#: a bug in it.
_TASK_PERMISSIONS = ("vault.document.view",)

#: How long a document may sit unscanned before the sweep treats it as stranded.
#: Long enough that a genuinely busy queue is not fought over, short enough that
#: nobody spends a working day unable to open a file.
STRANDED_AFTER = timedelta(minutes=20)


#: Attempts before a document is left in ERROR for the sweep to pick up. Five
#: attempts over roughly fifteen minutes covers a daemon restart or a deploy;
#: past that the engine is genuinely down and hammering it helps nobody.
MAX_SCAN_ATTEMPTS = 5


@shared_task(
    base=TenantTask,
    name="stacos.vault.scan_document",
    task_permissions=_TASK_PERMISSIONS,
)
def scan_document(*, tenant_id: str, document_id: str, attempt: int = 1) -> dict[str, Any]:
    """Scan one document and record the verdict.

    **The retry is an explicit re-enqueue, not `self.retry()`, and that is not a
    stylistic choice.** ``TenantTask`` runs every task body inside
    ``transaction.atomic``. Any exception leaving this function — Celery's own
    ``Retry`` included — rolls the transaction back, which would discard the very
    ERROR row written to explain the delay. The state would stay PENDING through
    all five attempts and a user asking "why can't I download this" would get a
    spinner and no answer.

    So a scanner outage returns normally, letting the ERROR commit, and schedules
    the next attempt as a fresh message. "Infected" is not retried at all: it is
    an answer, and retrying it scans a known-bad file four more times.
    """
    document = Document.objects.filter(pk=document_id, archived_at__isnull=True).first()
    if document is None:
        # Archived or deleted between enqueue and execution. Nothing to do, and
        # not an error: the bytes are already unreachable.
        return {"status": "gone", "document_id": document_id}

    scanner = get_scanner()

    try:
        with document.file.open("rb") as handle:
            verdict = scanner.scan(handle)
    except ScanError as exc:
        _record(document, state=ScanState.ERROR, result=f"error:{scanner.name}"[:120])
        logger.warning(
            "vault.scan.unavailable", document_id=document_id, attempt=attempt, error=str(exc)
        )

        if attempt >= MAX_SCAN_ATTEMPTS:
            # Left in ERROR deliberately. `sweep_pending` re-queues it every ten
            # minutes, so recovery happens when the engine comes back rather than
            # requiring somebody to notice.
            return {"status": "error", "document_id": document_id, "attempts": attempt}

        delay = min(30 * 2**attempt, 900)
        _enqueue_after_commit(
            scan_document,
            {"tenant_id": str(tenant_id), "document_id": str(document_id), "attempt": attempt + 1},
            countdown=delay,
        )
        return {"status": "retrying", "document_id": document_id, "in_seconds": delay}
    except FileNotFoundError:
        # Storage lost the object. Retrying will not conjure it back.
        _record(document, state=ScanState.ERROR, result="error:file-missing")
        logger.error("vault.scan.file_missing", document_id=document_id)
        return {"status": "file_missing", "document_id": document_id}

    state = ScanState.CLEAN if verdict.clean else ScanState.INFECTED
    _record(document, state=state, result=verdict.as_result())

    if not verdict.clean:
        logger.warning(
            "vault.scan.infected",
            document_id=document_id,
            signature=verdict.signature,
            engine=verdict.engine,
        )
        return {"status": "infected", "signature": verdict.signature}

    # Text extraction only for files that passed. Reading a known-bad file with
    # a document parser is the second thing that goes wrong after distributing
    # it, and parsers are historically where the exploits land.
    #
    # After commit, or a fast worker reads the document while it is still PENDING
    # in its own snapshot, declines to extract, and the text never appears.
    _enqueue_after_commit(
        extract_text, {"tenant_id": str(tenant_id), "document_id": str(document_id)}
    )
    return {"status": "clean", "document_id": document_id}


def _enqueue_after_commit(task: Any, kwargs: dict[str, Any], **options: Any) -> None:
    transaction.on_commit(lambda: task.apply_async(kwargs=kwargs, **options))


def _record(document: Document, *, state: str, result: str) -> None:
    document.scan_state = state
    document.scan_result = result
    document.scanned_at = timezone.now()
    document.save(update_fields=["scan_state", "scan_result", "scanned_at", "updated_at"])


@shared_task(
    base=TenantTask,
    name="stacos.vault.ocr_extract_text",
    task_permissions=_TASK_PERMISSIONS,
)
def extract_text(
    *,
    tenant_id: str,
    document_id: str,
) -> dict[str, Any]:
    """Pull searchable text out of a document.

    Never raises for a file it could not read. An unparseable PDF is a normal
    event in a product whose inputs come from government portals, and a task that
    fails on it retries three times and then sits in a dead-letter queue that
    nobody reads.
    """
    document = Document.objects.filter(pk=document_id, archived_at__isnull=True).first()
    if document is None:
        return {"status": "gone", "document_id": document_id}
    if document.scan_state != ScanState.CLEAN:
        # Ordering, not paranoia: this task can only be reached via a clean scan,
        # but a retry could arrive after the document was replaced.
        return {"status": "not_clean", "document_id": document_id}

    extractor = get_extractor()
    with document.file.open("rb") as handle:
        result = extractor.extract(
            handle,
            content_type=document.content_type,
            filename=document.original_filename or document.title,
        )

    if not result.attempted:
        state = OcrState.SKIPPED
    elif result.has_text:
        state = OcrState.DONE
    else:
        state = OcrState.EMPTY

    document.extracted_text = result.text
    document.ocr_state = state
    document.ocr_engine = result.engine[:20]
    document.save(update_fields=["extracted_text", "ocr_state", "ocr_engine", "updated_at"])

    return {"status": state, "characters": len(result.text), "engine": result.engine}


@shared_task(name="stacos.vault.sweep_pending", ignore_result=True)
def sweep_pending(limit: int = 500) -> dict[str, int]:
    """Re-enqueue documents the pipeline lost.

    Platform-wide, so it takes no ``tenant_id`` and opens an audited
    ``platform_scope`` — then dispatches per-tenant tasks that each re-derive
    their own scope. The sweep sees across tenants; the work does not.
    """
    cutoff = timezone.now() - STRANDED_AFTER

    with platform_scope(reason="vault:sweep-pending"):
        stranded = list(
            Document.objects.filter(
                archived_at__isnull=True,
                scan_state__in=[ScanState.PENDING, ScanState.ERROR],
                created_at__lt=cutoff,
            )
            .order_by("created_at")
            .values_list("pk", "tenant_id")[:limit]
        )

        # Clean documents whose text extraction never ran. Separate query because
        # the two failures are independent: OCR falling over does not block a
        # download, so it is swept without urgency.
        unextracted = list(
            Document.objects.filter(
                archived_at__isnull=True,
                scan_state=ScanState.CLEAN,
                ocr_state=OcrState.PENDING,
                scanned_at__lt=cutoff,
            )
            .order_by("scanned_at")
            .values_list("pk", "tenant_id")[:limit]
        )

    for pk, tenant_id in stranded:
        scan_document.apply_async(kwargs={"tenant_id": str(tenant_id), "document_id": str(pk)})
    for pk, tenant_id in unextracted:
        extract_text.apply_async(kwargs={"tenant_id": str(tenant_id), "document_id": str(pk)})

    if stranded or unextracted:
        logger.info("vault.sweep", rescanned=len(stranded), reextracted=len(unextracted))

    return {"rescanned": len(stranded), "reextracted": len(unextracted)}
