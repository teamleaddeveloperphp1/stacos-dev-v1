"""
Celery topology.

Queues are named and separated so that nothing starves the user-facing path.
Reminders are latency-sensitive; billing must never be dropped. Each gets its
own worker.

Two rules that apply to every task in this project:

  * `acks_late` guarantees at-least-once delivery, so exactly-once exists only if
    the task itself is idempotent. `stacos.core.tasks.TenantTask` provides an
    idempotency key backed by a unique constraint.
  * A queued task must never inherit ambient authority. Tenant-touching tasks
    take `tenant_id` explicitly and re-derive their own scope inside the task.
"""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("stacos")
app.config_from_object("django.conf:settings", namespace="CELERY")

QUEUES = (
    "default",
    "reminders",
    "materialise",
    "billing",
    "exports",
)

app.conf.update(
    task_default_queue="default",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=60 * 60 * 24 * 7,
    broker_connection_retry_on_startup=True,
    # Crontabs are expressed in IST because Indian statutory reporting means IST
    # when it says "a day"; messages themselves stay UTC.
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_routes={
        "stacos.notifications.*": {"queue": "reminders"},
        "stacos.obligations.materialise*": {"queue": "materialise"},
        "stacos.billing.*": {"queue": "billing"},
        "stacos.core.export*": {"queue": "exports"},
    },
    task_annotations={
        "*": {"time_limit": 900, "soft_time_limit": 840},
    },
)

app.conf.beat_schedule = {
    "purge-expired-verifications": {
        "task": "stacos.accounts.purge_expired_verifications",
        "schedule": crontab(minute="*/15"),
    },
    "purge-expired-trusted-devices": {
        "task": "stacos.accounts.purge_expired_trusted_devices",
        "schedule": crontab(hour=3, minute=0),
    },
    "reset-daily-message-spend": {
        "task": "stacos.accounts.roll_message_spend_ledger",
        "schedule": crontab(hour=0, minute=5),
    },
    # Rolls every tenant's eighteen-month horizon forward. Auto-applied without
    # review because the nightly delta is additive: one more period appears at
    # the far end and nothing already in the register changes. Runs at 01:30 IST,
    # after midnight rollovers have settled and long before anyone is working.
    "roll-obligation-horizon": {
        "task": "stacos.obligations.materialise_roll_horizon",
        "schedule": crontab(hour=1, minute=30),
    },
    # The reminder ladder. Hourly rather than daily so that a tenant whose
    # sweep failed at 06:00 is not silent until tomorrow, and because the
    # dedupe key makes a repeat run cost nothing. 06:00-22:00 IST only: a
    # WhatsApp message about a filing at three in the morning is a way to lose
    # a customer, not to remind them.
    "reminder-sweep": {
        "task": "stacos.notifications.sweep_tenants",
        "schedule": crontab(minute=15, hour="6-22"),
    },
    # Digests are selected by each subscriber's own hour, so this has to run
    # every hour to catch them.
    "notification-digests": {
        "task": "stacos.notifications.send_digests",
        "schedule": crontab(minute=0),
    },
}

app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> str:  # pragma: no cover - operational helper
    return f"request: {self.request!r}"
