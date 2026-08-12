"""Forms for return preparation."""

from __future__ import annotations

from typing import Any

from crispy_forms.helper import FormHelper
from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.returns.models import ReturnPreparation

__all__ = ["FigureForm", "FileReturnForm", "ReviewForm"]


class FigureForm(forms.ModelForm[ReturnPreparation]):
    """The numbers.

    ``figures`` stays a JSON blob — there are forty form types and their shapes
    change every budget, so a field per form would be forty migrations a year and
    still behind. What is extracted into columns is what has to be totalled,
    compared or shown without parsing JSON in SQL.
    """

    class Meta:
        model = ReturnPreparation
        fields = ["tax_payable", "interest_payable", "late_fee"]
        labels = {
            "tax_payable": _("Tax payable"),
            "interest_payable": _("Interest"),
            "late_fee": _("Late fee"),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False


class ReviewForm(forms.Form):
    """Accept, or send back with findings."""

    action = forms.ChoiceField(
        choices=(("REVIEW", _("Reviewed")), ("SEND_BACK", _("Send back"))),
        widget=forms.HiddenInput,
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label=_("Findings"),
        help_text=_("Required when sending back — otherwise the same return comes back."),
    )

    def clean(self) -> dict[str, Any]:
        super().clean()
        cleaned = self.cleaned_data
        if cleaned.get("action") == "SEND_BACK" and not (cleaned.get("notes") or "").strip():
            raise forms.ValidationError({"notes": _("Say what needs changing.")})
        return cleaned


class FileReturnForm(forms.Form):
    reference = forms.CharField(
        max_length=120,
        label=_("Acknowledgement number"),
        help_text=_("A filing that cannot be evidenced is not a filing."),
    )
    filed_on = forms.DateField(
        required=False,
        label=_("Filed on"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("Defaults to today."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["filed_on"].initial = timezone.localdate()
        self.helper = FormHelper()
        self.helper.form_tag = False
