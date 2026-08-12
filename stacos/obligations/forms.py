"""Forms for the register. Fields are rendered by crispy-forms."""

from __future__ import annotations

from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout
from django import forms
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

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
