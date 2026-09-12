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

__all__ = [
    "NotificationSerializer",
    "ObligationDetailSerializer",
    "ObligationSerializer",
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


class ObligationDetailSerializer(ObligationSerializer):
    allowed_transitions = serializers.SerializerMethodField()

    class Meta(ObligationSerializer.Meta):
        fields = [
            *ObligationSerializer.Meta.fields,
            "filing_reference",
            "filed_on",
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


class TransitionSerializer(serializers.Serializer[Any]):
    to_state = serializers.CharField(max_length=28)
    reference = serializers.CharField(max_length=120, required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)
