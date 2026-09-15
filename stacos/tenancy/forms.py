"""
Forms for tenancy objects.

crispy-forms renders the fields; django-cotton renders everything around them.
That boundary is fixed — mixing the two produces two design systems in one
product, and the seam is visible to users within a month.
"""

from __future__ import annotations

from typing import Any, cast

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Field, Layout, Row
from django import forms
from django.urls import reverse_lazy
from django.utils import timezone
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
from stacos.jurisdictions.registration_requirements import RegistrationRequirement
from stacos.jurisdictions.validators import get_validator, validate_registration_value
from stacos.tenancy.models import Entity, EntityPremises, EntityProfile, EntityRegistration, Role

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
            "entity_type",
            "incorporation_date",
            "registered_office_state",
            "registered_office_address",
        ]
        labels = {
            "name": _("Name"),
            "legal_name": _("Full legal name"),
            "entity_type": _("Entity type"),
            "incorporation_date": _("Date of incorporation"),
            "registered_office_state": _("Registered office state"),
            "registered_office_address": _("Registered office address"),
        }
        help_texts = {
            "name": _("What you call it day to day."),
            "legal_name": _("As it appears on the certificate of incorporation."),
            "incorporation_date": _("Drives first-year filings such as INC-20A and the first AGM."),
            "registered_office_state": _("Determines which state's labour and tax rules apply."),
        }
        widgets = {
            "incorporation_date": forms.DateInput(attrs={"type": "date"}),
            "registered_office_address": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args: Any, can_manage_registrations: bool = False, **kwargs: Any) -> None:
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
            choices=state_choices,
            help_text=_("Determines which state's labour and tax rules apply."),
        )

        # Every field on this form is mandatory, and says so with the red
        # asterisk crispy renders for `required` fields. The model keeps these
        # four nullable on purpose — entities also arrive from imports and
        # seeds, which have no user to ask — so the rule belongs here, at the
        # point where a person is filling the form in, not on the column.
        self.fields["legal_name"].required = True
        self.fields["incorporation_date"].required = True
        self.fields["registered_office_address"].required = True

        # A company cannot be incorporated in the future. The `max` attribute
        # keeps the date picker from offering those dates; clean_incorporation_date
        # below is the enforcement that actually matters.
        self.fields["incorporation_date"].widget.attrs["max"] = timezone.localdate().isoformat()

        # `can_manage_registrations` gates the identifier fields elsewhere on
        # this same screen (`EntityRegistrationFieldsForm`), rendered into
        # `#entity-registration-fields` and kept in step with this field. A
        # caller without the permission never gets the extra attributes, so
        # the browser never issues a request the endpoint would refuse anyway
        # (`entity_registration_fields` enforces the permission independently).
        entity_type_field: Any = "entity_type"
        if can_manage_registrations:
            entity_type_field = Field(
                "entity_type",
                **{
                    "hx-get": reverse_lazy("app:entity_registration_fields"),
                    "hx-trigger": "change",
                    "hx-target": "#entity-registration-fields",
                    "hx-swap": "innerHTML",
                    "hx-include": "closest form",
                },
            )

        self.helper = FormHelper()
        # The submit button lives in the modal footer, not in the form body.
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "name",
            "legal_name",
            entity_type_field,
            Row(Column("incorporation_date"), Column("registered_office_state")),
            "registered_office_address",
        )

    def clean_name(self) -> str:
        name = (self.cleaned_data.get("name") or "").strip()
        # Scoped by the manager, so this only ever sees the current tenant's rows.
        existing = Entity.objects.filter(name__iexact=name, archived_at__isnull=True)
        if self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise forms.ValidationError(_("You already have an entity with this name."))
        return name

    def clean_incorporation_date(self) -> Any:
        value = self.cleaned_data.get("incorporation_date")
        if value and value > timezone.localdate():
            raise forms.ValidationError(_("Date of incorporation cannot be in the future."))
        return value


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
    "FCRN": "FCRN — Foreign Company Registration Number",
    "FIRM_REGN": "Firm Regn. No.",
    "TRUST_REGN": "Trust Regn. No.",
    "SOCIETY_REGN": "Society Regn. No.",
    "COOP_REGN": "Co-op Regn. No.",
    "RBI_ROC_DETAILS": "RBI/ROC details",
    "RBI_APPROVAL": "RBI approval",
    "KARTA_PAN": "Karta PAN",
    "12AB": "12AB",
    "80G": "80G",
    "DARPAN": "Darpan",
}

