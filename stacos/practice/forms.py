"""Forms for the practice board and time sheet."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from crispy_forms.helper import FormHelper
from django import forms
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.core.audit import record_event
from stacos.core.forms import ScopedUserChoiceField
from stacos.core.models import AuditAction
from stacos.practice.models import RateCard, TimeEntry, WorkItem

__all__ = ["TimeEntryForm", "WorkItemForm", "rate_for"]


def rate_for(user: Any, *, on: date) -> Decimal:
    """The charge-out rate in force for a person on a date.

    Looked up by date rather than read from a current rate, so a rise in April
    does not silently reprice work done in March.
    """
    card = (
        RateCard.objects.filter(user=user, valid_from__lte=on)
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=on))
        .order_by("-valid_from")
        .first()
    )
    return card.rate if card else Decimal("0")


class WorkItemForm(forms.ModelForm[WorkItem]):
    # Narrowed to people in the caller's own organisation. Left to ModelForm this
    # is a plain FK to a model that is not tenant-scoped, so it renders every
    # user on the platform and accepts any of them on POST — other customers'
    # staff names, and work assignable to them. See stacos.core.forms.
    assigned_to = ScopedUserChoiceField(required=False, label=_("Assign to"))
    reviewer = ScopedUserChoiceField(required=False, label=_("Reviewer"))

    class Meta:
        model = WorkItem
        fields = [
            "title",
            "description",
            "priority",
            "due_on",
            "assigned_to",
            "reviewer",
            "estimated_hours",
            "fixed_fee",
        ]
        widgets = {
            "due_on": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "fixed_fee": _("Leave blank to bill the time at charge-out rates."),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False

    def save(self, commit: bool = True, *, actor: Any = None) -> WorkItem:
        if not commit:
            raise ValueError("WorkItemForm always persists; commit=False is not supported.")

        item: WorkItem = super().save(commit=False)
        # The practice's own tenant, taken from the bound scope rather than from
        # the form: a work item is the firm's internal record and must never be
        # created against a client's tenant.
        from stacos.core.scope import require_scope

        scope = require_scope()
        if scope.principal_tenant_id is None:
            raise ValueError(
                "No principal tenant is bound. A work item belongs to the firm that "
                "raised it, and there is no sensible default."
            )
        item.tenant_id = scope.principal_tenant_id
        if item.assigned_to_id and item.state == "BACKLOG":
            item.state = "ASSIGNED"
        item.save()

        record_event(action=AuditAction.CREATE, actor=actor, obj=item, after={"title": item.title})
        return item


class TimeEntryForm(forms.Form):
    worked_on = forms.DateField(label=_("Date"), widget=forms.DateInput(attrs={"type": "date"}))
    hours = forms.DecimalField(
        label=_("Hours"), max_digits=5, decimal_places=2, min_value=Decimal("0.01")
    )
    narrative = forms.CharField(
        max_length=300,
        required=False,
        label=_("What was done"),
        help_text=_("Appears on the client's invoice."),
    )
    is_billable = forms.BooleanField(required=False, initial=True, label=_("Billable"))

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["worked_on"].initial = timezone.localdate()
        self.helper = FormHelper()
        self.helper.form_tag = False

    def save(self, *, work_item: WorkItem, actor: Any) -> TimeEntry:
        """Write the entry with the rate frozen at the date worked."""
        worked_on = self.cleaned_data["worked_on"]
        billable = self.cleaned_data.get("is_billable", False)
        rate = rate_for(actor, on=worked_on) if billable else Decimal("0")

        return TimeEntry.objects.create(
            tenant_id=work_item.tenant_id,
            work_item=work_item,
            client_tenant_id=work_item.client_tenant_id,
            user=actor,
            worked_on=worked_on,
            hours=self.cleaned_data["hours"],
            rate=rate,
            # A billable hour with no rate is a hole in the WIP number that
            # nobody notices until month end, and the database refuses it — so
            # an unrated person's time is recorded as non-billable rather than
            # lost.
            is_billable=billable and rate > 0,
            narrative=self.cleaned_data.get("narrative", ""),
        )
