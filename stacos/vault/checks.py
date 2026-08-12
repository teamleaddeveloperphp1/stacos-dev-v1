"""
Deployment checks for the vault.

The one below exists because of a specific, extremely ordinary failure: the
development scanner is the default, it makes uploads work immediately on a
laptop, and nothing about running it in production *looks* wrong. Every file
passes, every download works, and the antivirus feature is decorative. There is
no error message and no symptom until the day it matters.

So it is a check, and `manage.py check --deploy` is a CI gate.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, Warning, register

__all__ = ["check_vault_scanner"]


@register(deploy=True)
def check_vault_scanner(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    provider = str(getattr(settings, "VAULT_SCANNER", {}).get("PROVIDER", "development")).lower()
    messages: list[CheckMessage] = []

    if provider in {"development", "memory"} and not settings.DEBUG:
        messages.append(
            Error(
                f"VAULT_SCANNER['PROVIDER'] is {provider!r} with DEBUG off. "
                f"Uploaded files would be marked clean without being scanned.",
                hint=(
                    "Set VAULT_SCANNER=clamav and point CLAMAV_HOST/CLAMAV_PORT at a "
                    "clamd the worker can reach. If this deployment genuinely has no "
                    "scanner, that is a decision to take explicitly and document — "
                    "not one to inherit from a default."
                ),
                id="stacos.vault.E001",
            )
        )

    if provider == "clamav":
        limit = int(getattr(settings, "VAULT_SCANNER", {}).get("MAX_BYTES", 0))
        upload_cap = getattr(settings, "DATA_UPLOAD_MAX_MEMORY_SIZE", 0) or 0
        if limit and upload_cap and limit < upload_cap:
            messages.append(
                Warning(
                    f"The scanner accepts {limit} bytes but uploads up to {upload_cap} "
                    f"are allowed, so the largest files can never be scanned.",
                    hint=(
                        "Raise CLAMAV_MAX_BYTES and clamd's own StreamMaxLength together, "
                        "or lower the upload limit. They must agree."
                    ),
                    id="stacos.vault.W001",
                )
            )

    return messages
