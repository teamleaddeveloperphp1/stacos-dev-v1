"""Forms for the secretarial module."""

from __future__ import annotations

from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout
from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.audit import record_event
from stacos.core.forms import ScopedModelChoiceField
from stacos.core.models import AuditAction
from stacos.secretarial.models import Meeting, MeetingAttendee
from stacos.tenancy.models import Entity

__all__ = ["AttendeeForm", "MeetingForm", "MinutesForm"]


class MeetingForm(forms.ModelForm[Meeting]):
    entity = ScopedModelChoiceField(
        Entity, filters={"archived_at__isnull": True}, label=_("Entity")
    )

    class Meta:
        model = Meeting
        fields = [
            "entity",
            "kind",
            "serial_number",
            "scheduled_for",
            "venue",
            "is_video_conference",
            "quorum_required",
            "chairperson",
            "agenda",
        ]
        widgets = {
            "scheduled_for": forms.DateInput(attrs={"type": "date"}),
            "agenda": forms.Textarea(attrs={"rows": 4}),
        }
        help_texts = {
            "quorum_required": _(
                "One third of directors or two, whichever is higher, for a board meeting."
            ),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["scheduled_for"].initial = timezone.localdate()
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "entity",
            "kind",
            "serial_number",
            "scheduled_for",
            "venue",
            "is_video_conference",
            "quorum_required",
            "chairperson",
            "agenda",
        )

    def save(self, commit: bool = True, *, actor: Any = None) -> Meeting:
        if not commit:
            raise ValueError(
                f"{type(self).__name__} writes related rows as part of the save, "
                f"which needs a primary key. commit=False is not supported."
            )
        meeting: Meeting = super().save(commit=False)
        meeting.tenant = meeting.entity.tenant
        meeting.recorded_by = actor if getattr(actor, "is_authenticated", False) else None
        meeting.save()

        record_event(
            action=AuditAction.CREATE,
            actor=actor,
            obj=meeting,
            after={"kind": meeting.kind, "scheduled_for": meeting.scheduled_for.isoformat()},
        )
        return meeting


class AttendeeForm(forms.Form):
    name = forms.CharField(max_length=200, label=_("Name"))
    din = forms.CharField(max_length=20, required=False, label=_("DIN"))
    role = forms.ChoiceField(
        choices=MeetingAttendee.Role.choices, initial=MeetingAttendee.Role.DIRECTOR
    )
    attendance = forms.ChoiceField(
        choices=MeetingAttendee.Attendance.choices,
        initial=MeetingAttendee.Attendance.PRESENT,
    )
    is_interested = forms.BooleanField(
        required=False,
        label=_("Interested in an agenda item"),
        help_text=_("An interested director must abstain from that item under Section 184."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False


class MinutesForm(forms.Form):
    held_on = forms.DateField(label=_("Held on"), widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["held_on"].initial = timezone.localdate()
        self.helper = FormHelper()
        self.helper.form_tag = False
