"""
WhatsApp delivery, via the Meta WhatsApp Business Cloud API.

STACOS sends its second verification channel over WhatsApp rather than SMS. For
Indian businesses that is the channel people actually read — an SMS competes with
a hundred promotional messages a day, and a WhatsApp message does not.

Three constraints shape this interface, all of them external:

* **Business-initiated messages must use a pre-approved template.** Free-form text
  is only allowed inside the 24-hour window opened by a message *from* the user.
  Registration and sign-in are always business-initiated, so ``template_name`` is
  part of the interface rather than a vendor detail.
* **One-time passcodes must use the AUTHENTICATION template category.** Meta
  rejects OTPs sent through marketing or utility templates, and an authentication
  template renders with a copy-code button and its own tamper warning — which is
  both better UX and the only compliant option.
* **Authentication templates take positional parameters only.** The code is
  ``{{1}}``; there is no named substitution to make the payload self-describing.

Template approval takes days to weeks and is per WhatsApp Business Account.
Start it early; it is the WhatsApp analogue of India's DLT registration for SMS
and blocks exactly the same way.

**The gap worth knowing about:** a phone number that is not registered on
WhatsApp cannot receive anything here. That is rare for an Indian business
contact but not impossible, and it is why :class:`WhatsAppResult` distinguishes
"not on WhatsApp" from a transport failure — so a fallback channel can be added
later without changing any caller.
"""

from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar
from urllib import error as urllib_error
from urllib import request as urllib_request

import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

__all__ = [
    "ConsoleWhatsAppProvider",
    "MemoryWhatsAppProvider",
    "MetaCloudProvider",
    "WhatsAppMessage",
    "WhatsAppProvider",
    "WhatsAppResult",
    "WhatsAppTemplate",
    "get_whatsapp_provider",
]


@dataclass(frozen=True, slots=True)
class WhatsAppTemplate:
    """A message template registered with, and approved by, Meta.

    ``body`` is kept beside the registered name so the console backend can show
    what a user would actually receive, and so tests can assert on content
    without a network call. It must stay in step with what was approved — Meta
    renders its own copy, not this one.
    """

    key: str
    #: The name registered in the WhatsApp Business Account.
    template_name: str
    #: AUTHENTICATION for one-time passcodes; UTILITY for transactional notices.
    category: str
    body: str
    language: str = "en"


