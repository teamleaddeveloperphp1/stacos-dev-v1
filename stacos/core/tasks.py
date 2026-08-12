"""
The Celery base task every tenant-touching job inherits.

Two rules, both learned the hard way in multi-tenant systems:

**A queued task must never inherit ambient authority.** The scope that was
current when the task was *enqueued* is serialised into the message headers, but
it is used only for audit correlation. The task re-derives its own scope from the
``tenant_id`` argument it was given. Otherwise a task enqueued during a
platform-scoped operation would run with platform authority hours later, in a
completely different context.

**At-least-once delivery means idempotency is mandatory.** ``acks_late`` is on, so
a worker dying mid-task redelivers the message. :class:`TenantTask` claims an
idempotency key inside a transaction; a redelivered message finds the row already
present and returns instead of doing the work twice.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from celery import Task
from django.db import IntegrityError, transaction
from django.utils import timezone

from stacos.core.models import TaskRun
from stacos.core.rls import apply_rls_tenants
from stacos.core.scope import tenant_context

logger = structlog.get_logger(__name__)

__all__ = ["TenantTask", "idempotent"]


class TenantTask(Task):
    """Base task that binds a tenant scope from an explicit argument.

    Subclasses (or ``@app.task(base=TenantTask)`` functions) must accept a
    ``tenant_id`` keyword. The scope is bound for the body of the task and torn
    down afterwards, and the PostgreSQL RLS setting is applied to match.

    Not used for platform-wide jobs — those call ``platform_scope()`` explicitly,
    which writes an audit row.
    """

    abstract = True
    acks_late = True
    reject_on_worker_lost = True
    max_retries = 3
    default_retry_delay = 60

    #: Permissions the task acts with. Kept explicit so a task cannot quietly
    #: gain reach when a role definition changes.
    task_permissions: tuple[str, ...] = ()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        tenant_id = kwargs.get("tenant_id")
        if tenant_id is None:
            raise TypeError(
                f"{self.name} inherits TenantTask and must be called with an explicit "
                f"`tenant_id=` keyword. A task must never infer its tenant from "
                f"ambient context."
            )

        tenant_uuid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))

        with (
            tenant_context(
                tenant_ids=tenant_uuid,
                reason=f"celery:{self.name}",
                permissions=self.task_permissions,
            ),
            transaction.atomic(),
        ):
            apply_rls_tenants({tenant_uuid})
            logger.bind(task=self.name, tenant_id=str(tenant_uuid))
            return super().__call__(*args, **kwargs)


def idempotent(key: str, *, task_name: str, tenant_id: UUID | None = None) -> bool:
    """Claim an idempotency key. Returns ``True`` if this caller should proceed.

    >>> if not idempotent(f"invoice:{invoice_id}", task_name="billing.issue"):
    ...     return  # already done, or in flight

    The claim is a unique-constrained insert, so two workers racing on the same
    redelivered message cannot both win.
    """
    try:
        with transaction.atomic():
            TaskRun.objects.create(
                idempotency_key=key,
                task_name=task_name,
                tenant_id_value=tenant_id,
            )
    except IntegrityError:
        logger.info("task.duplicate_suppressed", key=key, task=task_name)
        return False
    return True


def mark_task_finished(key: str, *, status: str, result_ref: str = "", error: str = "") -> None:
    """Close out an idempotency claim."""
    TaskRun.objects.filter(idempotency_key=key).update(
        status=status,
        result_ref=result_ref[:200],
        error=error[:2000],
        finished_at=timezone.now(),
    )
