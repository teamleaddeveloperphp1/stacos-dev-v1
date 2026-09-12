"""Forms for the onboarding wizard."""

from __future__ import annotations

from typing import Any, cast

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row
from django import forms
from django.utils.translation import gettext_lazy as _

from stacos.jurisdictions import subdivisions
from stacos.jurisdictions.decode import Confidence, IdentityReport

#: ``QuestionForm`` is re-exported, not defined here. It moved to
#: ``stacos.tenancy.forms`` when the compliance calendar started asking the same
#: questions of a saved entity — it was never specific to onboarding, and two
#: copies of the three-way boolean is one copy too many. Imported here so that
#: every existing caller keeps working.
from stacos.tenancy.forms import ENTITY_TYPE_LABELS, QuestionForm

__all__ = ["IdentityForm", "ProfileForm", "QuestionForm"]


class IdentityForm(forms.Form):
    """Paste whatever identifiers you have.

    Every field is optional. Making any of them required would block the case
    the wizard exists for — somebody registering an existing business who only
    has one identifier to hand.

    CIN and LLPIN have no field here: neither is required for anything this
    product currently tracks, and a pasted letterhead or signature block still
    gets one recognised — see ``sniff()`` in ``stacos.jurisdictions.decode`` —
    so nothing is lost, just no dedicated box for typing one in by hand.
    """

    pasted = forms.CharField(
        required=False,
        label=_("Paste anything you have"),
        help_text=_("A letterhead footer, a signature block, or the identifiers one per line."),
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "PAN, TAN, GSTIN…"}),
    )
    pan = forms.CharField(required=False, label=_("PAN"), max_length=10)
    tan = forms.CharField(required=False, label=_("TAN"), max_length=10)
    gstin = forms.CharField(required=False, label=_("GSTIN"), max_length=15)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("pasted", Row(Column("pan"), Column("tan"), Column("gstin")))

    def identifiers(self) -> list[tuple[str, str]]:
        """``(registration_type, value)`` pairs, from the typed fields."""
        data = self.cleaned_data
        pairs = [
            ("PAN", data.get("pan", "")),
            ("TAN", data.get("tan", "")),
            ("GST", data.get("gstin", "")),
        ]
        return [(kind, value.strip()) for kind, value in pairs if value and value.strip()]


class ProfileForm(forms.Form):
    """Name, legal form and where the business operates.

    The entity-type field is where the decoder earns its keep. A ``CERTAIN`` hint
    fills it in and the template says which identifier it came from; a
    ``POSSIBLE`` one leaves it unset and promotes the candidates to the top of
    the list, because "we guessed, pick one" is honest and "we filled it in from
    a guess" is not.
    """

    name = forms.CharField(label=_("Business name"), max_length=200)
    legal_name = forms.CharField(required=False, label=_("Registered legal name"), max_length=250)
    entity_type = forms.ChoiceField(label=_("Legal form"))
    registered_office_state = forms.ChoiceField(label=_("Registered office state"))
    incorporation_date = forms.DateField(
        required=False,
        label=_("Date of incorporation"),
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    states_of_operation = forms.MultipleChoiceField(
        required=False,
        label=_("Other states you operate in"),
        widget=forms.SelectMultiple(attrs={"size": 8}),
        help_text=_("A state where you hold a registration is added automatically."),
    )

    def __init__(self, *args: Any, report: IdentityReport | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        state_choices = subdivisions.choices()
        entity_choices = sorted(ENTITY_TYPE_LABELS.items(), key=lambda pair: pair[1])

        suggested = report.hint_for("entity_type") if report else None
        if suggested is not None and suggested.confidence is not Confidence.CERTAIN:
            # Ambiguous: promote the candidates, select nothing.
            promoted = [(code, label) for code, label in entity_choices if code in suggested.values]
            rest = [(code, label) for code, label in entity_choices if code not in suggested.values]
            entity_choices = promoted + rest

        entity_field = cast("forms.ChoiceField", self.fields["entity_type"])
        state_field = cast("forms.ChoiceField", self.fields["registered_office_state"])
        operating_field = cast("forms.MultipleChoiceField", self.fields["states_of_operation"])
        entity_field.choices = [("", _("Select…")), *entity_choices]
        state_field.choices = [("", _("Select…")), *state_choices]
        operating_field.choices = state_choices

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(Column("name"), Column("legal_name")),
            Row(Column("entity_type"), Column("registered_office_state")),
            Row(Column("incorporation_date"), Column("states_of_operation")),
        )