#: Every template STACOS sends. Each needs approval before it will deliver.
TEMPLATES: dict[str, WhatsAppTemplate] = {
    "otp_registration": WhatsAppTemplate(
        key="otp_registration",
        template_name="stacos_registration_code",
        category="AUTHENTICATION",
        body="{code} is your STACOS registration code. It expires in {minutes} minutes.",
    ),
    "otp_login": WhatsAppTemplate(
        key="otp_login",
        template_name="stacos_login_code",
        category="AUTHENTICATION",
        body=(
            "{code} is your STACOS verification code. It expires in {minutes} minutes. "
            "Do not share it with anyone."
        ),
    ),
    "otp_step_up": WhatsAppTemplate(
        key="otp_step_up",
        template_name="stacos_step_up_code",
        category="AUTHENTICATION",
        body=(
            "{code} is your STACOS code to confirm a sensitive action. "
            "If this was not you, sign out of all devices immediately."
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class WhatsAppMessage:
    to_e164: str
    template_key: str
    params: dict[str, str] = field(default_factory=dict)

    @property
    def template(self) -> WhatsAppTemplate:
        return TEMPLATES[self.template_key]

    def render(self) -> str:
        """The message as the recipient will see it. Used for dev and tests."""
        return self.template.body.format(**self.params)

    def positional_parameters(self) -> list[str]:
        """Ordered parameters for the template body.

        WhatsApp authentication templates accept exactly one parameter — the
        code — and substitute it positionally as ``{{1}}``.
        """
        return [self.params["code"]] if "code" in self.params else []


@dataclass(frozen=True, slots=True)
class WhatsAppResult:
    provider: str
    provider_message_id: str = ""
    cost_units: Decimal = Decimal("0")
    accepted: bool = True
    error: str = ""
    #: True when the number simply has no WhatsApp account, as opposed to the
    #: send having failed. A different problem needing a different remedy, so it
    #: is reported separately rather than folded into `error`.
    not_on_whatsapp: bool = False


class WhatsAppProvider(abc.ABC):
    """Anything that can deliver a WhatsApp message."""

    name: ClassVar[str] = "abstract"

    @abc.abstractmethod
    def send(self, message: WhatsAppMessage) -> WhatsAppResult:
        """Deliver one message. Must not raise; report failure in the result."""


class ConsoleWhatsAppProvider(WhatsAppProvider):
    """Development backend: prints the message instead of sending it.

    Deliberately unmissable in a dev console, so the verification flow can be
    exercised without a WhatsApp Business Account or a second device.
    """

    name = "console"

    def send(self, message: WhatsAppMessage) -> WhatsAppResult:
        body = message.render()
        logger.info(
            "whatsapp.console",
            to=message.to_e164,
            template=message.template.template_name,
            body=body,
        )
        print(f"\n  ── WhatsApp to {message.to_e164} ──\n  {body}\n")  # noqa: T201
        return WhatsAppResult(provider=self.name, provider_message_id="console")


class MemoryWhatsAppProvider(WhatsAppProvider):
    """Test backend: collects messages so tests can read the codes back."""

    name = "memory"
    outbox: ClassVar[list[WhatsAppMessage]] = []

    def send(self, message: WhatsAppMessage) -> WhatsAppResult:
        self.outbox.append(message)
        return WhatsAppResult(provider=self.name, provider_message_id=f"mem-{len(self.outbox)}")

    @classmethod
    def clear(cls) -> None:
        cls.outbox.clear()

    @classmethod
    def last_for(cls, to_e164: str) -> WhatsAppMessage | None:
        for message in reversed(cls.outbox):
            if message.to_e164 == to_e164:
                return message
        return None


class MetaCloudProvider(WhatsAppProvider):
    """The real thing: Meta's WhatsApp Business Cloud API.

    Uses ``urllib`` rather than adding an HTTP client dependency — this is one
    POST with a JSON body, and the retry policy that would justify a heavier
    client belongs to Celery, which already owns it.

    Never raises. A verification flow that 500s because Meta had a bad minute is
    worse than one that reports "we could not reach WhatsApp" and offers a retry.
    """

    name = "meta"
    timeout_seconds = 10

    def send(self, message: WhatsAppMessage) -> WhatsAppResult:
        config = settings.WHATSAPP
        token = config.get("ACCESS_TOKEN", "")
        phone_number_id = config.get("PHONE_NUMBER_ID", "")

        if not token or not phone_number_id:
            return WhatsAppResult(
                provider=self.name,
                accepted=False,
                error="WhatsApp is not configured (ACCESS_TOKEN / PHONE_NUMBER_ID missing).",
            )

        template = message.template
        payload = {
            "messaging_product": "whatsapp",
            # The API wants the number without a leading '+'.
            "to": message.to_e164.lstrip("+"),
            "type": "template",
            "template": {
                "name": template.template_name,
                "language": {"code": template.language},
                "components": self._components(message),
            },
        }

        url = (
            f"https://graph.facebook.com/{config.get('API_VERSION', 'v21.0')}"
            f"/{phone_number_id}/messages"
        )
        request = urllib_request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib_request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                body = json.loads(response.read().decode())
        except urllib_error.HTTPError as exc:
            return self._from_http_error(exc)
        except Exception as exc:
            logger.warning("whatsapp.transport_error", error=str(exc))
            return WhatsAppResult(provider=self.name, accepted=False, error=str(exc))

        message_id = ""
        messages = body.get("messages") or []
        if messages:
            message_id = str(messages[0].get("id", ""))

        return WhatsAppResult(
            provider=self.name,
            provider_message_id=message_id,
            # Authentication conversations are billed per message; the real
            # figure comes from the billing webhook, so this is a placeholder
            # the spend ledger reconciles against later.
            cost_units=Decimal(str(settings.WHATSAPP.get("COST_PER_MESSAGE", "0.125"))),
        )

    def _components(self, message: WhatsAppMessage) -> list[dict[str, Any]]:
        """Build the template components.

        An authentication template carries the code twice: once in the body and
        once on the copy-code button, and Meta rejects the message if the two do
        not agree.
        """
        parameters = [{"type": "text", "text": value} for value in message.positional_parameters()]
        if not parameters:
            return []

        components: list[dict[str, Any]] = [{"type": "body", "parameters": parameters}]

        if message.template.category == "AUTHENTICATION":
            components.append(
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [{"type": "text", "text": parameters[0]["text"]}],
                }
            )

        return components

    def _from_http_error(self, exc: urllib_error.HTTPError) -> WhatsAppResult:
        try:
            detail = json.loads(exc.read().decode()).get("error", {})
        except Exception:
            detail = {}

        code = detail.get("code")
        message = str(detail.get("message", exc.reason))

        # 131026: the recipient has no WhatsApp account, or cannot receive the
        # message. Distinct from a failure on our side, and the one case where a
        # different channel would help.
        if code == 131026:
            logger.info("whatsapp.recipient_unreachable", detail=message)
            return WhatsAppResult(
                provider=self.name,
                accepted=False,
                not_on_whatsapp=True,
                error="This number does not appear to have WhatsApp.",
            )

        logger.warning("whatsapp.api_error", code=code, detail=message)
        return WhatsAppResult(provider=self.name, accepted=False, error=message)


_PROVIDERS: dict[str, type[WhatsAppProvider]] = {
    "console": ConsoleWhatsAppProvider,
    "memory": MemoryWhatsAppProvider,
    "meta": MetaCloudProvider,
}


def get_whatsapp_provider() -> WhatsAppProvider:
    """Return the configured provider."""
    key = settings.WHATSAPP.get("PROVIDER", "console")
    try:
        return _PROVIDERS[key]()
    except KeyError:
        raise ValueError(
            f"Unknown WhatsApp provider {key!r}. Choose one of: {', '.join(sorted(_PROVIDERS))}."
        ) from None
