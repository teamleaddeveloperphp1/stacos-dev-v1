"""
The enquiry form.

The only unauthenticated write on the public surface, which makes it the only
part of the marketing site with a threat model. Three defences, none of which
inconveniences a real person:

* a **honeypot** field that a browser never fills and a bot usually does;
* a **minimum fill time**, because a form submitted 1.4 seconds after it was
  rendered was not typed;
* a **per-IP rate limit**, applied in the view.

Deliberately not a model. An enquiry is an email to a human inbox; giving it a
table would create an unscoped, unauthenticated write path into the database for
the sake of data nobody would query.
"""

from __future__ import annotations

import time
from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Layout, Row, Submit
from django import forms
from django.utils.translation import gettext_lazy as _

__all__ = ["ContactForm"]

#: A human cannot read the page, decide, and type a message in under this many
#: seconds. Generous, because a person pasting a prepared message is real.
MIN_FILL_SECONDS = 3

TOPICS = (
    ("sales", _("Talk to sales")),
    ("demo", _("Request a demonstration")),
    ("support", _("Existing customer support")),
    ("security", _("Security or procurement questionnaire")),
    ("partner", _("Partnership or reselling")),
    ("press", _("Press")),
    ("other", _("Something else")),
)

SIZES = (
    ("", _("Select one")),
    ("1", _("One entity")),
    ("2-5", _("2–5 entities")),
    ("6-25", _("6–25 entities")),
    ("25+", _("More than 25 entities")),
    ("practice", _("Professional firm")),
)


class ContactForm(forms.Form):
    topic = forms.ChoiceField(label=_("What is this about?"), choices=TOPICS, initial="sales")
    name = forms.CharField(label=_("Your name"), max_length=120)
    email = forms.EmailField(label=_("Work email"))
    phone = forms.CharField(label=_("Phone"), max_length=32, required=False)
    organisation = forms.CharField(label=_("Organisation"), max_length=160, required=False)
    size = forms.ChoiceField(label=_("How many entities?"), choices=SIZES, required=False)
    message = forms.CharField(
        label=_("Message"),
        widget=forms.Textarea(attrs={"rows": 5}),
        max_length=4000,
        help_text=_("What are you trying to do? The more specific, the better the reply."),
    )

    # --- Anti-spam. Neither field is ever shown to a person. ---
    website = forms.CharField(required=False, widget=forms.HiddenInput)
    rendered_at = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["rendered_at"].initial = str(int(time.time()))
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.attrs = {"novalidate": "novalidate"}
        self.helper.layout = Layout(
            "topic",
            Row(Column("name"), Column("email")),
            Row(Column("phone"), Column("organisation")),
            "size",
            "message",
            "website",
            "rendered_at",
        )
        self.helper.add_input(
            Submit("submit", _("Send enquiry"), css_class="btn-primary btn-lg mt-2")
        )

    def clean_website(self) -> str:
        """The honeypot. Filled means a bot; the error is deliberately vague."""
        if self.cleaned_data.get("website"):
            raise forms.ValidationError(_("This enquiry could not be sent."))
        return ""

    def clean_rendered_at(self) -> str:
        stamp = self.cleaned_data.get("rendered_at") or ""
        if not stamp.isdigit():
            # An absent or mangled stamp is not by itself evidence of a bot —
            # a restored tab or a stripped field looks the same — so it is
            # tolerated rather than rejected.
            return ""
        if time.time() - int(stamp) < MIN_FILL_SECONDS:
            raise forms.ValidationError(_("This enquiry could not be sent."))
        return stamp

    def as_email_body(self) -> str:
        data = self.cleaned_data
        lines = [
            f"Topic:        {dict(TOPICS).get(data['topic'], data['topic'])}",
            f"Name:         {data['name']}",
            f"Email:        {data['email']}",
            f"Phone:        {data.get('phone') or '—'}",
            f"Organisation: {data.get('organisation') or '—'}",
            f"Size:         {data.get('size') or '—'}",
            "",
            data["message"],
        ]
        return "\n".join(lines)
