"""
Wire formats for the mobile client.

Read-only projections, deliberately. Nothing here writes: every mutation goes
through the same service function the web views call, so a rule cannot be
enforced in one client and forgotten in the other. A serializer that saves is how
a codebase ends up with two subtly different definitions of "filed".

They are also deliberately *flat*. A phone on a train renders a list; it does not
want a nested graph it has to walk, and every extra level is another join on the
server for a field nobody displays.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from stacos.engine.lifecycle import derive_display_status
from stacos.notifications.models import Notification
from stacos.obligations.models import ObligationInstance
from stacos.requests.models import InformationRequest, RequestItem
from stacos.vault.models import Document

__all__ = [
    "DocumentSerializer",
    "NotificationSerializer",
    "ObligationDetailSerializer",
    "ObligationSerializer",
    "RequestDetailSerializer",
    "RequestItemSerializer",
    "RequestSerializer",
]


class ObligationSerializer(serializers.ModelSerializer[ObligationInstance]):
    """One row in the calendar.

    ``display_status`` is computed rather than stored: "overdue" is a fact about
    today, not about the row, and a client that derived it locally from
    ``due_date`` would disagree with the server the moment a government extension
    moved the date.
    """

    entity_name = serializers.CharField(source="entity.name", read_only=True)
    display_status = serializers.SerializerMethodField()
    days_to_due = serializers.SerializerMethodField()

    class Meta:
        model = ObligationInstance
        fields = [
            "id",
            "title",
            "definition_code",
            "category",
            "entity",
            "entity_name",
            "period_key",
            "period_label",
            "due_date",
            "state",
            "display_status",
            "days_to_due",
            "assigned_to",
        ]
        read_only_fields = fields

    def get_display_status(self, obligation: ObligationInstance) -> str:
        return str(
            derive_display_status(
                state=obligation.state,
                due_date=obligation.due_date,
                as_of=self.context["as_of"],
            )
        )

    def get_days_to_due(self, obligation: ObligationInstance) -> int | None:
        if obligation.due_date is None:
            return None
        return (obligation.due_date - self.context["as_of"]).days


class DocumentSerializer(serializers.ModelSerializer[Document]):
    """Evidence, as the client needs to see it.

    ``download_url`` is a route on this API, not a storage URL. A signed storage
    link would outlive the permission that produced it and leave the platform
    unable to say whether the bytes were ever fetched.
    """

    download_url = serializers.SerializerMethodField()
    downloadable = serializers.BooleanField(source="is_downloadable", read_only=True)

    class Meta:
        model = Document
        fields = [
            "id",
            "title",
            "kind",
            "original_filename",
            "content_type",
            "size_bytes",
            "scan_state",
            "downloadable",
            "download_url",
            "created_at",
        ]
        read_only_fields = fields

    def get_download_url(self, document: Document) -> str | None:
        if not document.is_downloadable:
            return None
        return f"/app/documents/{document.pk}/download/"


class ObligationDetailSerializer(ObligationSerializer):
    documents = DocumentSerializer(many=True, read_only=True)
    allowed_transitions = serializers.SerializerMethodField()

    class Meta(ObligationSerializer.Meta):
        fields = [
            *ObligationSerializer.Meta.fields,
            "filing_reference",
            "filed_on",
            "documents",
            "allowed_transitions",
        ]
        read_only_fields = fields

    def get_allowed_transitions(self, obligation: ObligationInstance) -> list[dict[str, Any]]:
        """What this user may do next, decided here rather than in the app.

        The same `available_actions` the web action menu is built from, so the two
        clients cannot drift. Hardcoding the lifecycle into a mobile binary would
        mean an app-store release every time a state is added — and, worse, a
        stale binary offering a transition the server then refuses.
        """
        from stacos.obligations.transitions import available_actions

        permissions: frozenset[str] = self.context.get("permissions", frozenset())
        return [
            {
                "to_state": str(move.target),
                "label": str(move.label),
                "requires_note": move.requires_note,
                "requires_filing_reference": move.requires_filing_reference,
                "confirmation": move.confirmation,
            }
            for move in available_actions(obligation, permissions=permissions)
        ]


class RequestItemSerializer(serializers.ModelSerializer[RequestItem]):
    answered = serializers.BooleanField(source="is_answered", read_only=True)

    class Meta:
        model = RequestItem
        fields = [
            "id",
            "label",
            "help_text",
            "kind",
            "is_mandatory",
            "answered",
            "response_value",
            "document_count",
        ]
        read_only_fields = fields


class RequestSerializer(serializers.ModelSerializer[InformationRequest]):
    entity_name = serializers.CharField(source="entity.name", read_only=True)
    outstanding = serializers.SerializerMethodField()

    class Meta:
        model = InformationRequest
        fields = [
            "id",
            "title",
            "entity",
            "entity_name",
            "state",
            "due_on",
            "outstanding",
            "created_at",
        ]
        read_only_fields = fields

    def get_outstanding(self, request: InformationRequest) -> int:
        # `items.all()` rather than a `.count()` query: the view prefetches, and a
        # count per row is the N+1 that makes a list feel slow on a phone.
        return sum(1 for item in request.items.all() if not item.is_answered)


class RequestDetailSerializer(RequestSerializer):
    items = RequestItemSerializer(many=True, read_only=True)

    class Meta(RequestSerializer.Meta):
        fields = [*RequestSerializer.Meta.fields, "message", "items"]
        read_only_fields = fields


class NotificationSerializer(serializers.ModelSerializer[Notification]):
    read = serializers.BooleanField(source="is_read", read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id",
            "kind",
            "severity",
            "title",
            "body",
            "url",
            "subject_type",
            "subject_id",
            "read",
            "created_at",
        ]
        read_only_fields = fields


class EvidenceUploadSerializer(serializers.Serializer[Any]):
    """A photograph of a challan, taken on the spot.

    ``file`` rather than a base64 blob: a 4 MB photograph becomes 5.4 MB of JSON
    that has to be held in memory whole, and multipart streams to disk.
    """

    file = serializers.FileField()
    title = serializers.CharField(max_length=250, required=False, allow_blank=True)
    kind = serializers.CharField(max_length=20, required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)


class TransitionSerializer(serializers.Serializer[Any]):
    to_state = serializers.CharField(max_length=28)
    reference = serializers.CharField(max_length=120, required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)


class ItemResponseSerializer(serializers.Serializer[Any]):
    value = serializers.CharField(required=False, allow_blank=True)
    file = serializers.FileField(required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs.get("value") and not attrs.get("file"):
            raise serializers.ValidationError(
                "Send a value, a file, or both — an empty response is not an answer."
            )
        return attrs
