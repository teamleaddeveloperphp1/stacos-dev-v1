"""
Forms for tenancy objects.

crispy-forms renders the fields; django-cotton renders everything around them.
That boundary is fixed — mixing the two produces two design systems in one
product, and the seam is visible to users within a month.
"""

from __future__ import annotations

from typing import Any, cast

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row
from django import forms
from django.utils.translation import gettext_lazy as _

from stacos.jurisdictions import subdivisions
from stacos.jurisdictions.facts import (
    ENTITY_TYPES,
    IN_STATE_CODES,
    PREMISES_TYPES,
    REGISTRATION_TYPES,
    REGISTRY,
    FactType,
)
from stacos.tenancy.models import Entity, EntityPremises, EntityRegistration, Role

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

#: Derived from the subdivision table rather than hand-maintained. The old
#: copy here was one of five independent spellings of the same 36 places, and
#: nothing kept them in step.
STATE_LABELS: dict[str, str] = dict(subdivisions.choices())


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


#: Human labels for the premises types the fact registry knows about. Same
#: reasoning as ``REGISTRATION_TYPE_LABELS``.
PREMISES_TYPE_LABELS: dict[str, str] = {
    "REGISTERED_OFFICE": "Registered office",
    "CORPORATE_OFFICE": "Corporate office",
    "BRANCH": "Branch",
    "FACTORY": "Factory",
    "PLANT": "Plant",
    "WAREHOUSE": "Warehouse",
    "RETAIL_STORE": "Retail store",
    "SITE": "Site",
}


class PremisesForm(forms.ModelForm[EntityPremises]):
    """Record a physical location an entity operates from.

    A whole family of obligations is per-premises rather than per-entity —
    factory licence renewals, fire NOCs, pollution consents — so this is a
    first-class row, not a text field on the entity.

    The entity is not a field here either, for the same reason as
    ``RegistrationForm``: it comes from the URL and is re-fetched under the
    caller's scope in the view.
    """

    class Meta:
        model = EntityPremises
        fields = [
            "name",
            "type",
            "jurisdiction",
            "address",
            "operational_from",
            "operational_to",
        ]
        labels = {
            "name": _("Name"),
            "jurisdiction": _("State"),
            "address": _("Address"),
            "operational_from": _("Operational from"),
            "operational_to": _("Operational to"),
        }
        help_texts = {
            "name": _("What you call this site — “Surat Head Office”, “Plant 2”."),
            "jurisdiction": _("Determines which state's factory, fire and pollution rules apply."),
            "operational_to": _("Leave blank while the site is in use. Set it when a site closes."),
        }
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
            "operational_from": forms.DateInput(attrs={"type": "date"}),
            "operational_to": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        type_choices: list[tuple[str, Any]] = [("", _("Select…"))]
        type_choices += [(code, PREMISES_TYPE_LABELS.get(code, code)) for code in PREMISES_TYPES]

        state_choices: list[tuple[str, Any]] = [("", _("Not state-specific"))]
        state_choices += sorted(
            ((code, STATE_LABELS.get(code, code)) for code in IN_STATE_CODES),
            key=lambda pair: pair[1],
        )

        self.fields["type"] = forms.ChoiceField(
            label=_("Type"),
            choices=type_choices,
            help_text=_("Some obligations — a factory licence, a fire NOC — apply only to a type."),
        )
        self.fields["jurisdiction"] = forms.ChoiceField(
            label=_("State"), required=False, choices=state_choices
        )
        self.fields["address"].required = False
        self.fields["operational_from"].required = False
        self.fields["operational_to"].required = False

        self.helper = FormHelper()
        # The submit button lives in the modal footer, not in the form body.
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(Column("name"), Column("type")),
            Row(Column("jurisdiction")),
            "address",
            Row(Column("operational_from"), Column("operational_to")),
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
                # Matches `EntityProfile`'s `DecimalField(max_digits=18, decimal_places=2)`
                # columns (aggregate_turnover, paid_up_capital, net_worth) — without
                # `max_digits` here, a value too large for that column reaches the
                # database as a clean-looking form submission and dies there instead,
                # as a 500 rather than a field error next to the input.
                field = forms.DecimalField(
                    required=False, min_value=0, max_digits=18, decimal_places=2
                )
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
        """The answer in the shape the fact registry expects, or ``None``.

        Answers are held in ``OnboardingDraft.answers``, which the session
        backend serialises as JSON — so nothing here may return a
        ``decimal.Decimal``, which ``DecimalField.clean()`` produces and which
        the JSON encoder cannot handle. ``float`` is a safe substitute: the
        fact registry's own validation already accepts it for
        ``FactType.DECIMAL`` (``jurisdictions/facts.py``), and
        ``engine.types.to_decimal`` converts it back via ``Decimal(str(value))``
        when a rule actually needs to compare it.
        """
        raw = self.cleaned_data.get("answer")
        if raw in (None, ""):
            return None
        if getattr(self, "fact", None) is None:
            return raw
        if self.fact.type is FactType.BOOL:
            return raw == "yes"
        if self.fact.type is FactType.DECIMAL:
            return float(raw)
        return raw


class InviteColleagueForm(forms.Form):
    """Ask a colleague by email, and say what they will be able to do.

    The role is chosen at the point of invitation rather than afterwards, so
    nobody lands in the workspace with whatever the default happened to be and
    has to be corrected. Only the system roles for this tenant's type are
    offered — inviting somebody into a practice role at an organisation would
    produce a membership whose permissions name features that are not there.
    """

    email = forms.EmailField(label=_("Their work email"))
    role: forms.ModelChoiceField[Role] = forms.ModelChoiceField(
        label=_("What can they do?"), queryset=Role.objects.none()
    )
    message = forms.CharField(
        required=False,
        label=_("Add a note (optional)"),
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Included in the invitation email."),
    )

    def __init__(self, *args: Any, tenant: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        role_field = cast("forms.ModelChoiceField[Role]", self.fields["role"])
        if tenant is not None:
            role_field.queryset = Role.objects.filter(
                tenant__isnull=True, tenant_type=tenant.type
            ).order_by("rank", "name")

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(Row(Column("email"), Column("role")), "message")

    def clean_email(self) -> str:
        return str(self.cleaned_data["email"]).strip().lower()
