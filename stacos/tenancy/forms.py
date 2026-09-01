"""
Forms for tenancy objects.

crispy-forms renders the fields; django-cotton renders everything around them.
That boundary is fixed — mixing the two produces two design systems in one
product, and the seam is visible to users within a month.
"""

from __future__ import annotations

from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row
from django import forms
from django.utils.translation import gettext_lazy as _

from stacos.jurisdictions.facts import ENTITY_TYPES, IN_STATE_CODES, REGISTRATION_TYPES
from stacos.tenancy.models import Entity, EntityRegistration

#: Human labels for the entity types the fact registry knows about. Kept here
#: rather than on the model so the vocabulary stays data, not a hardcoded enum
#: that another jurisdiction would have to fight.
ENTITY_TYPE_LABELS: dict[str, str] = {
    "PVT_LTD": "Private Limited Company",
    "PUBLIC_LTD": "Public Limited Company",
    "OPC": "One Person Company",
    "LLP": "Limited Liability Partnership",
    "PARTNERSHIP": "Partnership Firm",
    "PROPRIETORSHIP": "Sole Proprietorship",
    "TRUST": "Trust",
    "SOCIETY": "Society",
    "SECTION_8": "Section 8 Company",
    "BRANCH_OFFICE": "Branch Office of a foreign company",
    "LIAISON_OFFICE": "Liaison Office",
    "COOPERATIVE": "Co-operative Society",
    "HUF": "Hindu Undivided Family",
}

STATE_LABELS: dict[str, str] = {
    "IN-AN": "Andaman and Nicobar Islands",
    "IN-AP": "Andhra Pradesh",
    "IN-AR": "Arunachal Pradesh",
    "IN-AS": "Assam",
    "IN-BR": "Bihar",
    "IN-CH": "Chandigarh",
    "IN-CT": "Chhattisgarh",
    "IN-DH": "Dadra and Nagar Haveli and Daman and Diu",
    "IN-DL": "Delhi",
    "IN-GA": "Goa",
    "IN-GJ": "Gujarat",
    "IN-HP": "Himachal Pradesh",
    "IN-HR": "Haryana",
    "IN-JH": "Jharkhand",
    "IN-JK": "Jammu and Kashmir",
    "IN-KA": "Karnataka",
    "IN-KL": "Kerala",
    "IN-LA": "Ladakh",
    "IN-LD": "Lakshadweep",
    "IN-MH": "Maharashtra",
    "IN-ML": "Meghalaya",
    "IN-MN": "Manipur",
    "IN-MP": "Madhya Pradesh",
    "IN-MZ": "Mizoram",
    "IN-NL": "Nagaland",
    "IN-OR": "Odisha",
    "IN-PB": "Punjab",
    "IN-PY": "Puducherry",
    "IN-RJ": "Rajasthan",
    "IN-SK": "Sikkim",
    "IN-TG": "Telangana",
    "IN-TN": "Tamil Nadu",
    "IN-TR": "Tripura",
    "IN-UP": "Uttar Pradesh",
    "IN-UT": "Uttarakhand",
    "IN-WB": "West Bengal",
}


class EntityForm(forms.ModelForm[Entity]):
    """Create or edit a legal entity.

    Deliberately short. The full compliance profile — turnover, headcount,
    registrations, premises, the flags that drive applicability — is a separate,
    longer wizard. Asking for all of it before an entity exists is how onboarding
    gets abandoned.
    """

    class Meta:
        model = Entity
        fields = [
            "name",
            "legal_name",
            "short_code",
            "entity_type",
            "incorporation_date",
            "registered_office_state",
            "registered_office_address",
        ]
        labels = {
            "name": _("Name"),
            "legal_name": _("Full legal name"),
            "short_code": _("Short code"),
            "entity_type": _("Entity type"),
            "incorporation_date": _("Date of incorporation"),
            "registered_office_state": _("Registered office state"),
            "registered_office_address": _("Registered office address"),
        }
        help_texts = {
            "name": _("What you call it day to day."),
            "legal_name": _("As it appears on the certificate of incorporation."),
            "short_code": _("A few letters, used in lists and filenames. Optional."),
            "incorporation_date": _("Drives first-year filings such as INC-20A and the first AGM."),
            "registered_office_state": _("Determines which state's labour and tax rules apply."),
        }
        widgets = {
            "incorporation_date": forms.DateInput(attrs={"type": "date"}),
            "registered_office_address": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        # Choices come from the fact registry rather than a model enum, so a new
        # jurisdiction adds entity types as data instead of as a migration.
        entity_choices: list[tuple[str, Any]] = [("", _("Select…"))]
        entity_choices += [(code, ENTITY_TYPE_LABELS.get(code, code)) for code in ENTITY_TYPES]

        state_choices: list[tuple[str, Any]] = [("", _("Select…"))]
        state_choices += sorted(
            ((code, STATE_LABELS.get(code, code)) for code in IN_STATE_CODES),
            key=lambda pair: pair[1],
        )

        self.fields["entity_type"] = forms.ChoiceField(
            label=_("Entity type"),
            choices=entity_choices,
            help_text=_("Drives which company-law filings apply."),
        )
        self.fields["registered_office_state"] = forms.ChoiceField(
            label=_("Registered office state"),
            required=False,
            choices=state_choices,
            help_text=_("Determines which state's labour and tax rules apply."),
        )
        self.fields["legal_name"].required = False
        self.fields["short_code"].required = False
        self.fields["registered_office_address"].required = False

        self.helper = FormHelper()
        # The submit button lives in the modal footer, not in the form body.
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "name",
            "legal_name",
            Row(Column("entity_type"), Column("short_code")),
            Row(Column("incorporation_date"), Column("registered_office_state")),
            "registered_office_address",
        )

    def clean_short_code(self) -> str:
        return (self.cleaned_data.get("short_code") or "").upper().strip()

    def clean_name(self) -> str:
        name = (self.cleaned_data.get("name") or "").strip()
        # Scoped by the manager, so this only ever sees the current tenant's rows.
        existing = Entity.objects.filter(name__iexact=name, archived_at__isnull=True)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError(_("You already have an entity with this name."))
        return name


