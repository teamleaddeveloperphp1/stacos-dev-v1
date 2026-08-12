"""
The vault screens: a searchable list, an upload endpoint, and a download that
checks permission and logs.

The download view is the one that matters. It is the only route by which bytes
leave the system, so it is the only place that has to be right about permission,
about scanning, and about recording who took a copy.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q, QuerySet
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Toast, is_fragment_request, oob
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.vault.forms import UploadForm
from stacos.vault.models import Document, DocumentKind, LinkTarget
from stacos.vault.services import attach, documents_for, record_download, store


def _filtered(request: HttpRequest) -> QuerySet[Document]:
    queryset = Document.objects.filter(archived_at__isnull=True).select_related(
        "entity", "uploaded_by"
    )

    search = request.GET.get("q", "").strip()
    if search:
        queryset = queryset.filter(
            Q(title__icontains=search)
            | Q(original_filename__icontains=search)
            | Q(tags__contains=[search])
            # Only reached when OCR has run. Cheap to include and it is the
            # query a user actually wants: "which document mentions this number".
            | Q(extracted_text__icontains=search)
        )

    kind = request.GET.get("kind", "").strip()
    if kind:
        queryset = queryset.filter(kind=kind)

    entity_id = request.GET.get("entity", "").strip()
    if entity_id:
        queryset = queryset.filter(entity_id=entity_id)

    return queryset.order_by("-created_at")


@require_permission("vault.document.view")
def document_list(request: HttpRequest) -> HttpResponse:
    rows = list(_filtered(request)[:100])
    context = {
        "documents": rows,
        "search": request.GET.get("q", ""),
        "kind": request.GET.get("kind", ""),
        "kinds": DocumentKind.choices,
    }
    template = (
        "vault/_fragments/list_body.html" if is_fragment_request(request) else "vault/list.html"
    )
    return render(request, template, context)


@require_permission("vault.document.view")
def document_detail(request: HttpRequest, pk: str) -> HttpResponse:
    document = _get(pk)
    return render(
        request,
        "vault/_fragments/document_detail.html",
        {
            "document": document,
            "downloads": document.downloads.select_related("downloaded_by")[:20],
            "links": document.links.all()[:20],
            "versions": document.versions.all()[:10],
        },
    )


def _get(pk: str) -> Document:
    document = (
        Document.objects.filter(pk=pk, archived_at__isnull=True)
        .select_related("entity", "uploaded_by")
        .first()
    )
    if document is None:
        # 404 rather than 403: confirming a document exists in another tenant is
        # itself a disclosure, and for a vault that is the whole game.
        raise Http404
    return document


@require_permission("vault.document.upload")
@require_http_methods(["GET", "POST"])
def document_upload(request: HttpRequest) -> HttpResponse:
    """Upload one or more files, optionally attaching them to something.

    The target is taken from the query string so the same endpoint serves an
    information request item, an obligation and the plain vault — one upload
    path, one place where deduplication and scanning are decided.
    """
    target_type = request.GET.get("target_type", "") or request.POST.get("target_type", "")
    target_id = request.GET.get("target_id", "") or request.POST.get("target_id", "")

    form = UploadForm(request.POST or None, request.FILES or None)

    if request.method == "POST" and form.is_valid():
        entity = form.cleaned_data["entity"]
        stored: list[Document] = []
        duplicates = 0

        for upload in form.cleaned_data["files"]:
            document, created = store(
                tenant=entity.tenant,
                entity=entity,
                upload=upload,
                title=form.cleaned_data.get("title") or upload.name,
                kind=form.cleaned_data["kind"],
                classification=form.cleaned_data["classification"],
                period_key=form.cleaned_data.get("period_key", ""),
                actor=current_user(request),
            )
            stored.append(document)
            duplicates += 0 if created else 1

            if target_type and target_id:
                attach(
                    document,
                    target_type=target_type,
                    target_id=target_id,
                    evidence_key=form.cleaned_data.get("evidence_key", ""),
                    actor=current_user(request),
                )

        message = _("%(count)d file(s) stored.") % {"count": len(stored)}
        if duplicates:
            # Said out loud rather than hidden: a user who uploads the same file
            # twice should understand that it was recognised, not silently eaten.
            message += " " + _("%(dupes)d already in the vault and linked again.") % {
                "dupes": duplicates
            }

        return oob(
            request,
            "",
            toast=Toast(message),
            triggers={
                "stacos:modal-close": True,
                "stacos:documents-changed": {"target": target_id},
            },
        )

    return render(
        request,
        "vault/_fragments/upload_modal.html",
        {"form": form, "target_type": target_type, "target_id": target_id},
        status=422 if request.method == "POST" else 200,
    )


@require_permission("vault.document.download")
def document_download(request: HttpRequest, pk: str) -> HttpResponseBase:
    """Hand over the bytes, once, to somebody entitled to them.

    Streamed from storage rather than redirected to a signed URL. A signed URL
    leaves the platform unable to say whether the bytes were actually fetched,
    and a shared URL outlives the permission that produced it.
    """
    document = _get(pk)

    if not document.is_downloadable:
        # Refusing an unscanned file is not paranoia: a practice and its clients
        # exchange files through this product, and one distributed malware
        # incident ends it.
        #
        # 409 for "not yet" and 403 for "never": the first invites a retry and
        # the second forecloses it, and a client polling a quarantined file
        # forever is a support call nobody needs.
        return HttpResponse(
            document.download_refusal,
            status=403 if document.is_quarantined else 409,
            content_type="text/plain; charset=utf-8",
        )

    record_download(
        document,
        actor=current_user(request),
        ip_address=request.META.get("REMOTE_ADDR"),
    )

    response = FileResponse(
        document.file.open("rb"),
        as_attachment=True,
        filename=document.original_filename or document.title,
    )
    # The vault is not cacheable by anything in the middle. A shared proxy
    # holding a client's bank statement is exactly the outcome to avoid.
    response["Cache-Control"] = "private, no-store"
    return response


@require_permission("vault.document.view")
def attachments(request: HttpRequest) -> HttpResponse:
    """The attachment strip for anything that owns documents.

    One endpoint rather than one per owning module: every caller wants the same
    list with the same upload button, and duplicating that per module is how
    four slightly different attachment widgets appear.
    """
    target_type = request.GET.get("target_type", "")
    target_id = request.GET.get("target_id", "")
    if target_type not in LinkTarget.values or not target_id:
        raise Http404

    return render(
        request,
        "vault/_fragments/attachments.html",
        {
            "documents": documents_for(target_type=target_type, target_id=target_id),
            "target_type": target_type,
            "target_id": target_id,
            "can_upload": "vault.document.upload" in _permissions(request),
        },
    )


def _permissions(request: HttpRequest) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


@require_permission("vault.document.delete")
@require_http_methods(["POST"])
def document_archive(request: HttpRequest, pk: str) -> HttpResponse:
    """Archive a document. Never destroys the bytes."""
    document = _get(pk)
    document.archive(reason=request.POST.get("reason", ""))

    from stacos.core.audit import record_event
    from stacos.core.models import AuditAction

    record_event(action=AuditAction.ARCHIVE, actor=current_user(request), obj=document)

    return oob(
        request,
        main="",
        toast=Toast(_("Document archived. The file is retained and remains in the audit trail.")),
    )


def context_for_target(target_type: str, target_id: Any) -> dict[str, Any]:
    """Helper for other modules rendering an attachment strip inline."""
    return {
        "documents": documents_for(target_type=target_type, target_id=target_id),
        "target_type": target_type,
        "target_id": target_id,
    }
