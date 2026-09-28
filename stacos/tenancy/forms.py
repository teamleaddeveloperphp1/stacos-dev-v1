"""
Forms for tenancy objects.

crispy-forms renders the fields; django-cotton renders everything around them.
That boundary is fixed — mixing the two produces two design systems in one
product, and the seam is visible to users within a month.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Div, Field, Layout, Row
from django import forms
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.forms import ScopedModelMultipleChoiceField
from stacos.jurisdictions import subdivisions
from stacos.jurisdictions.facts import (
    ENTITY_TYPES,
    IN_STATE_CODES,
    PREMISES_TYPES,
    REGISTRY,
    FactType,
)
from stacos.jurisdictions.registration_requirements import RegistrationRequirement
from stacos.jurisdictions.validators import get_validator, validate_registration_value
from stacos.tenancy.access import AccessGrant, engaged_clients
from stacos.tenancy.models import (
    Entity,
    EntityPremises,
    EntityProfile,
    EntityRegistration,
    Membership,
    Role,
    Tenant,
)
from stacos.tenancy.registration_gaps import RegistrationGaps

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
    "GOVERNMENT": "Government",
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
    "PF": "Employees' Provident Fund (EPF) Number",
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

    def __init__(self, *args: Any, entity: Entity, gaps: RegistrationGaps, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        # Only what the entity does not already hold — see
        # `stacos.tenancy.registration_gaps`. A forged post naming a held type
        # fails as an invalid choice rather than on the unique constraint.
        type_choices: list[tuple[str, Any]] = [("", _("Select…"))]
        type_choices += [
            (r.code, REGISTRATION_TYPE_LABELS.get(r.code, r.code)) for r in gaps.available
        ]

        # The State list narrows to the states still free for the chosen type,
        # when that type is issued per state. Re-rendered on every change of
        # type by the `hx-get` below, and re-derived here from the posted type,
        # so the rule holds without the browser's co-operation.
        chosen = self.data.get(self.add_prefix("type")) if self.is_bound else None
        chosen = chosen or self.initial.get("type") or ""
        requirement = gaps.requirement(chosen)
        taken = (
            gaps.taken_jurisdictions(chosen)
            if requirement is not None and requirement.per_jurisdiction
            else frozenset()
        )
        type_label = REGISTRATION_TYPE_LABELS.get(chosen, chosen)

        state_choices: list[tuple[str, Any]] = []
        if "" not in taken:
            state_choices.append(("", _("Not state-specific")))
        state_choices += sorted(
            ((code, STATE_LABELS.get(code, code)) for code in IN_STATE_CODES if code not in taken),
            key=lambda pair: pair[1],
        )

        self.fields["type"] = forms.ChoiceField(
            label=_("Type"),
            choices=type_choices,
            help_text=_("Each registration generates its own filings."),
            error_messages={"invalid_choice": _("That registration is already recorded.")},
        )
        self.fields["jurisdiction"] = forms.ChoiceField(
            label=_("State"),
            required=False,
            choices=state_choices,
            help_text=(
                _("States that already have one are not listed.")
                if taken
                else _("Only for state-issued registrations, such as a GSTIN or a PT number.")
            ),
            error_messages={
                "invalid_choice": _("%(type)s is already recorded for that state.")
                % {"type": type_label}
            },
        )
        self.fields["valid_from"].required = False

        self.helper = FormHelper()
        # The submit button lives in the modal footer, not in the form body.
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column(
                    Field(
                        "type",
                        hx_get=reverse("app:registration_create", args=[entity.pk]),
                        hx_target="#registration-jurisdiction",
                        hx_select="#registration-jurisdiction",
                        hx_swap="outerHTML",
                        hx_include="closest form",
                        hx_params="type,jurisdiction",
                    )
                ),
                Column("value"),
            ),
            Div("jurisdiction", css_id="registration-jurisdiction", aria_live="polite"),
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
    """Turnover and headcount — optional on every entity type. Leaving either
    blank is a legitimate "unknown" fact: the engine resolves the applicable
    rule to UNKNOWN and materialises the obligation unconfirmed rather than
    guessing, so onboarding must not force a value out of the user.

    Deliberately independent of ``EntityRegistrationFieldsForm``: these two
    facts live on ``EntityProfile``, not as an ``EntityRegistration`` row, so
    duplicating them there would be exactly the second parallel store the
    product spec forbids.
    """

    class Meta:
        model = EntityProfile
        fields = ["aggregate_turnover", "employee_count"]
        labels = {
            "employee_count": _("Number of Employees"),
        }
        help_texts = {
            "employee_count": _("Employees on payroll."),
        }

    #: Turnover is typed in crores — a ten-digit rupee figure is unreadable in
    #: an input box and one slipped zero is a 10x error — but stored in rupees,
    #: because every applicability threshold in the catalog is in rupees.
    #: Seven decimal places so any rupee amount round-trips exactly: editing an
    #: entity must never silently round a value already on file.
    aggregate_turnover = forms.DecimalField(
        label=_("Turnover (₹ crore)"),
        help_text=_(
            "Annual aggregate turnover for the most recently closed year, in crores — "
            "e.g. 12.5 for ₹12.5 Cr, 0.4 for ₹40 lakh."
        ),
        required=False,
        min_value=Decimal("0"),
        max_digits=18,
        decimal_places=7,
        widget=forms.NumberInput(attrs={"step": "any", "inputmode": "decimal"}),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["employee_count"].required = False
        if self.instance.aggregate_turnover is not None:
            self.initial["aggregate_turnover"] = _rupees_to_crore(self.instance.aggregate_turnover)

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(Row(Column("aggregate_turnover"), Column("employee_count")))

    def clean_aggregate_turnover(self) -> Decimal | None:
        crore: Decimal | None = self.cleaned_data.get("aggregate_turnover")
        if crore is None:
            return None
        return (crore * RUPEES_PER_CRORE).quantize(Decimal("0.01"))


RUPEES_PER_CRORE = Decimal(10_000_000)


def _rupees_to_crore(rupees: Decimal) -> str:
    """``Decimal("805000000.00")`` -> ``"80.5"`` — no trailing zeros, never
    scientific notation, so the input box shows what a person would type."""
    crore = (Decimal(rupees) / RUPEES_PER_CRORE).normalize()
    return format(crore, "f")


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


class _AccessFieldsMixin:
    """The reach half of an access form: which entities, clients and areas.

    Kept apart from the role on purpose. The role says what somebody may do; these
    say where. A business chooses entities, a firm chooses clients, both choose
    compliance areas; a dealer has no compliance reach to choose at all.
    """

    tenant: Any
    fields: dict[str, forms.Field]

    def _add_access_fields(self) -> list[str]:
        tenant_type = getattr(self.tenant, "type", None)
        names: list[str] = []
        if tenant_type == Tenant.Type.ORGANISATION:
            self.fields["entities"] = ScopedModelMultipleChoiceField(
                Entity,
                filters={"archived_at__isnull": True},
                required=False,
                label=_("Entities"),
                help_text=_("Leave all unticked for every entity, including ones added later."),
                widget=forms.CheckboxSelectMultiple,
            )
            names.append("entities")
        if tenant_type == Tenant.Type.PRACTICE:
            self.fields["client_tenants"] = forms.MultipleChoiceField(
                required=False,
                label=_("Clients"),
                help_text=_("Leave all unticked for the whole client book."),
                choices=[(str(pk), name) for pk, name in engaged_clients(self.tenant)],
                widget=forms.CheckboxSelectMultiple,
            )
            names.append("client_tenants")
        return names

    def grant(self, *, categories: tuple[str, ...] = ()) -> AccessGrant:
        """The reach chosen on the form. Compliance areas are not chosen here: an
        invitation takes the role's default, and an edit keeps what the member has."""
        data = cast("dict[str, Any]", getattr(self, "cleaned_data", {}))
        entities = data.get("entities") or []
        return AccessGrant(
            all_entities=not entities,
            entity_ids=tuple(entity.pk for entity in entities),
            categories=categories,
            client_tenant_ids=tuple(UUID(pk) for pk in data.get("client_tenants") or ()),
        )


