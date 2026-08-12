"""
The document vault.

Every other module produces or consumes documents — an information request
collects them, a notice arrives as one, a return is filed with them attached —
so the vault is deliberately generic about *what* a document belongs to and
strict about everything else.

Three decisions carry weight:

**Documents are content-addressed.** The SHA-256 of the bytes is stored and
uniquely constrained per tenant. A client who uploads the same bank statement to
four information requests stores it once and links it four times. That is not
just storage economy: it is what makes "where else does this document appear"
answerable, which is the question an assessment actually asks.

**Links are a separate table, not a foreign key.** A document belongs to as many
things as it belongs to. Modelling it as ``document.obligation_id`` forces a copy
per attachment point and loses the relationship the moment somebody re-uploads.

**Downloads are logged individually.** The audit log records that a document was
downloaded; this is a compliance product and "who saw this, when" is a question
that gets asked years later about a specific file.
"""

from __future__ import annotations

import hashlib
from typing import ClassVar

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.models import SoftDeleteModel, TenantScopedModel

__all__ = [
    "Document",
    "DocumentDownload",
    "DocumentKind",
    "DocumentLink",
    "DocumentVersion",
    "LinkTarget",
]


class DocumentKind(models.TextChoices):
    """What a document *is*, for filtering and for retention rules."""

    EVIDENCE = "EVIDENCE", _("Evidence for a filing")
    RETURN = "RETURN", _("Filed return or acknowledgement")
    CHALLAN = "CHALLAN", _("Tax payment challan")
    CERTIFICATE = "CERTIFICATE", _("Certificate or licence")
    NOTICE = "NOTICE", _("Notice from an authority")
    CORRESPONDENCE = "CORRESPONDENCE", _("Letter or email")
    FINANCIAL = "FINANCIAL", _("Financial statement or ledger")
    AGREEMENT = "AGREEMENT", _("Contract or agreement")
    IDENTITY = "IDENTITY", _("Identity or registration document")
    MINUTES = "MINUTES", _("Meeting minutes or resolution")
    OTHER = "OTHER", _("Other")


class ScanState(models.TextChoices):
    """Where a file is in the scanning pipeline.

    Three of these are terminal and one is not, and the distinction is the whole
    point of not using a boolean. ``ERROR`` means the engine could not be
    reached: the file is neither safe nor condemned, it is unknown, and the sweep
    task will ask again. Folding that into "not scanned" makes an engine outage
    indistinguishable from a queue backlog; folding it into "infected" quarantines
    a firm's real documents because a daemon restarted.
    """

    PENDING = "PENDING", _("Waiting to be scanned")
    CLEAN = "CLEAN", _("Scanned, no threat found")
    INFECTED = "INFECTED", _("Threat found — quarantined")
    ERROR = "ERROR", _("Scanner unavailable")


class OcrState(models.TextChoices):
    """Whether text has been pulled out of a file, and whether it is worth trying.

    ``SKIPPED`` exists so a spreadsheet is not re-queued nightly forever by a
    sweep looking for documents with no extracted text.
    """

    PENDING = "PENDING", _("Not yet processed")
    DONE = "DONE", _("Text extracted")
    EMPTY = "EMPTY", _("Processed, no text found")
    SKIPPED = "SKIPPED", _("Not a file text can be read from")


class LinkTarget(models.TextChoices):
    """What a document can be attached to.

    A string rather than a ``GenericForeignKey``: the target set is small, closed
    and known, and a content-type join on every list render is a cost with no
    corresponding benefit.
    """

    OBLIGATION = "OBLIGATION", _("Obligation")
    REQUEST_ITEM = "REQUEST_ITEM", _("Information request item")
    NOTICE = "NOTICE", _("Notice")
    RETURN = "RETURN", _("Return preparation")
    MEETING = "MEETING", _("Meeting")
    ENTITY = "ENTITY", _("Entity")
    INVOICE = "INVOICE", _("Invoice")