#: The "Full name (ABBR)" display used only for identifiers with a well-known
#: abbreviation. Everything else keeps its short label from
#: ``REGISTRATION_TYPE_LABELS`` above, verbatim — no invented expansion.
REGISTRATION_FULL_NAME_LABELS: dict[str, str] = {
    "PAN": "Permanent Account Number (PAN)",
    "TAN": "Tax Deduction Account Number (TAN)",
    "GST": "GST Identification Number (GSTIN)",
    "CIN": "Corporate Identity Number (CIN)",
    "LLPIN": "LLP Identification Number (LLPIN)",
    "ESIC": "Employees' State Insurance Corporation (ESIC)",
    "PF": "Employees' Provident Fund (EPF)",
    "FCRN": "Foreign Company Registration Number (FCRN)",
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
            "is_primary",
        ]
        labels = {
            "value": _("Number"),
            "jurisdiction": _("State"),
            "valid_from": _("Valid from"),
            "valid_to": _("Valid to"),
            "is_primary": _("This is the primary one of its type"),
        }
        help_texts = {
            "valid_to": _("Leave blank while it is current. Set it when a registration lapses."),
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

        self.helper = FormHelper()
        # The submit button lives in the modal footer, not in the form body.
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(Column("type"), Column("value")),
            "jurisdiction",
            Row(Column("valid_from"), Column("valid_to")),
            "is_primary",
        )


def registration_field_name(code: str) -> str:
    return f"reg_{code}"


class EntityRegistrationFieldsForm(forms.Form):
    """The identifier fields the Add/Edit Entity screen shows for one entity
    type — one plain ``CharField`` per :class:`RegistrationRequirement`,
    resolved from the jurisdiction pack by
    ``stacos.jurisdictions.registration_requirements.get_registration_requirements``.

    Deliberately not a ``ModelForm``: each field maps to its own
    ``EntityRegistration`` row (upserted by the view, at ``jurisdiction=""``),
    not to a column on this form's own "model" — there is no one model whose
    fields these are.

    Format validation reuses ``validate_registration_value`` — the exact
    function ``EntityRegistration.clean()`` calls — so a value accepted here is
    guaranteed to be accepted when the view saves it, and there is exactly one
    place a format rule is written down.
    """

    def __init__(
        self,
        *args: Any,
        requirements: list[RegistrationRequirement],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.requirements = requirements

        field_names: list[str] = []
        for requirement in requirements:
            code = requirement.code
            name = registration_field_name(code)
            field_names.append(name)

            label = REGISTRATION_FULL_NAME_LABELS.get(
                code, REGISTRATION_TYPE_LABELS.get(code, code)
            )
            help_text = ""
            if not requirement.verified:
                validator = get_validator(code)
                if validator is not None and validator.help_text:
                    help_text = validator.help_text

            self.fields[name] = forms.CharField(
                label=label,
                max_length=64,
                required=requirement.is_mandatory,
                help_text=help_text,
            )

        self.helper = FormHelper()
        self.helper.form_tag = False
        # Paired two-per-row rather than one long vertical stack — the same
        # `Row(Column(...), Column(...))` idiom `EntityForm` already uses for
        # incorporation date/state. An odd field out (most rows have an even
        # count, but not all — HUF has six, Liaison Office seven) takes a full
        # width row rather than leaving a half-empty gap next to it.
        rows: list[Any] = []
        for i in range(0, len(field_names), 2):
            pair = field_names[i : i + 2]
            rows.append(Row(Column(pair[0]), Column(pair[1])) if len(pair) == 2 else pair[0])
        self.helper.layout = Layout(*rows)

    def clean(self) -> dict[str, Any]:
        super().clean()
        for requirement in self.requirements:
            name = registration_field_name(requirement.code)
            value = (self.cleaned_data.get(name) or "").strip()
            if not value:
                continue
            try:
                validate_registration_value(requirement.code, value)
            except forms.ValidationError as exc:
                self.add_error(name, exc)
        return self.cleaned_data

    def values(self) -> dict[str, str]:
        """``{code: value}`` for every field that was actually filled in."""
        return {
            requirement.code: value.strip().upper()
            for requirement in self.requirements
            if (value := self.cleaned_data.get(registration_field_name(requirement.code)))
        }

    def cleared_codes(self) -> set[str]:
        """Codes whose field was submitted, but left blank."""
        return {
            requirement.code
            for requirement in self.requirements
            if not (self.cleaned_data.get(registration_field_name(requirement.code)) or "").strip()
        }


class EntityProfileForm(forms.ModelForm[EntityProfile]):
    """Turnover and headcount — mandatory for every entity type per the
    product spec, but a plain form-level rule rather than something the
    jurisdiction pack decides, since it does not vary by entity type.

    Deliberately independent of ``EntityRegistrationFieldsForm``: these two
    facts live on ``EntityProfile``, not as an ``EntityRegistration`` row, so
    duplicating them there would be exactly the second parallel store the
    product spec forbids.
    """

    class Meta:
        model = EntityProfile
        fields = ["aggregate_turnover", "employee_count"]
        labels = {
            "aggregate_turnover": _("Turnover"),
            "employee_count": _("Employees"),
        }
        help_texts = {
            "aggregate_turnover": _("Annual aggregate turnover for the most recently closed year."),
            "employee_count": _("Employees on payroll."),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["aggregate_turnover"].required = True
        self.fields["employee_count"].required = True

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(Row(Column("aggregate_turnover"), Column("employee_count")))


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