#: Human labels for the registration types the fact registry knows about. Same
#: reasoning as ``ENTITY_TYPE_LABELS``: the vocabulary is data, and a
#: jurisdiction that issues a different identifier should not need a migration.
REGISTRATION_TYPE_LABELS: dict[str, str] = {
    "PAN": "PAN — Permanent Account Number",
    "TAN": "TAN — Tax Deduction Account Number",
    "GST": "GSTIN — Goods and Services Tax",
    "CIN": "CIN — Corporate Identity Number",
    "LLPIN": "LLPIN — LLP Identification Number",
    "PF": "EPF — Provident Fund establishment code",
    "ESIC": "ESIC — Employees' State Insurance",
    "PT_EC": "Professional Tax — Enrolment Certificate",
    "PT_RC": "Professional Tax — Registration Certificate",
    "IEC": "IEC — Importer Exporter Code",
    "UDYAM": "Udyam — MSME registration",
    "FACTORY_LICENCE": "Factory licence",
    "SHOPS_ESTAB": "Shops and Establishments registration",
    "PCB_CONSENT": "Pollution Control Board consent",
    "DRUG_LICENCE": "Drug licence",
    "FSSAI": "FSSAI licence",
    "LEGAL_METROLOGY": "Legal Metrology registration",
    "BIS": "BIS certification",
    "TRADE_LICENCE": "Trade licence",
    "FIRE_NOC": "Fire NOC",
    "CONTRACT_LABOUR": "Contract Labour registration",
}


class RegistrationForm(forms.ModelForm[EntityRegistration]):
    """Record a tax or statutory identifier against an entity.

    Deliberately thin on validation of its own: the value is checked by
    ``EntityRegistration.clean()``, which routes to the per-type validator in
    ``stacos.jurisdictions.validators``. Duplicating a PAN regex here would give
    two places to correct when the format changes, and they would disagree.

    The entity is not a field. It comes from the URL and is re-fetched under the
    caller's scope in the view, so there is nothing to forge.
    """

    class Meta:
        model = EntityRegistration
        fields = [
            "type",
            "value",
            "jurisdiction",
            "valid_from",
            "valid_to",
            "label",
            "is_primary",
        ]
        labels = {
            "value": _("Number"),
            "jurisdiction": _("State"),
            "valid_from": _("Valid from"),
            "valid_to": _("Valid to"),
            "label": _("Label"),
            "is_primary": _("This is the primary one of its type"),
        }
        help_texts = {
            "valid_to": _("Leave blank while it is current. Set it when a registration lapses."),
            "label": _("Optional. Useful when an entity holds several of the same type."),
        }
        widgets = {
            "valid_from": forms.DateInput(attrs={"type": "date"}),
            "valid_to": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        type_choices: list[tuple[str, Any]] = [("", _("Select…"))]
        type_choices += [
            (code, REGISTRATION_TYPE_LABELS.get(code, code)) for code in REGISTRATION_TYPES
        ]

        state_choices: list[tuple[str, Any]] = [("", _("Not state-specific"))]
        state_choices += sorted(
            ((code, STATE_LABELS.get(code, code)) for code in IN_STATE_CODES),
            key=lambda pair: pair[1],
        )

        self.fields["type"] = forms.ChoiceField(
            label=_("Type"),
            choices=type_choices,
            help_text=_("Each registration generates its own filings."),
        )
        self.fields["jurisdiction"] = forms.ChoiceField(
            label=_("State"),
            required=False,
            choices=state_choices,
            help_text=_("Only for state-issued registrations, such as a GSTIN or a PT number."),
        )
        self.fields["valid_from"].required = False
        self.fields["label"].required = False

        self.helper = FormHelper()
        # The submit button lives in the modal footer, not in the form body.
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(Column("type"), Column("value")),
            Row(Column("jurisdiction"), Column("label")),
            Row(Column("valid_from"), Column("valid_to")),
            "is_primary",
        )
