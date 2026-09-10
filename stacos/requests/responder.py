"""
The screen an outside contact answers a request on.

Public views, in the precise sense that they carry no session and no membership.
Everything they can reach is decided by :func:`~stacos.requests.services.responder_scope`,
which narrows the ordinary tenant scope to one entity and three permissions — so
the holder of a link goes through the same scoped managers and the same
Row-Level Security policy as an employee of the firm, rather than around them.

Three things are true here that are worth stating plainly, because each one is a
way this kind of feature usually goes wrong:

* **The link is the credential, and it is never stored.** Only an HMAC of it is.
* **The link names one request.** Not an entity, not a tenant. Guessing another
  request's id gets nothing, because the scope is built from the token's own row.
* **Everything is audited under the address the link was sent to**, so "who
  uploaded this" has an answer that survives the link being forwarded.
"""

from __future__ import annotations

from typing import Any

import structlog
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, oob
from stacos.core.permissions import public_view
from stacos.core.request_context import bind_user_label
from stacos.requests.forms import ItemResponseForm
from stacos.requests.models import RequestItem
from stacos.requests.services import (
    RequestError,
    record_responder_use,
    record_response,
    resolve_responder_token,
    responder_scope,
)
from stacos.vault.models import Document, DocumentKind, LinkTarget
from stacos.vault.services import attach, documents_for, store

logger = structlog.get_logger(__name__)


def _token_or_404(raw: str) -> Any:
    token = resolve_responder_token(raw)
    if token is None:
        # One answer for expired, revoked and forged. Distinguishing them tells a
        # guesser which links used to exist.
        raise Http404
    return token


def _context(token: Any, raw: str) -> dict[str, Any]:
    information_request = token.request
    items = list(information_request.items.all())
    return {
        "token": token,
        # The raw token, so the forms on the page can post back to the address
        # the browser is already on. Held for the render and nowhere else — the
        # database has only its HMAC.
        "token_value": raw,
        "request_obj": information_request,
        "items": [
            {
                "item": item,
                "documents": documents_for(target_type=LinkTarget.REQUEST_ITEM, target_id=item.pk),
                "form": ItemResponseForm(item=item),
            }
            for item in items
        ],
    }


@public_view
def respond(request: HttpRequest, token: str) -> HttpResponse:
    """The checklist, as the outside contact sees it.

    Deliberately not the firm's detail screen: no timeline, no other requests, no
    entity page, no navigation into the application. What is on it is the list of
    things being asked for and a way to supply each one.
    """
    row = _token_or_404(token)
    with responder_scope(row):
        record_responder_use(row)
        bind_user_label(f"{row.email} (responder link)")
        return render(request, "requests/responder.html", _context(row, token))


@public_view
@require_http_methods(["POST"])
def respond_item(request: HttpRequest, token: str, item_pk: str) -> HttpResponse:
    """Answer one item through the link.

    The item is fetched through the *scoped* manager and then checked to belong
    to this token's request. Both, deliberately: the scope stops another tenant's
    row being reachable at all, and the second check stops a sibling request of
    the same entity being answered by a link that was not issued for it.
    """
    row = _token_or_404(token)
    with responder_scope(row):
        bind_user_label(f"{row.email} (responder link)")

        item = RequestItem.objects.filter(pk=item_pk).select_related("request").first()
        if item is None or item.request_id != row.request_id:
            raise Http404

        form = ItemResponseForm(request.POST, item=item)
        if not form.is_valid():
            return _panel(request, row, token, error=_("Check the answer."), status=422)

        try:
            record_response(item, value=form.cleaned_data.get("value", ""), actor=None)
        except RequestError as exc:
            return _panel(request, row, token, error=str(exc), status=422)

        logger.info(
            "rfi.responder_answered",
            request_id=str(row.request_id),
            item_id=str(item.pk),
            email=row.email,
        )
        return _panel(request, row, token, toast=Toast(_("Thank you — that has been sent.")))


@public_view
@require_http_methods(["POST"])
def upload_item(request: HttpRequest, token: str, item_pk: str) -> HttpResponse:
    """Attach a file to one item through the link.

    A separate endpoint from ``vault:document_upload`` rather than a reuse of it,
    and the difference is the point. That view lives under ``/app/``, needs a
    session, and takes its target from the query string — so it would either have
    to be opened up to anonymous callers or have a second authentication path
    bolted onto it, and both are how a bearer link turns into a way into the
    vault. This one can only ever write to the item the token names.
    """
    row = _token_or_404(token)
    with responder_scope(row):
        bind_user_label(f"{row.email} (responder link)")

        item = RequestItem.objects.filter(pk=item_pk).select_related("request").first()
        if item is None or item.request_id != row.request_id:
            raise Http404
        if item.kind != RequestItem.Kind.DOCUMENT:
            raise Http404

        uploads = request.FILES.getlist("files")
        if not uploads:
            return _panel(request, row, token, error=_("Choose a file first."), status=422)

        information_request = row.request
        for upload in uploads:
            document, _created = store(
                tenant=information_request.entity.tenant,
                entity=information_request.entity,
                upload=upload,
                title=upload.name,
                kind=DocumentKind.OTHER,
                classification=Document.Classification.GENERAL,
                actor=None,
            )
            attach(
                document,
                target_type=LinkTarget.REQUEST_ITEM,
                target_id=item.pk,
                actor=None,
            )

        logger.info(
            "rfi.responder_uploaded",
            request_id=str(row.request_id),
            item_id=str(item.pk),
            email=row.email,
            files=len(uploads),
        )
        return _panel(request, row, token, toast=Toast(_("Thank you — that has been received.")))


def _panel(
    request: HttpRequest,
    token: Any,
    raw: str,
    *,
    toast: Toast | None = None,
    error: str = "",
    status: int = 200,
) -> HttpResponse:
    context = _context(token, raw)
    context["error"] = error

    if status != 200:
        return render(request, "requests/_fragments/responder_body.html", context, status=status)

    # A Fragment, not the template name: `oob` treats a bare string as rendered
    # HTML and would send the path to the browser as the page.
    return oob(
        request,
        Fragment("requests/_fragments/responder_body.html", context),
        toast=toast,
    )
