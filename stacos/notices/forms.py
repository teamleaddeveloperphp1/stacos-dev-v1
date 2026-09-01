"""Forms for the notice tracker."""

from __future__ import annotations

from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout
from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.forms import ScopedModelChoiceField, ScopedUserChoiceField
from stacos.jurisdictions.models import Authority
from stacos.notices.models import Notice, NoticeState
from stacos.notices.services import record_notice
from stacos.tenancy.models import Entity

__all__ = ["NoticeForm", "NoticeResponseForm", "TransitionForm"]


class NoticeForm(forms.ModelForm[Notice]):
    """Record a notice.

    ``respond_by`` is optional and stays empty when the notice does not state a
    deadline. The form offers the thirty-day convention as help text rather than
    pre-filling it: a guessed statutory deadline that looks like a fact is the
    one thing this product must not produce.
    """

    entity = ScopedModelChoiceField(
        Entity, filters={"archived_at__isnull": True}, label=_("Entity")
    )
    # Not tenant-scoped — the regulator registry is the same for everyone — so
    # an ordinary queryset built at import time is correct here.
    authority = forms.ModelChoiceField(queryset=Authority.objects.all(), label=_("Authority"))

    # Narrowed to people in the caller's own organisation. Left to ModelForm this
    # is a plain FK to a model that is not tenant-scoped, so it renders every
    # user on the platform and accepts any of them on POST — other customers'
    # staff names, and work assignable to them. See stacos.core.forms.
    assigned_to = ScopedUserChoiceField(required=False, label=_("Assign to"))

    class Meta:
        model = Notice
        fields = [
            "entity",
            "authority",
            "reference_number",
            "notice_type",
            "subject",
            "statutory_reference",
            "summary",
            "period_key",
            "issued_on",
            "received_on",
            "respond_by",
            "demand_amount",
            "risk",
            "assigned_to",
        ]
        widgets = {
            "issued_on": forms.DateInput(attrs={"type": "date"}),
            "received_on": forms.DateInput(attrs={"type": "date"}),
            "respond_by": forms.DateInput(attrs={"type": "date"}),
            "summary": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "respond_by": _(
                "Leave blank if the notice does not state one — we will flag it for you "
                "to decide rather than assume thirty days."
            ),
            "reference_number": _("DIN, notice number or order number, exactly as printed."),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["received_on"].initial = timezone.localdate()
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "entity",
            "authority",
            "reference_number",
            "notice_type",
            "subject",
            "statutory_reference",
            "summary",
            "period_key",
            "issued_on",
            "received_on",
            "respond_by",
            "demand_amount",
            "risk",
            "assigned_to",
        )

    def clean(self) -> dict[str, Any]:
        super().clean()
        cleaned = self.cleaned_data
        received = cleaned.get("received_on")
        respond_by = cleaned.get("respond_by")
        if received and respond_by and respond_by < received:
            raise forms.ValidationError(
                {"respond_by": _("The response deadline cannot be before the notice arrived.")}
            )
        return cleaned

    def save(self, commit: bool = True, *, actor: Any = None) -> Notice:
        if not commit:
            raise ValueError(
                f"{type(self).__name__} writes related rows as part of the save, "
                f"which needs a primary key. commit=False is not supported."
            )
        data = dict(self.cleaned_data)
        entity = data.pop("entity")
        authority = data.pop("authority")
        return record_notice(
            tenant=entity.tenant,
            entity=entity,
            authority=authority,
            reference_number=data.pop("reference_number"),
            subject=data.pop("subject"),
            received_on=data.pop("received_on"),
            notice_type=data.pop("notice_type"),
            respond_by=data.pop("respond_by", None),
            actor=actor,
            **{key: value for key, value in data.items() if value not in (None, "")},
        )


class TransitionForm(forms.Form):
    target = forms.ChoiceField(choices=NoticeState.choices, widget=forms.HiddenInput)
    note = forms.CharField(required=False, max_length=500, label=_("Note"))


class NoticeResponseForm(forms.Form):
    """Record that the authority has been answered."""

    responded_on = forms.DateField(
        label=_("Responded on"), widget=forms.DateInput(attrs={"type": "date"})
    )
    reference = forms.CharField(
        label=_("Acknowledgement reference"),
        max_length=120,
        help_text=_("A response that cannot be evidenced is not a response."),
    )
    note = forms.CharField(required=False, max_length=500, label=_("Note"))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["responded_on"].initial = timezone.localdate()
        self.helper = FormHelper()
        self.helper.form_tag = False
