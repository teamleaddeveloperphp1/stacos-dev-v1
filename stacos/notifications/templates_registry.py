"""
The WhatsApp templates reminders need, and the approval each one is waiting on.

This file is mostly a lead-time warning in code form. Business-initiated
WhatsApp messages — which every reminder is, by definition — will not deliver
until Meta has approved a template *by name*, per WhatsApp Business Account, and
approval takes days to weeks. Discovering that during launch week is the failure
this file exists to prevent, which is why the templates are declared here in full
before anything sends them.

Two rules from the platform shape the wording:

* Reminders are **UTILITY**, not MARKETING and not AUTHENTICATION. Meta rejects a
  reminder sent as an authentication template, and a marketing template is
  subject to the user's marketing opt-out — which a statutory deadline should
  not be.
* Parameters are positional. ``{1}``, ``{2}``, in order, with no names. The
  ``body`` below keeps a readable copy beside the registered name so the console
  provider can show what a user would see and so tests can assert on it.

Nothing here reaches Meta on its own. Registration is a manual step in the
WhatsApp Manager, and `docs/whatsapp-templates.md` is the checklist for it.
"""

from __future__ import annotations

from stacos.accounts.whatsapp import TEMPLATES, WhatsAppTemplate
from stacos.notifications.models import NotificationKind

__all__ = ["REMINDER_TEMPLATES", "template_key_for"]

#: One template per kind we are willing to send over WhatsApp. Kinds absent from
#: this map are never sent on that channel — deliberately, because every entry
#: here is a separate approval to obtain and a per-message cost to carry.
REMINDER_TEMPLATES: dict[str, WhatsAppTemplate] = {
    NotificationKind.OBLIGATION_DUE: WhatsAppTemplate(
        key="reminder_obligation_due",
        template_name="stacos_filing_due",
        category="UTILITY",
        body="{entity}: {obligation} is due on {date}. Open STACOS to see what is outstanding.",
    ),
    NotificationKind.OBLIGATION_OVERDUE: WhatsAppTemplate(
        key="reminder_obligation_overdue",
        template_name="stacos_filing_overdue",
        category="UTILITY",
        body="{entity}: {obligation} was due on {date} and has not been filed.",
    ),
    NotificationKind.REQUEST_SENT: WhatsAppTemplate(
        key="reminder_request_sent",
        template_name="stacos_information_requested",
        category="UTILITY",
        body="{entity}: {count} document(s) have been requested from you, needed by {date}.",
    ),
    NotificationKind.REQUEST_REMINDER: WhatsAppTemplate(
        key="reminder_request_followup",
        template_name="stacos_information_reminder",
        category="UTILITY",
        body="{entity}: {count} item(s) are still outstanding. They were needed by {date}.",
    ),
    NotificationKind.NOTICE_DEADLINE: WhatsAppTemplate(
        key="reminder_notice_deadline",
        template_name="stacos_notice_deadline",
        category="UTILITY",
        body="{entity}: the response to {reference} is due on {date}.",
    ),
    NotificationKind.INVOICE_OVERDUE: WhatsAppTemplate(
        key="reminder_invoice_overdue",
        template_name="stacos_invoice_overdue",
        category="UTILITY",
        body="Invoice {reference} for {amount} was due on {date}.",
    ),
}

# Registered into the shared table the provider reads, so the account's whole
# template inventory — authentication and utility alike — is enumerable in one
# place when somebody has to reconcile it against the WhatsApp Manager.
TEMPLATES.update({template.key: template for template in REMINDER_TEMPLATES.values()})


def template_key_for(kind: str) -> str | None:
    """The approved template for a kind, or ``None`` if WhatsApp is not used for it."""
    template = REMINDER_TEMPLATES.get(kind)
    return template.key if template else None
