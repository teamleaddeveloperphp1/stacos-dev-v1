"""Billing forms.

Amounts are entered in rupees and stored in paise. The conversion happens in one
place — ``clean_amount`` — so a decimal never reaches the database.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from crispy_forms.helper import FormHelper
from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from stacos.billing.models import Payment, to_minor

__all__ = ["PaymentForm"]


class PaymentForm(forms.Form):
    amount = forms.DecimalField(
        label=_("Amount received"),
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    method = forms.ChoiceField(
        choices=[
            (value, label)
            for value, label in Payment.Method.choices
            # Gateway rails record themselves through a webhook; this form is
            # for the ones a human reconciles.
            if value in {Payment.Method.OFFLINE, Payment.Method.UPI, Payment.Method.CREDIT}
        ],
        initial=Payment.Method.OFFLINE,
    )
    external_reference = forms.CharField(
        max_length=120,
        required=False,
        label=_("Reference"),
        help_text=_(
            "UTR, cheque number or transfer reference, so this can be matched to a statement."
        ),
    )
    received_on = forms.DateField(
        required=False, label=_("Received on"), widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["received_on"].initial = timezone.localdate()
        self.helper = FormHelper()
        self.helper.form_tag = False

    @property
    def amount_minor_value(self) -> int:
        return to_minor(self.cleaned_data["amount"])

    def clean(self) -> dict[str, Any]:
        super().clean()
        cleaned = self.cleaned_data
        if "amount" in cleaned:
            # The single place rupees become paise.
            cleaned["amount_minor"] = to_minor(cleaned["amount"])
        return cleaned
