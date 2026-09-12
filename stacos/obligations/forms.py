"""Forms for the register. Fields are rendered by crispy-forms."""

from __future__ import annotations

from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row
from django import forms
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from stacos.core.forms import ScopedUserChoiceField
from stacos.jurisdictions.events import EVENT_TYPES
from stacos.obligations.models import EntityEvent, ObligationInstance

#: A translated string is a ``Promise`` until something renders it, which is what
#: lets one process serve a user in English and another in Hindi.
StrOrPromise = str | Promise


class TransitionForm(forms.Form):
    """The payload behind every state change.

    One form for every transition rather than one per action: the guards live in
    the transition table, and duplicating them across eleven form classes is how
    they end up disagreeing.
    """

    target = forms.CharField(max_length=28, widget=forms.HiddenInput)
    note = forms.CharField(
        label=_("Note"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Recorded on the timeline and in the audit trail."),
    )
    filing_reference = forms.CharField(
        label=_("Acknowledgement number"),
        max_length=120,
        required=False,
    )
    filed_on = forms.DateField(
        label=_("Filed on"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("Defaults to today. Set it if you are recording a past filing."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("note", "filing_reference", "filed_on")


class EntityEventForm(forms.ModelForm[EntityEvent]):
    """Records the date that unblocks a due date.

    Reached from the "tell us your AGM date to schedule this" prompt on an
    obligation the engine could not date. Deliberately tiny — the whole point is
    that answering it takes one field and ten seconds.
    """

    class Meta:
        model = EntityEvent
        fields = ["key", "occurred_on", "note"]
        widgets = {
            "occurred_on": forms.DateInput(attrs={"type": "date"}),
            "key": forms.HiddenInput,
        }
        labels = {"occurred_on": _("Date"), "note": _("Note")}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("occurred_on", "note")

    def clean_key(self) -> str:
        """The key arrives in a hidden field, so it is client-controlled.

        Not new, but sixty event types is sixty more things a forged post could
        claim to be — and an unknown key would record an event that triggers
        nothing and is invisible everywhere.
        """
        key = str(self.cleaned_data["key"])
        if key not in EVENT_TYPES:
            raise forms.ValidationError(_("That is not an event this product records."))
        return key


class RecordEventForm(forms.ModelForm[EntityEvent]):
    """Record something that happened, from the entity's own events panel.

    Distinct from :class:`EntityEventForm`, which answers one specific "we cannot
    schedule this until you tell us the AGM date" prompt with a hidden key and a
    single field. This one is the general case: the user picks *what* happened,
    and the form grows the fields that event type declares.
    """

    class Meta:
        model = EntityEvent
        fields = ["key", "occurred_on", "subject_label", "subject_ref", "note"]
        widgets = {"occurred_on": forms.DateInput(attrs={"type": "date"})}
        labels = {
            "occurred_on": _("Date it happened"),
            "subject_label": _("Who or what"),
            "subject_ref": _("Reference"),
            "note": _("Note"),
        }

    def __init__(self, *args: Any, country: str = "IN", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.fields["key"] = forms.ChoiceField(
            label=_("What happened"),
            choices=[("", _("Select…"))]
            + [
                (definition.code, definition.label)
                for definition in EVENT_TYPES.for_country(country)
            ],
        )

        # Every declared attribute becomes a field. Two directors appointed on
        # one day are only distinguishable if the role of each is captured, and
        # `trigger.when` filters on exactly these values.
        self._attribute_fields: list[str] = []
        for definition in EVENT_TYPES.for_country(country):
            for attribute in definition.attributes:
                name = f"attr_{definition.code}_{attribute.key}"
                if name in self.fields:
                    continue
                self.fields[name] = _attribute_field(attribute)
                self._attribute_fields.append(name)

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "key",
            Row(Column("occurred_on"), Column("subject_label")),
            Row(Column("subject_ref"), Column("note")),
        )

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        key = cleaned.get("key")
        definition = EVENT_TYPES.get(str(key)) if key else None
        if definition is None:
            return cleaned

        if definition.requires_subject and not cleaned.get("subject_label"):
            self.add_error(
                "subject_label",
                _("%(label)s is needed — it is what tells two same-day records apart.")
                % {"label": definition.subject_label},
            )

        attributes = {
            attribute.key: cleaned.get(f"attr_{definition.code}_{attribute.key}")
            for attribute in definition.attributes
        }
        attributes = {k: v for k, v in attributes.items() if v not in (None, "")}
        for problem in EVENT_TYPES.validate_attributes(definition.code, attributes):
            self.add_error(None, problem)

        self.instance.attributes = attributes
        return cleaned


class AssignForm(forms.Form):
    """Who is holding this obligation.

    ``assigned_to`` goes through :class:`~stacos.core.forms.ScopedUserChoiceField`
    rather than a plain ``ModelChoiceField``: ``User`` is not tenant-scoped, so an
    ordinary picker would list every person on the platform and a forged POST
    would hand a client's obligation to a stranger at another company.
    """

    assigned_to = ScopedUserChoiceField(
        label=_("Assign to"),
        required=False,
        empty_label=_("Nobody"),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("assigned_to")


def _attribute_field(attribute: Any) -> forms.Field:
    if attribute.type == "ENUM":
        field: forms.Field = forms.ChoiceField(
            required=False,
            choices=[("", "—")]
            + [
                (value, value.replace("_", " ").title())
                for value in (attribute.allowed_values or ())
            ],
        )
    elif attribute.type == "BOOL":
        field = forms.NullBooleanField(required=False)
    elif attribute.type == "DECIMAL":
        field = forms.DecimalField(required=False)
    else:
        field = forms.CharField(required=False, max_length=120)
    field.label = attribute.label
    field.help_text = attribute.help_text
    return field


#: Filters offered above the calendar. Kept as data so the toolbar, the empty
#: state and the query builder cannot drift out of step.
#:
#: Typed with ``StrOrPromise`` because these are lazily translated: the label is
#: a promise until something renders it, which is what lets one process serve a
#: user in English and another in Hindi.
STATUS_FILTERS: tuple[tuple[str, StrOrPromise], ...] = (
    ("", _("Everything open")),
    ("overdue", _("Overdue")),
    ("due_soon", _("Due in 7 days")),
    ("pending", _("Pending")),
    ("unconfirmed", _("Needs confirming")),
    ("needs_input", _("Waiting on a date")),
    ("completed", _("Completed")),
    ("all", _("All, including completed")),
)


def obligation_display(instance: ObligationInstance) -> str:
    """One-line description used in toasts and confirmation dialogs."""
    parts = [instance.title]
    if instance.scope_label:
        parts.append(instance.scope_label)
    if instance.period_label:
        parts.append(instance.period_label)
    return " · ".join(parts)
