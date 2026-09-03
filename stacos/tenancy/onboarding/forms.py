"""Forms for the onboarding wizard."""

from __future__ import annotations

from typing import Any, cast

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row
from django import forms
from django.utils.translation import gettext_lazy as _

from stacos.jurisdictions import subdivisions
from stacos.jurisdictions.decode import Confidence, IdentityReport
from stacos.jurisdictions.facts import REGISTRY, FactType
from stacos.tenancy.forms import ENTITY_TYPE_LABELS

__all__ = ["IdentityForm", "ProfileForm", "QuestionForm"]


class IdentityForm(forms.Form):
    """Paste whatever identifiers you have.

    Every field is optional. Somebody who has just incorporated has a CIN and
    nothing else; somebody registering an existing business has all four. Making
    any of them required would block the case the wizard exists for.
    """

    pasted = forms.CharField(
        required=False,
        label=_("Paste anything you have"),
        help_text=_("A letterhead footer, a signature block, or the identifiers one per line."),
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "CIN, PAN, GSTIN, LLPIN…"}),
    )
    cin = forms.CharField(required=False, label=_("CIN"), max_length=21)
    pan = forms.CharField(required=False, label=_("PAN"), max_length=10)
    gstin = forms.CharField(required=False, label=_("GSTIN"), max_length=15)
    llpin = forms.CharField(required=False, label=_("LLPIN"), max_length=8)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "pasted", Row(Column("cin"), Column("pan")), Row(Column("gstin"), Column("llpin"))
        )

    def identifiers(self) -> list[tuple[str, str]]:
        """``(registration_type, value)`` pairs, from the typed fields."""
        data = self.cleaned_data
        pairs = [
            ("CIN", data.get("cin", "")),
            ("PAN", data.get("pan", "")),
            ("GST", data.get("gstin", "")),
            ("LLPIN", data.get("llpin", "")),
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


class QuestionForm(forms.Form):
    """One ranked question, rendered according to its fact type.

    A boolean is a three-way choice, not a checkbox. "Unanswered" has to stay
    distinguishable from "no", or the Kleene logic the whole engine rests on is
    thrown away at the last moment by the UI: an unticked box would read as a
    definite denial and silently remove obligations the user never ruled out.
    """

    def __init__(self, *args: Any, fact_key: str = "", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        definition = REGISTRY.get(fact_key)
        if definition is None:
            return

        self.fact = definition
        match definition.type:
            case FactType.BOOL:
                field: forms.Field = forms.ChoiceField(
                    required=False,
                    choices=[("", _("Not sure yet")), ("yes", _("Yes")), ("no", _("No"))],
                    widget=forms.RadioSelect,
                )
            case FactType.INT:
                field = forms.IntegerField(required=False, min_value=0)
            case FactType.DECIMAL:
                field = forms.DecimalField(required=False, min_value=0, decimal_places=2)
            case FactType.ENUM:
                field = forms.ChoiceField(
                    required=False,
                    choices=[("", _("Not sure yet"))]
                    + [
                        (value, value.replace("_", " ").title())
                        for value in (definition.allowed_values or ())
                    ],
                )
            case _:
                field = forms.CharField(required=False)

        field.label = definition.label
        field.help_text = definition.help_text
        self.fields["answer"] = field

    def answer(self) -> Any:
        """The answer in the shape the fact registry expects, or ``None``."""
        raw = self.cleaned_data.get("answer")
        if raw in (None, ""):
            return None
        if getattr(self, "fact", None) is not None and self.fact.type is FactType.BOOL:
            return raw == "yes"
        return raw
