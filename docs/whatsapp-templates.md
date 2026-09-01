# WhatsApp templates

Every message STACOS sends on WhatsApp is **business-initiated**, and Meta will
not deliver a business-initiated message unless the template was approved in
advance, by name, for that WhatsApp Business Account.

Approval takes **days to weeks**. It is the WhatsApp analogue of DLT
registration for SMS and it blocks in exactly the same way: the code is finished,
the tests pass, and nothing arrives on anybody's phone. Start it early.

This file is the checklist. The templates themselves are declared in code —
`stacos/accounts/whatsapp.py` for authentication and
`stacos/notifications/templates_registry.py` for reminders — and the `body` kept
beside each registered name is what the console provider prints and what the
tests assert on. **Meta renders its own approved copy, not ours**, so if the two
drift, the copy in this repository is the one that is wrong.

## Before you start

1. A verified Meta Business Account.
2. A WhatsApp Business Account (WABA) with a registered phone number. The number
   cannot be one already in use by the consumer WhatsApp app.
3. A system user access token with `whatsapp_business_messaging` and
   `whatsapp_business_management`.

Set these in the environment; nothing here is a code change:

```
WHATSAPP_PROVIDER=meta
WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_BUSINESS_ACCOUNT_ID=...
```

Until then `WHATSAPP_PROVIDER=console` prints codes and messages to the terminal,
which is what development runs on.

## The categories matter

Meta rejects a template submitted under the wrong category, and the rejection
usually arrives days later.

| Category | What it is for | Ours |
|---|---|---|
| `AUTHENTICATION` | One-time passcodes only. Renders with a copy-code button and its own tamper warning. | Sign-in, registration and step-up codes. |
| `UTILITY` | A transactional message about something the customer is already engaged in. | Every reminder: filings, information requests, notice deadlines, overdue invoices. |
| `MARKETING` | Promotion. Subject to the user's marketing opt-out. | **None.** A statutory deadline must never be sent as marketing — an opt-out would silence it. |

Two rules that catch people out:

* An OTP sent through a utility or marketing template is **rejected**. It must be
  `AUTHENTICATION`.
* Authentication templates take **positional parameters only** — `{{1}}`,
  `{{2}}`. There is no named substitution, which is why the payload in
  `whatsapp.py` looks the way it does.

## The templates to register

Submit each of these in WhatsApp Manager → Account tools → Message templates. The
**name** column is what must match exactly; the code sends by name.

### Authentication — `stacos/accounts/whatsapp.py`

| Name | Purpose |
|---|---|
| `stacos_registration_code` | Code sent while creating an account. |
| `stacos_login_code` | Code sent when signing in from a new device. |
| `stacos_step_up_code` | Code sent to confirm a sensitive action. |

### Utility — `stacos/notifications/templates_registry.py`

| Name | Sent when |
|---|---|
| `stacos_filing_due` | A filing is approaching its due date. |
| `stacos_filing_overdue` | A filing has passed its due date and is still open. |
| `stacos_information_requested` | Documents have been requested from a client. |
| `stacos_information_reminder` | Items on a request are still outstanding. |
| `stacos_notice_deadline` | A response to an authority's notice is due. |
| `stacos_invoice_overdue` | A subscription invoice has passed its due date. |

A notification kind absent from that registry is **never sent over WhatsApp** —
the delivery row records `SUPPRESSED` with the reason, so "why did the client not
get a WhatsApp" is answerable without reading the source. Adding a kind to the
channel means obtaining a new approval first, and carrying its per-message cost
afterwards.

## After approval

* Confirm each template shows **Active** in WhatsApp Manager. A template in
  review will not deliver.
* Send one of each to a test number with `WHATSAPP_PROVIDER=meta`.
* Watch `WhatsAppResult.not_on_whatsapp` in the logs. A number with no WhatsApp
  account cannot be reached at all — that is reported separately from a delivery
  failure precisely so a fallback channel can be added later without touching
  any caller. **No fallback exists yet.**

## Cost

Utility conversations are charged per conversation, per country. The daily cap
(`WHATSAPP_DAILY_SPEND_CAP_UNITS`) exists so that a bug in a reminder sweep is a
capped expense rather than an open-ended one. Reconcile
`WHATSAPP_COST_PER_MESSAGE` against Meta's billing periodically; it is indicative
and used only for the cap.

WhatsApp is **off by default** in a user's notification preferences for this
reason. Email costs nothing and WhatsApp does; making a customer opt in is both
the polite default and the one that does not surprise them with a bill.
