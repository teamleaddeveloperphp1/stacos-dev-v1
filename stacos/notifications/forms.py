"""The preferences form.

Muted kinds are checkboxes rather than a text field, because the alternative is a
user typing a kind code wrong and silently muting nothing — a failure that looks
exactly like the feature working.
"""

from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from stacos.notifications.models import NotificationKind, NotificationPreference

__all__ = ["PreferenceForm"]

#: Kinds a user may switch off. Urgent-by-nature kinds are absent on purpose: a
#: statutory deadline is not something the product offers to stop mentioning.
MUTEABLE = [
    NotificationKind.OBLIGATION_DUE,
    NotificationKind.REQUEST_ANSWERED,
    NotificationKind.INVOICE_ISSUED,
    NotificationKind.WORK_ASSIGNED,
    NotificationKind.RETURN_REVIEW,
    NotificationKind.DOCUMENT_QUARANTINED,
]


class PreferenceForm(forms.ModelForm[NotificationPreference]):
    muted_kinds = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        choices=[(kind.value, kind.label) for kind in MUTEABLE],
        label=_("Do not email me about"),
        help_text=_("These still appear in the app. Urgent deadlines are always sent."),
    )
    digest_hour = forms.TypedChoiceField(
        coerce=int,
        choices=[(hour, f"{hour:02d}:00") for hour in range(24)],
        label=_("Send my summary at"),
    )

    class Meta:
        model = NotificationPreference
        fields = ["email_enabled", "whatsapp_enabled", "digest", "digest_hour", "muted_kinds"]
        labels = {
            "email_enabled": _("Email me"),
            "whatsapp_enabled": _("Message me on WhatsApp"),
            "digest": _("How often"),
        }
        help_texts = {
            "whatsapp_enabled": _(
                "Charged per message. Urgent deadlines are sent whatever this says."
            ),
        }

    def clean_muted_kinds(self) -> list[str]:
        # A list, not the QueryDict's own list proxy: this is stored as JSON and
        # the proxy does not survive serialisation.
        return list(self.cleaned_data.get("muted_kinds") or [])

    def save(self, commit: bool = True) -> NotificationPreference:
        instance = super().save(commit=False)
        instance.muted_kinds = self.cleaned_data["muted_kinds"]
        if commit:
            instance.save()
        return instance
