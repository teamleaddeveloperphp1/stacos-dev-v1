"""Housekeeping tasks for identity data."""

from __future__ import annotations

from datetime import timedelta

import structlog
from celery import shared_task
from django.utils import timezone

logger = structlog.get_logger(__name__)


@shared_task(name="stacos.accounts.purge_expired_verifications")
def purge_expired_verifications() -> int:
    """Mark timed-out verifications expired and delete very old rows.

    Rows are retained for a week after expiry: a support question about "I never
    got the code" is unanswerable once the record is gone.
    """
    from stacos.accounts.models import PendingVerification

    now = timezone.now()
    expired = PendingVerification.objects.filter(
        status=PendingVerification.Status.PENDING, expires_at__lt=now
    ).update(status=PendingVerification.Status.EXPIRED)

    deleted, _ = PendingVerification.objects.filter(created_at__lt=now - timedelta(days=7)).delete()

    logger.info("accounts.purge_verifications", expired=expired, deleted=deleted)
    return expired


@shared_task(name="stacos.accounts.purge_expired_trusted_devices")
def purge_expired_trusted_devices() -> int:
    """Remove device trust records that are long past their window."""
    from stacos.accounts.models import TrustedDevice

    cutoff = timezone.now() - timedelta(days=30)
    deleted, _ = TrustedDevice.objects.filter(expires_at__lt=cutoff).delete()
    logger.info("accounts.purge_trusted_devices", deleted=deleted)
    return deleted


@shared_task(name="stacos.accounts.roll_message_spend_ledger")
def roll_message_spend_ledger() -> None:
    """Open today's ledger rows so the spend cap has something to read."""
    from stacos.accounts.models import MessageSpendLedger
    from stacos.accounts.whatsapp import _PROVIDERS

    today = timezone.localdate()
    for provider in _PROVIDERS:
        MessageSpendLedger.objects.get_or_create(
            day=today, channel=MessageSpendLedger.Channel.WHATSAPP, provider=provider
        )
