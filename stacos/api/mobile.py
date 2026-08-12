"""
What the mobile client can do.

The app is not a small copy of the web application. It is for the four things
people genuinely do away from a desk, and building only those is what keeps it
worth maintaining:

* **See what is due.** The calendar, filtered to what is urgent.
* **Photograph evidence.** A challan stamped at a bank counter, captured before
  it is lost. This is the single feature that justifies the app existing.
* **Answer a request.** A client on a phone responding to "send us the August
  bank statement".
* **Move something on.** Marking a filing done, with its acknowledgement number.

Every mutation calls the same service function the web view calls. There is no
mobile-specific business logic in this file and there must never be: the moment
"filed" means something slightly different over JSON, the audit trail is fiction.

Permissions are declared as ``permission_classes`` built from the same codes the
web views use — see `stacos.api.permissions`. `manage.py check_view_permissions`
walks the URL resolver and insists a DRF view sets that attribute in its own
class body, so "someone decided" is provable here exactly as it is there.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from uuid import UUID

import structlog
from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response

from stacos.api.permissions import is_sensitive, requires
from stacos.api.scoping import ScopedAPIView
from stacos.api.serializers import (
    DocumentSerializer,
    EvidenceUploadSerializer,
    ItemResponseSerializer,
    NotificationSerializer,
    ObligationDetailSerializer,
    ObligationSerializer,
    RequestDetailSerializer,
    RequestSerializer,
    TransitionSerializer,
)
from stacos.core.typing import current_user
from stacos.engine.lifecycle import transition_for
from stacos.notifications.models import Notification
from stacos.obligations.models import ObligationInstance
from stacos.obligations.queries import live
from stacos.obligations.transitions import TransitionError, apply_transition
from stacos.requests.models import InformationRequest, RequestItem
from stacos.requests.services import RequestError, record_response
from stacos.vault.models import DocumentKind, LinkTarget
from stacos.vault.services import attach, documents_for, store

logger = structlog.get_logger(__name__)

#: A phone screen shows a handful of rows. Fifty is a generous page and a bound
#: on what a client on a train has to download over a patchy connection.
PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

#: The default calendar window. Far enough ahead to plan a week, far enough back
#: that anything overdue is still in view — which is the whole reason somebody
#: opens the app.
DEFAULT_LOOKBACK = 60
DEFAULT_HORIZON = 45


def _permissions(request: Request) -> frozenset[str]:
    scope = getattr(request, "access_scope", None)
    return scope.permissions if scope is not None else frozenset()


def _paginate(queryset: Any, request: Request) -> tuple[list[Any], dict[str, Any]]:
    """Offset pagination, bounded, with an honest `has_more`.

    Keyset pagination is what the web list views use and what the database
    prefers. It is not used here because a mobile client pulls to refresh and
    jumps to "overdue", and an opaque cursor makes both awkward — while the
    bounded page size keeps the offset small enough that the usual objection
    does not bite.
    """
    try:
        limit = min(int(request.query_params.get("limit", PAGE_SIZE)), MAX_PAGE_SIZE)
        offset = max(int(request.query_params.get("offset", 0)), 0)
    except ValueError:
        limit, offset = PAGE_SIZE, 0

    window = list(queryset[offset : offset + limit + 1])
    return window[:limit], {
        "limit": limit,
        "offset": offset,
        "has_more": len(window) > limit,
    }


def _as_of(request: Request) -> date:
    """The day the client is asking about.

    Accepted from the client so a phone in a different timezone, or one whose
    clock has drifted, still agrees with the server about what "overdue" means —
    and rejected quietly back to today if it is nonsense.
    """
    raw = request.query_params.get("as_of", "")
    try:
        return date.fromisoformat(raw) if raw else timezone.localdate()
    except ValueError:
        return timezone.localdate()


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


class CalendarView(ScopedAPIView):
    """What is due, soonest first."""

    permission_classes = [requires("compliance.obligation.view")]

    @extend_schema(
        parameters=[
            OpenApiParameter("as_of", str, description="ISO date. Defaults to today."),
            OpenApiParameter("entity", str, description="Restrict to one entity."),
            OpenApiParameter(
                "status",
                str,
                description="`overdue`, `due_soon`, `open` (default) or `all`.",
            ),
            OpenApiParameter("days", int, description="How far ahead to look."),
            OpenApiParameter("limit", int),
            OpenApiParameter("offset", int),
        ],
        responses={200: ObligationSerializer(many=True)},
    )
    def get(self, request: Request) -> Response:
        as_of = _as_of(request)
        horizon = as_of + timedelta(days=_int(request, "days", DEFAULT_HORIZON))

        queryset = live(
            ObligationInstance.objects.filter(
                due_date__gte=as_of - timedelta(days=DEFAULT_LOOKBACK)
            )
        ).select_related("entity")

        wanted = request.query_params.get("status", "open")
        if wanted == "overdue":
            queryset = queryset.filter(due_date__lt=as_of)
        elif wanted == "due_soon":
            queryset = queryset.filter(due_date__gte=as_of, due_date__lte=as_of + timedelta(days=7))
        elif wanted != "all":
            queryset = queryset.filter(due_date__lte=horizon)

        entity = _uuid_or_none(request.query_params.get("entity", ""))
        if entity is not None:
            queryset = queryset.filter(entity_id=entity)

        if request.query_params.get("mine"):
            queryset = queryset.filter(assigned_to=current_user(request))

        rows, page = _paginate(queryset.order_by("due_date", "title"), request)
        return Response(
            {
                "as_of": as_of.isoformat(),
                "results": ObligationSerializer(rows, many=True, context={"as_of": as_of}).data,
                **page,
            }
        )


class ObligationDetailView(ScopedAPIView):
    permission_classes = [requires("compliance.obligation.view")]

    @extend_schema(responses={200: ObligationDetailSerializer})
    def get(self, request: Request, pk: UUID) -> Response:
        obligation = _obligation(pk)
        obligation.documents = documents_for(  # type: ignore[attr-defined]
            target_type=LinkTarget.OBLIGATION, target_id=obligation.pk
        )
        return Response(
            ObligationDetailSerializer(
                obligation,
                context={"as_of": _as_of(request), "permissions": _permissions(request)},
            ).data
        )


class ObligationTransitionView(ScopedAPIView):
    """Move an obligation on.

    The class-level permission is only the floor. There is deliberately no
    umbrella "may edit an obligation" code: the transition table declares a
    permission per move, and `apply_transition` enforces that one — because "can
    start preparing this" and "can certify that this was filed" are not the same
    claim, and a single code would collapse them.
    """

    permission_classes = [
        requires(
            "compliance.obligation.prepare",
            "compliance.obligation.review",
            "compliance.obligation.file",
            "compliance.obligation.close",
            "compliance.obligation.defer",
            any_of=True,
        )
    ]

    @extend_schema(
        request=TransitionSerializer,
        responses={
            200: ObligationDetailSerializer,
            409: OpenApiResponse(description="The obligation has already moved on."),
            422: OpenApiResponse(description="The move needs something it was not given."),
        },
    )
    def post(self, request: Request, pk: UUID) -> Response:
        serializer = TransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        obligation = _obligation(pk)
        target = serializer.validated_data["to_state"]

        # Step-up re-authentication has no mobile equivalent yet. Refusing the
        # sensitive moves — filing, approving, reopening — is the honest answer:
        # letting them through here would make the web-side step-up decorative,
        # and the whole point of it is that certifying a filing is a deliberate,
        # freshly-authenticated act.
        move = transition_for(obligation.state, target)
        if move is not None and is_sensitive(move.permission):
            return Response(
                {
                    "detail": (
                        "This action needs re-authentication, which the app cannot yet "
                        "do. Use the web application to complete it."
                    ),
                    "code": "step_up_required",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            result = apply_transition(
                obligation,
                target=target,
                actor=current_user(request),
                permissions=_permissions(request),
                note=serializer.validated_data.get("note", ""),
                filing_reference=serializer.validated_data.get("reference", ""),
            )
        except TransitionError as exc:
            # `stale` is a race — a colleague moved it first — and a client should
            # refetch rather than treat it as its own mistake. Everything else is
            # something the request itself got wrong.
            return Response(
                {"detail": str(exc), "code": exc.code},
                status=(
                    status.HTTP_409_CONFLICT
                    if exc.code == "stale"
                    else status.HTTP_422_UNPROCESSABLE_ENTITY
                ),
            )

        obligation = result.obligation
        obligation.documents = documents_for(  # type: ignore[attr-defined]
            target_type=LinkTarget.OBLIGATION, target_id=obligation.pk
        )
        return Response(
            ObligationDetailSerializer(
                obligation,
                context={"as_of": _as_of(request), "permissions": _permissions(request)},
            ).data
        )


class ObligationEvidenceView(ScopedAPIView):
    """Photograph a challan and attach it, in one request.

    The reason the app exists. Somebody standing at a bank counter with a stamped
    receipt has about thirty seconds of intent; anything that asks them to upload
    it later gets a receipt that is never uploaded.

    The response deliberately reports `scan_state`, because the file is *not*
    downloadable yet — the scanner has to pass it first — and a client that
    assumed otherwise would show a broken link.
    """

    permission_classes = [requires("vault.document.upload")]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request=EvidenceUploadSerializer,
        responses={201: DocumentSerializer},
    )
    def post(self, request: Request, pk: UUID) -> Response:
        serializer = EvidenceUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        obligation = _obligation(pk)

        document, created = store(
            tenant=obligation.tenant,
            entity=obligation.entity,
            upload=serializer.validated_data["file"],
            title=serializer.validated_data.get("title", ""),
            kind=serializer.validated_data.get("kind") or DocumentKind.EVIDENCE,
            period_key=obligation.period_key,
            note=serializer.validated_data.get("note", ""),
            actor=current_user(request),
        )
        attach(
            document,
            target_type=LinkTarget.OBLIGATION,
            target_id=obligation.pk,
            actor=current_user(request),
        )

        logger.info(
            "api.evidence_uploaded",
            obligation_id=str(obligation.pk),
            document_id=str(document.pk),
            deduplicated=not created,
        )
        return Response(
            DocumentSerializer(document).data,
            # 201 even when the bytes were already there: from the client's point
            # of view the attachment it asked for now exists, and reporting 200
            # would invite it to retry.
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Information requests
# ---------------------------------------------------------------------------


class RequestListView(ScopedAPIView):
    permission_classes = [requires("rfi.request.view")]

    @extend_schema(responses={200: RequestSerializer(many=True)})
    def get(self, request: Request) -> Response:
        queryset = (
            InformationRequest.objects.filter(archived_at__isnull=True)
            .select_related("entity")
            .prefetch_related("items")
            .order_by("due_on", "-created_at")
        )
        if request.query_params.get("mine"):
            queryset = queryset.filter(assigned_to=current_user(request))

        rows, page = _paginate(queryset, request)
        return Response({"results": RequestSerializer(rows, many=True).data, **page})


class RequestDetailView(ScopedAPIView):
    permission_classes = [requires("rfi.request.view")]

    @extend_schema(responses={200: RequestDetailSerializer})
    def get(self, request: Request, pk: UUID) -> Response:  # noqa: ARG002 - DRF handler signature
        information_request = (
            InformationRequest.objects.filter(pk=pk, archived_at__isnull=True)
            .select_related("entity")
            .prefetch_related("items")
            .first()
        )
        if information_request is None:
            raise Http404
        return Response(RequestDetailSerializer(information_request).data)


class RequestItemRespondView(ScopedAPIView):
    """Answer one item — a value, a file, or both."""

    permission_classes = [requires("rfi.request.respond")]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        request=ItemResponseSerializer,
        responses={200: RequestDetailSerializer, 422: OpenApiResponse(description="Refused.")},
    )
    def post(self, request: Request, pk: UUID) -> Response:
        serializer = ItemResponseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        item = (
            RequestItem.objects.filter(pk=pk).select_related("request", "request__entity").first()
        )
        if item is None:
            raise Http404

        upload = serializer.validated_data.get("file")
        if upload is not None:
            document, _created = store(
                tenant=item.tenant,
                entity=item.request.entity,
                upload=upload,
                title=item.label,
                kind=DocumentKind.EVIDENCE,
                actor=current_user(request),
            )
            attach(
                document,
                target_type=LinkTarget.REQUEST_ITEM,
                target_id=item.pk,
                evidence_key=item.label[:60],
                actor=current_user(request),
            )
            item.refresh_from_db()

        try:
            record_response(
                item,
                value=serializer.validated_data.get("value", ""),
                actor=current_user(request),
            )
        except RequestError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        parent = (
            InformationRequest.objects.filter(pk=item.request_id)
            .select_related("entity")
            .prefetch_related("items")
            .first()
        )
        return Response(RequestDetailSerializer(parent).data)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class NotificationListView(ScopedAPIView):
    """What the platform has told this user, for the app's own inbox."""

    permission_classes = [requires("notifications.view")]

    @extend_schema(responses={200: NotificationSerializer(many=True)})
    def get(self, request: Request) -> Response:
        queryset = Notification.objects.filter(recipient=current_user(request))
        if request.query_params.get("unread"):
            queryset = queryset.filter(read_at__isnull=True)

        rows, page = _paginate(queryset.order_by("-created_at"), request)
        return Response(
            {
                "unread": Notification.objects.filter(
                    recipient=current_user(request), read_at__isnull=True
                ).count(),
                "results": NotificationSerializer(rows, many=True).data,
                **page,
            }
        )


class NotificationReadView(ScopedAPIView):
    permission_classes = [requires("notifications.view")]

    @extend_schema(responses={204: OpenApiResponse(description="Marked as read.")})
    def post(self, request: Request, pk: UUID) -> Response:
        notification = Notification.objects.filter(pk=pk, recipient=current_user(request)).first()
        if notification is None:
            raise Http404
        notification.mark_read()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _obligation(pk: UUID) -> ObligationInstance:
    obligation = (
        ObligationInstance.objects.filter(pk=pk).select_related("entity", "assigned_to").first()
    )
    if obligation is None:
        # 404 rather than 403. Confirming that an obligation exists in another
        # tenant is itself a disclosure.
        raise Http404
    return obligation


def _int(request: Request, name: str, default: int) -> int:
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default


def _uuid_or_none(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        return None
