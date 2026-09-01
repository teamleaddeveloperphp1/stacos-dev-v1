"""Forms for information requests."""

from __future__ import annotations

from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout
from django import forms
from django.utils.translation import gettext_lazy as _

from stacos.core.audit import record_event
from stacos.core.forms import ScopedModelChoiceField, ScopedUserChoiceField
from stacos.core.models import AuditAction
from stacos.requests.models import InformationRequest, RequestItem
from stacos.requests.services import log
from stacos.tenancy.models import Entity

__all__ = ["ItemResponseForm", "RejectForm", "RequestForm"]


class RequestForm(forms.ModelForm[InformationRequest]):
    """Raise a request, with its items entered as one per line.

    A formset would be the obvious modelling choice and the wrong interaction:
    somebody listing six documents wants to type six lines. The structure is
    recovered on save, which is where it belongs.
    """

    # Resolved at render and validation time, inside a request. Declaring it
    # here rather than letting ModelForm build it means the class body does not
    # query at import time — and that a forged entity id is re-checked against
    # the caller's scope when the form is cleaned.
    entity = ScopedModelChoiceField(
        Entity, filters={"archived_at__isnull": True}, label=_("Entity")
    )

    # Narrowed to people in the caller's own organisation. Left to ModelForm this
    # is a plain FK to a model that is not tenant-scoped, so it renders every
    # user on the platform and accepts any of them on POST — other customers'
    # staff names, and work assignable to them. See stacos.core.forms.
    assigned_to = ScopedUserChoiceField(required=False, label=_("Ask this person"))

    items_text = forms.CharField(
        label=_("What do you need?"),
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text=_(
            "One per line. Add a '?' at the end of a line to ask for a yes or no instead of a document."
        ),
    )

    class Meta:
        model = InformationRequest
        fields = [
            "entity",
            "title",
            "message",
            "due_on",
            "priority",
            "assigned_to",
            "assigned_email",
        ]
        widgets = {
            "due_on": forms.DateInput(attrs={"type": "date"}),
            "message": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "due_on": _("Needed by"),
            "assigned_to": _("Ask this person"),
            "assigned_email": _("…or this email address"),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            "entity",
            "title",
            "message",
            "items_text",
            "due_on",
            "priority",
            "assigned_to",
            "assigned_email",
        )

    def clean(self) -> dict[str, Any]:
        super().clean()
        cleaned = self.cleaned_data
        if not cleaned.get("assigned_to") and not cleaned.get("assigned_email"):
            raise forms.ValidationError(
                _("Say who should answer this — a request addressed to nobody is never answered.")
            )
        return cleaned

    def clean_items_text(self) -> str:
        lines = [line.strip() for line in self.cleaned_data["items_text"].splitlines()]
        if not [line for line in lines if line]:
            raise forms.ValidationError(_("List at least one thing you need."))
        return self.cleaned_data["items_text"]

    def save(self, commit: bool = True, *, actor: Any = None) -> InformationRequest:
        if not commit:
            raise ValueError(
                f"{type(self).__name__} writes related rows as part of the save, "
                f"which needs a primary key. commit=False is not supported."
            )
        instance: InformationRequest = super().save(commit=False)
        instance.tenant = instance.entity.tenant
        instance.requested_by = actor if getattr(actor, "is_authenticated", False) else None
        instance.save()

        for ordinal, line in enumerate(
            line.strip() for line in self.cleaned_data["items_text"].splitlines()
        ):
            if not line:
                continue
            RequestItem.objects.create(
                tenant=instance.tenant,
                entity=instance.entity,
                request=instance,
                label=line[:250],
                # A line ending in a question mark is a question, not a document
                # request. Small heuristic, and it saves a field on every row.
                kind=(
                    RequestItem.Kind.CONFIRMATION
                    if line.endswith("?")
                    else RequestItem.Kind.DOCUMENT
                ),
                ordinal=ordinal,
            )

        log(instance, kind="CREATED", actor=actor, note=instance.title)
        record_event(
            action=AuditAction.CREATE,
            actor=actor,
            obj=instance,
            after={"title": instance.title, "items": instance.items.count()},
        )
        return instance


class ItemResponseForm(forms.Form):
    """Answer one item.

    Document items answer by attaching a file through the vault, so this form
    carries no field for them — a text box beside an upload control invites
    somebody to type "attached" and consider it answered.
    """

    value = forms.CharField(required=False, max_length=500, label=_("Answer"))

    def __init__(self, *args: Any, item: RequestItem | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.item = item
        if item is not None and item.kind == RequestItem.Kind.DOCUMENT:
            del self.fields["value"]
        elif item is not None and item.kind == RequestItem.Kind.CONFIRMATION:
            self.fields["value"] = forms.ChoiceField(
                choices=(("YES", _("Yes")), ("NO", _("No"))), label=item.label
            )


class RejectForm(forms.Form):
    reason = forms.CharField(
        max_length=250,
        label=_("What was wrong with it?"),
        widget=forms.TextInput(attrs={"placeholder": _("The client will see this.")}),
    )