def upload_to(instance: Document, filename: str) -> str:
    """Partition by tenant and year.

    Tenant first so a bulk export or a deletion request is a prefix operation
    rather than a scan, and year second so a bucket lifecycle rule can archive
    cold objects without consulting the database.
    """
    stamp = timezone.now()
    return f"vault/{instance.tenant_id}/{stamp:%Y/%m}/{filename}"


class Document(TenantScopedModel, SoftDeleteModel):
    """One file, stored once, referenced from anywhere."""

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    class Source(models.TextChoices):
        UPLOAD = "UPLOAD", _("Uploaded by a user")
        EMAIL = "EMAIL", _("Received by email")
        PORTAL = "PORTAL", _("Fetched from a government portal")
        GENERATED = "GENERATED", _("Generated by STACOS")

    class Classification(models.TextChoices):
        """How carefully this has to be handled.

        Drives retention and who may download it. A PAN card and a board minute
        are both documents; treating them identically is how a data-protection
        finding happens.
        """

        GENERAL = "GENERAL", _("General")
        FINANCIAL = "FINANCIAL", _("Financial")
        PERSONAL = "PERSONAL", _("Contains personal data")
        PRIVILEGED = "PRIVILEGED", _("Legally privileged")

    entity = models.ForeignKey(
        "tenancy.Entity",
        on_delete=models.CASCADE,
        related_name="documents",
        null=True,
        blank=True,
        help_text=_("Null for tenant-level documents such as an engagement letter."),
    )

    title = models.CharField(max_length=250)
    kind = models.CharField(max_length=20, choices=DocumentKind.choices, db_index=True)
    classification = models.CharField(
        max_length=12, choices=Classification.choices, default=Classification.GENERAL
    )
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.UPLOAD)

    file = models.FileField(upload_to=upload_to, max_length=500)
    original_filename = models.CharField(max_length=250, blank=True)
    content_type = models.CharField(max_length=120, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    #: SHA-256 of the bytes. Unique per tenant, which is what makes the same file
    #: uploaded twice one row with two links rather than two rows.
    content_hash = models.CharField(max_length=64, db_index=True)

    #: Free-form, searchable. Deliberately not a taxonomy: every attempt to
    #: impose one on a document store is abandoned by the second month.
    tags = ArrayField(models.CharField(max_length=40), default=list, blank=True)
    note = models.TextField(blank=True)

    #: The period the document relates to, where that is meaningful. Lets "show
    #: me everything for FY 2025-26" work without a link to an obligation.
    period_key = models.CharField(max_length=32, blank=True, db_index=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    #: Set from the classification and the jurisdiction's retention rules. Null
    #: means "keep indefinitely", which is the safe default for evidence.
    retain_until = models.DateField(null=True, blank=True)

    #: Where this file is in the scanning pipeline. Downloads are refused until
    #: it reads CLEAN — a compliance product that distributes malware between a
    #: practice and its clients has one incident and no customers.
    scan_state = models.CharField(
        max_length=10, choices=ScanState.choices, default=ScanState.PENDING, db_index=True
    )
    #: The engine's own words: `clean:clamav`, `clamav:Win.Trojan.Agent`. Kept
    #: verbatim because "which signature" is the first question anybody asks
    #: about a quarantined file, and the second is "which engine said so".
    scan_result = models.CharField(max_length=120, blank=True)
    scanned_at = models.DateTimeField(null=True, blank=True)

    #: Populated by OCR where the file is an image or a scanned PDF. Indexed for
    #: full-text search rather than parsed: the useful query is "which document
    #: mentions this notice number", not "extract field 7".
    extracted_text = models.TextField(blank=True)
    ocr_state = models.CharField(
        max_length=10, choices=OcrState.choices, default=OcrState.PENDING, db_index=True
    )
    ocr_engine = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "content_hash"],
                condition=Q(archived_at__isnull=True),
                name="document_tenant_hash_uniq",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"], name="document_tenant_time_idx"),
            models.Index(fields=["entity", "kind"], name="document_entity_kind_idx"),
            GinIndex(fields=["tags"], name="document_tags_gin"),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def is_downloadable(self) -> bool:
        return self.scan_state == ScanState.CLEAN and not self.is_archived

    @property
    def is_quarantined(self) -> bool:
        return self.scan_state == ScanState.INFECTED

    @property
    def download_refusal(self) -> str:
        """Why the bytes are being withheld, in words a user can act on.

        A single "not available" covers three situations a user would respond to
        differently — wait, tell somebody, or stop trying — so each one says
        which it is.

        Resolved to a real string rather than left lazy: this is only ever called
        while rendering a response, so the active language is already bound, and
        the caller is sometimes an `HttpResponse` body rather than a template.
        """
        if self.is_downloadable:
            return ""
        if self.scan_state == ScanState.INFECTED:
            return str(
                _(
                    "This file was quarantined: the virus scanner found %(signature)s. "
                    "It cannot be downloaded. Ask whoever uploaded it to send a clean copy."
                )
                % {"signature": self.scan_result or _("a threat")}
            )
        if self.scan_state == ScanState.ERROR:
            return str(
                _(
                    "This file could not be scanned because the scanner was unavailable. "
                    "It will be retried automatically; downloads stay blocked until it passes."
                )
            )
        if self.is_archived:
            return str(_("This document has been archived."))
        return str(_("This file has not finished virus scanning yet. Try again shortly."))

    @staticmethod
    def hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


class DocumentVersion(TenantScopedModel):
    """A superseded version of a document.

    Append-only. Replacing a document keeps the old bytes, because "the version
    that was filed" and "the version we have now" are different questions and an
    assessment asks the first one.

    Tenant-scoped in its own right even though it is only ever reached through
    its parent. It holds the *bytes of a customer's document*; leaning on the
    parent's scoping would put those bytes outside Row-Level Security, and the
    second line of defence exists precisely for the query nobody thought about.
    """

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    file = models.FileField(upload_to="vault/versions/", max_length=500)
    content_hash = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField(default=0)
    replaced_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    replaced_at = models.DateTimeField(default=timezone.now)
    reason = models.CharField(max_length=250, blank=True)

    class Meta:
        ordering = ["document", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "version"], name="docversion_document_version_uniq"
            ),
        ]

    def __str__(self) -> str:
        return f"v{self.version} of {self.document_id}"