def _system_roles_for(tenant: Any) -> Any:
    return Role.objects.filter(tenant__isnull=True, tenant_type=tenant.type).order_by(
        "rank", "name"
    )


class InviteColleagueForm(_AccessFieldsMixin, forms.Form):
    """Ask a colleague by email, and say what they will be able to do — and where.

    The role is chosen at the point of invitation rather than afterwards, so
    nobody lands in the workspace with whatever the default happened to be and
    has to be corrected. Only the system roles for this tenant's type are
    offered — inviting somebody into a practice role at an organisation would
    produce a membership whose permissions name features that are not there.

    The reach is chosen here too, for the same reason: a department user invited
    to "everything" and narrowed a day later has already seen the rest.
    """

    email = forms.EmailField(label=_("Their email"))
    role: forms.ModelChoiceField[Role] = forms.ModelChoiceField(
        label=_("Role"),
        queryset=Role.objects.none(),
        help_text=_("You can add or remove individual permissions once they have joined."),
    )
    def __init__(self, *args: Any, tenant: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.tenant = tenant
        role_field = cast("forms.ModelChoiceField[Role]", self.fields["role"])
        access_fields: list[str] = []
        if tenant is not None:
            role_field.queryset = _system_roles_for(tenant)
            access_fields = self._add_access_fields()

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(Column("email"), Column("role")),
            *([Div(*access_fields, css_class="access-fields")] if access_fields else []),
        )

    def clean_email(self) -> str:
        return str(self.cleaned_data["email"]).strip().lower()


class MemberAccessForm(_AccessFieldsMixin, forms.Form):
    """Change what an existing member may do, and where, in one step."""

    role: forms.ModelChoiceField[Role] = forms.ModelChoiceField(
        label=_("Role"),
        queryset=Role.objects.none(),
        help_text=_("Changing the role resets any individual permission changes below."),
    )

    def __init__(self, *args: Any, membership: Membership, **kwargs: Any) -> None:
        self.tenant = membership.tenant
        if not args and "initial" not in kwargs:
            kwargs["initial"] = {
                "role": membership.role_id,
                "entities": (
                    []
                    if membership.all_entities
                    else list(membership.entities.values_list("pk", flat=True))
                ),
                "client_tenants": [
                    str(pk) for pk in membership.client_tenants.values_list("pk", flat=True)
                ],
            }
        super().__init__(*args, **kwargs)
        role_field = cast("forms.ModelChoiceField[Role]", self.fields["role"])
        role_field.queryset = _system_roles_for(self.tenant)
        access_fields = self._add_access_fields()

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("role", *access_fields)


class WorkspaceRenameForm(forms.Form):
    """Give the workspace a real name in place of the derived one.

    A plain ``forms.Form`` rather than a ``ModelForm`` on ``Tenant``: the view
    already has the tenant from ``request.tenant`` and the write goes through
    ``tenancy.services.rename_tenant`` so the audit entry and the "no longer
    provisional" flag are written together, in the one place that means either.
    """

    name = forms.CharField(
        label=_("Organisation name"),
        max_length=200,
        help_text=_("Shown throughout STACOS, and on anything sent to your team."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("name")

    def clean_name(self) -> str:
        name = " ".join(self.cleaned_data["name"].split())
        if not name:
            raise forms.ValidationError(_("Enter a name."))
        return name
