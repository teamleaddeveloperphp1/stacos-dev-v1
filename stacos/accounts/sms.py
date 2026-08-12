"""
Pluggable SMS delivery.

``template_id`` is part of the interface rather than a vendor detail, because of
one hard India constraint: under TRAI's DLT regime every commercial SMS to an
Indian number must be sent under a **pre-registered sender header** *and* a
**pre-registered content template**, with that template's id passed to the
aggregator. Unregistered traffic is discarded by the operator, so this cannot be
worked around at send time by switching vendors.

Registration is a multi-week external process. It should be started in parallel
with development, not when the code is ready to test.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar

import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

__all__ = [
    "ConsoleSmsProvider",
    "MemorySmsProvider",
    "SmsMessage",
    "SmsProvider",
    "SmsResult",
    "SmsTemplate",
    "get_sms_provider",
]


@dataclass(frozen=True, slots=True)
class SmsTemplate:
    """A DLT-registered message template.

    ``body`` is kept alongside the registered id so the console backend can show
    what a user would actually receive, and so tests can assert on content.
    """

    key: str
    body: str
    dlt_template_id: str = ""


TEMPLATES: dict[str, SmsTemplate] = {
    "otp_login": SmsTemplate(
        key="otp_login",
        body="{code} is your STACOS verification code. It expires in {minutes} minutes. "
        "Do not share it with anyone.",
    ),
    "otp_registration": SmsTemplate(
        key="otp_registration",
        body="{code} is your STACOS registration code, valid for {minutes} minutes.",
    ),
    "otp_step_up": SmsTemplate(
        key="otp_step_up",
        body="{code} is your STACOS code to confirm a sensitive action. "
        "If this was not you, sign out of all devices immediately.",
    ),
}


@dataclass(frozen=True, slots=True)
class SmsMessage:
    to_e164: str
    template_key: str
    params: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        template = TEMPLATES[self.template_key]
        return template.body.format(**self.params)


@dataclass(frozen=True, slots=True)
class SmsResult:
    provider: str
    provider_message_id: str = ""
    cost_units: Decimal = Decimal("0")
    accepted: bool = True
    error: str = ""


class SmsProvider(abc.ABC):
    """Anything that can deliver an SMS."""

    name: ClassVar[str] = "abstract"

    @abc.abstractmethod
    def send(self, message: SmsMessage) -> SmsResult:
        """Deliver one message. Must not raise; report failure in the result."""


class ConsoleSmsProvider(SmsProvider):
    """Development backend: logs the message instead of sending it.

    Prints the rendered body prominently so the code can be copied while
    developing the verification flow without a real aggregator or a real phone.
    """

    name = "console"

    def send(self, message: SmsMessage) -> SmsResult:
        body = message.render()
        logger.info(
            "sms.console",
            to=message.to_e164,
            template=message.template_key,
            body=body,
        )
        # Deliberately unmissable in a dev console.
        print(f"\n  ── SMS to {message.to_e164} ──\n  {body}\n")  # noqa: T201
        return SmsResult(provider=self.name, provider_message_id="console", cost_units=Decimal("0"))


class MemorySmsProvider(SmsProvider):
    """Test backend: collects messages for assertions."""

    name = "memory"
    outbox: ClassVar[list[SmsMessage]] = []

    def send(self, message: SmsMessage) -> SmsResult:
        self.outbox.append(message)
        return SmsResult(provider=self.name, provider_message_id=f"mem-{len(self.outbox)}")

    @classmethod
    def clear(cls) -> None:
        cls.outbox.clear()

    @classmethod
    def last_for(cls, to_e164: str) -> SmsMessage | None:
        for message in reversed(cls.outbox):
            if message.to_e164 == to_e164:
                return message
        return None


class Msg91Provider(SmsProvider):
    """MSG91 — an Indian aggregator with DLT support.

    Not implemented in the foundation: it needs live credentials and approved DLT
    templates. The interface is fixed so the switch is a settings change.
    """

    name = "msg91"

    def send(self, message: SmsMessage) -> SmsResult:  # pragma: no cover
        raise NotImplementedError(
            "MSG91 delivery needs an auth key and DLT-approved template ids. "
            "Set SMS_PROVIDER=console until TRAI DLT registration completes."
        )


class TwilioProvider(SmsProvider):
    """Twilio — used for international numbers, where DLT does not apply."""

    name = "twilio"

    def send(self, message: SmsMessage) -> SmsResult:  # pragma: no cover
        raise NotImplementedError(
            "Twilio delivery needs account credentials. Set SMS_PROVIDER=console in development."
        )


_PROVIDERS: dict[str, type[SmsProvider]] = {
    "console": ConsoleSmsProvider,
    "memory": MemorySmsProvider,
    "msg91": Msg91Provider,
    "twilio": TwilioProvider,
}


def get_sms_provider() -> SmsProvider:
    """Return the configured provider."""
    key = getattr(settings, "SMS_PROVIDER", "console")
    try:
        return _PROVIDERS[key]()
    except KeyError:
        raise ValueError(
            f"Unknown SMS_PROVIDER {key!r}. Choose one of: {', '.join(sorted(_PROVIDERS))}."
        ) from None
