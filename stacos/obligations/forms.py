"""Forms for the register. Fields are rendered by crispy-forms."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row
from django import forms
from django.utils import timezone
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
    #: Only meaningful for "Defer" — see ``ObligationSuppression.expires_on``.
    #: Optional: an indefinite deferral is a real, common answer, not a form
    #: left half-filled.
    defer_until = forms.DateField(
        label=_("Come back to this on"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("Leave blank if there is no date yet — you can always come back sooner."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("note", "filing_reference", "filed_on", "defer_until")


#: What an acknowledgement may be. A portal hands back a PDF; a phone hands back
#: a photograph of the screen. Nothing else is worth accepting, and an allowlist
#: is the only form of this check that is not a guessing game — the extension is
#: checked as well as the browser-declared type, because the latter is simply
#: whatever the client chose to claim.
ACKNOWLEDGEMENT_SUFFIXES: frozenset[str] = frozenset({".pdf", ".png", ".jpg", ".jpeg"})
ACKNOWLEDGEMENT_TYPES: frozenset[str] = frozenset({"application/pdf", "image/png", "image/jpeg"})
#: Ten megabytes. An acknowledgement is one page; anything larger is a mistake or
#: an attempt to fill the disk.
ACKNOWLEDGEMENT_MAX_BYTES = 10 * 1024 * 1024


class FilingCompletedForm(forms.Form):
    """ "Yes, it is done" — when, under what number, and the proof.

    The detail page asks one question and this is half the answer. Deliberately
    three fields: the date and the acknowledgement number are what an assessment
    is defended with, and the document is what makes the number checkable
    without logging into the portal.
    """

    filed_on = forms.DateField(
        label=_("Submission date"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("The date it was actually submitted, not the date you are recording it."),
    )
    filing_reference = forms.CharField(
        label=_("Acknowledgement number"),
        max_length=120,
        help_text=_("The reference the portal gave back."),
    )
    acknowledgement = forms.FileField(
        label=_("Acknowledgement document"),
        required=False,
        help_text=_("PDF or photo, up to 10 MB. You can add it later."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(Column("filed_on"), Column("filing_reference")), "acknowledgement"
        )

    def clean_filed_on(self) -> date:
        """A filing cannot have happened tomorrow.

        Caught here rather than left to the database: a future date would sail
        past ``filed_late`` — which compares two dates and asks no questions
        about either — and quietly report a late filing as on time.
        """
        filed_on: date = self.cleaned_data["filed_on"]
        if filed_on > timezone.localdate():
            raise forms.ValidationError(_("That date is in the future."))
        return filed_on

    def clean_acknowledgement(self) -> Any:
        return validate_acknowledgement(self.cleaned_data.get("acknowledgement"))


class AcknowledgementForm(forms.Form):
    """Just the document, for the case where the number was recorded first.

    Shares :func:`validate_acknowledgement` with :class:`FilingCompletedForm`
    rather than restating the rules: two upload paths reaching the same field
    with two different ideas of what is acceptable is how the stricter one gets
    quietly bypassed.
    """

    acknowledgement = forms.FileField(label=_("Acknowledgement document"))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("acknowledgement")

    def clean_acknowledgement(self) -> Any:
        return validate_acknowledgement(self.cleaned_data.get("acknowledgement"))


def validate_acknowledgement(upload: Any) -> Any:
    """Size and type, checked on every path that accepts one of these.

    Both halves of the type check earn their place. The browser-declared content
    type is whatever the client chose to claim, so it cannot be trusted alone;
    the extension is what the filename ends in, which says nothing about the
    bytes. Requiring both to be plausible is not proof of anything — it is the
    cheap half of the job, and the expensive half is a virus scanner this
    product does not have yet.
    """
    if not upload:
        return upload

    if upload.size > ACKNOWLEDGEMENT_MAX_BYTES:
        raise forms.ValidationError(
            _("That file is larger than 10 MB. An acknowledgement should be one page.")
        )

    suffix = Path(str(upload.name)).suffix.lower()
    declared = (getattr(upload, "content_type", "") or "").split(";")[0].strip().lower()
    if suffix not in ACKNOWLEDGEMENT_SUFFIXES or (
        declared and declared not in ACKNOWLEDGEMENT_TYPES
    ):
        raise forms.ValidationError(_("Attach a PDF or a photo (PNG or JPEG)."))
    return upload


class FilingPendingForm(forms.Form):
    """ "Not yet" — why not, and when it will be.

    Both fields are required. "Pending" on its own is what the register already
    knew; the answer only earns its place on the timeline if it says something
    the due date did not.
    """

    pending_reason = forms.CharField(
        label=_("Why is it still pending?"),
        max_length=300,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("Recorded on the timeline, so whoever picks this up knows where it stands."),
    )
    expected_completion_date = forms.DateField(
        label=_("When do you expect it done?"),
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("An estimate. It does not move the statutory due date."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # A picker for "when will this be done" that still opens on last
        # month invites a date that is already wrong the moment it is saved.
        # The `min` attribute is a browser hint, not the guard — a client that
        # ignores it still hits `clean_expected_completion_date` below.
        today = timezone.localdate().isoformat()
        self.fields["expected_completion_date"].widget.attrs["min"] = today

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("pending_reason", "expected_completion_date")

    def clean_expected_completion_date(self) -> date:
        value: date = self.cleaned_data["expected_completion_date"]
        if value < timezone.localdate():
            raise forms.ValidationError(_("Pick today or a later date."))
        return value


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


class CommentForm(forms.Form):
    """A remark on the obligation's timeline — not a state change."""

    note = forms.CharField(
        label=_("Comment"),
        widget=forms.TextInput(attrs={"placeholder": _("Write a comment")}),
        max_length=2000,
    )