class DocumentLink(TenantScopedModel):
    """A document attached to something.

    Many-to-many by design. One bank statement can be evidence for a GST return,
    a response to an information request and an attachment to a notice reply, and
    it is the same file in all three places.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="links")
    entity = models.ForeignKey(
        "tenancy.Entity",
        on_delete=models.CASCADE,
        related_name="document_links",
        null=True,
        blank=True,
    )

    target_type = models.CharField(max_length=20, choices=LinkTarget.choices, db_index=True)
    target_id = models.UUIDField(db_index=True)
    #: The evidence requirement key this satisfies, where the target declares
    #: one. How "every mandatory evidence item is present" becomes checkable
    #: rather than a matter of opinion.
    evidence_key = models.CharField(max_length=60, blank=True)

    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["document", "target_type", "target_id", "evidence_key"],
                name="doclink_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["target_type", "target_id"], name="doclink_target_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.document_id} -> {self.target_type}:{self.target_id}"


class DocumentDownload(TenantScopedModel):
    """Who downloaded what, and when.

    Separate from the audit log because the question asked about a document is
    narrower and more frequent than the question asked about a tenant, and
    answering it should not mean scanning an append-only table that grows with
    every event in the product.
    """

    ENTITY_FIELD: ClassVar[str | None] = "entity_id"
    #: Written on behalf of the downloader, including from a read-only
    #: engagement — the record has to exist precisely when access is limited.
    ENFORCE_WRITE_SCOPE: ClassVar[bool] = False

    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="downloads")
    entity = models.ForeignKey(
        "tenancy.Entity",
        on_delete=models.CASCADE,
        related_name="document_downloads",
        null=True,
        blank=True,
    )
    downloaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    actor_label = models.CharField(max_length=200, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    downloaded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-downloaded_at"]
        indexes = [
            models.Index(fields=["document", "-downloaded_at"], name="docdownload_doc_time_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.actor_label} at {self.downloaded_at:%Y-%m-%d %H:%M}"