class BulkNotApplicableForm(forms.Form):
    """One shared reason behind a bulk "Mark Not Applicable".

    Required at the form level, not just inside ``apply_transition``: a user
    picking several rows at once should see the validation error before
    anything is attempted, not after the first of ten obligations has already
    failed for a reason the other nine share.
    """

    reason = forms.CharField(
        label=_("Reason"),
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Recorded on the timeline and in the audit trail of every obligation chosen."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("reason")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("note")


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
    # The calendar's default landing scope — see `views._filtered`. Overdue,
    # of any age, plus everything else due within 90 days: a work queue, not
    # a fixed lookback/lookahead split, because a backlog does not stop
    # mattering once it is more than ninety days old.
    ("latest", _("Due now (overdue + next 90 days)")),
    ("overdue", _("Overdue")),
    ("due_soon", _("Due in 7 days")),
    # Upcoming only, unlike "latest" above — a planning view of what is
    # coming, deliberately excluding the backlog "latest" already surfaces.
    ("due_30", _("Due in 30 days (upcoming only)")),
    ("due_90", _("Due in 90 days (upcoming only)")),
    ("", _("Everything open")),
    ("pending", _("Upcoming / later")),
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


class LibraryReasonForm(forms.Form):
    """The payload behind removing or force-adding a library definition.

    One form for both actions, the same way :class:`TransitionForm` is one
    form for every lifecycle transition — the guard logic (which state a
    definition has to be in, and whether the engine can actually compute a due
    date) lives in ``stacos.obligations.library``, not duplicated per form.
    """

    reason = forms.CharField(
        label=_("Reason"),
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Recorded on the audit trail."),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout("reason")


#: The three library states, as data so the toolbar, the query builder and the
#: empty state cannot drift out of step — same convention as ``STATUS_FILTERS``.
LIBRARY_STATE_FILTERS: tuple[tuple[str, StrOrPromise], ...] = (
    ("", _("Every state")),
    ("added", _("Added")),
    ("removed", _("Removed")),
    ("not_added", _("Not added yet")),
)

#: The universal status vocabulary (``DisplayStatus``), offered as a filter
#: only over "added" rows — a "removed" or "not added" row has no progress to
#: filter on.
LIBRARY_PROGRESS_FILTERS: tuple[tuple[str, StrOrPromise], ...] = (
    ("", _("Every progress status")),
    ("overdue", _("Overdue")),
    ("due-soon", _("Due soon")),
    ("not-started", _("Not started")),
    ("pending", _("Pending")),
    ("in-progress", _("In progress")),
    ("waiting", _("Waiting")),
    ("complete", _("Complete")),
    ("disputed", _("Disputed")),
)
