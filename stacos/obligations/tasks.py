"""
Background materialisation.

Three triggers, and the difference between them is not cosmetic:

**Nightly**, rolling the eighteen-month horizon forward. Auto-applied without
review, because the daily delta is purely additive — one more month appears at
the far end and nothing existing changes.

**On profile change**, synchronous and shown as a diff. Ten to twenty-five
milliseconds, so the user sees "3 added, 1 removed, 2 dates changed" and approves
it. That path lives in the views, not here.

**On catalog publication**, staged. A rule change that would supersede existing
instances across ten thousand tenants is the platform's worst case, so that fan-out
is deliberately not a single task that runs everywhere at once.

Every task takes ``tenant_id`` explicitly and re-derives its scope inside the
task. A message sitting on a queue for six hours must not carry the authority of
whatever context enqueued it.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import structlog
from celery import shared_task
from django.utils import timezone

from stacos.core.scope import platform_scope
from stacos.core.tasks import TenantTask, idempotent
from stacos.obligations.models import MaterialisationRun
from stacos.obligations.services import materialise
from stacos.tenancy.models import Entity, Tenant

logger = structlog.get_logger(__name__)

__all__ = ["materialise_entity", "materialise_tenant", "roll_horizon"]

#: Permissions the materialiser acts with. Narrow on purpose: it plans and
#: writes obligations and does nothing else.
_TASK_PERMISSIONS = ("compliance.calendar.rebuild", "compliance.obligation.view")


@shared_task(
    base=TenantTask,
    bind=True,
    name="stacos.obligations.materialise_entity",
    task_permissions=_TASK_PERMISSIONS,
)
def materialise_entity(
    self: TenantTask,
    *,
    tenant_id: str,
    entity_id: str,
    as_of: str | None = None,
    trigger: str = MaterialisationRun.Trigger.NIGHTLY,
) -> dict[str, int | str]:
    """Rebuild one entity's register.

    Idempotent twice over: the claim below suppresses a redelivered message, and
    the planner itself is idempotent, so even a message that slips past the claim
    produces an empty plan rather than duplicate rows.
    """
    day = date.fromisoformat(as_of) if as_of else timezone.localdate()

    # Keyed on the day rather than on the message, so a redelivery hours later is
    # still recognised as the same work.
    key = f"materialise:{entity_id}:{day.isoformat()}:{trigger}"
    if not idempotent(key, task_name=self.name, tenant_id=UUID(str(tenant_id))):
        return {"status": "duplicate", "entity_id": entity_id}

    entity = Entity.objects.filter(pk=entity_id, archived_at__isnull=True).first()
    if entity is None:
        # Scoped lookup: an entity id from another tenant simply is not found,
        # which is the correct outcome and not an error worth retrying.
        logger.warning("materialise.entity_missing", entity_id=entity_id)
        return {"status": "not_found", "entity_id": entity_id}

    run = materialise(entity, as_of=day, trigger=trigger)

    return {
        "status": "ok",
        "entity_id": entity_id,
        "summary": run.summary(),
        **{k: v for k, v in run.as_dict().items() if isinstance(v, int)},
    }


@shared_task(
    base=TenantTask,
    name="stacos.obligations.materialise_tenant",
    task_permissions=_TASK_PERMISSIONS,
)
def materialise_tenant(
    *,
    tenant_id: str,
    as_of: str | None = None,
    trigger: str = MaterialisationRun.Trigger.NIGHTLY,
) -> dict[str, int]:
    """Fan out to every active entity of one tenant.

    One task per entity rather than one loop: a tenant with eighty entities
    should not hold a worker for minutes, and one entity with a broken profile
    should not stop the other seventy-nine from being rebuilt.
    """
    entity_ids = list(
        Entity.objects.filter(
            archived_at__isnull=True,
            status__in=[Entity.Status.ACTIVE, Entity.Status.DORMANT],
        ).values_list("id", flat=True)
    )

    for entity_id in entity_ids:
        materialise_entity.apply_async(
            kwargs={
                "tenant_id": str(tenant_id),
                "entity_id": str(entity_id),
                "as_of": as_of,
                "trigger": trigger,
            }
        )

    return {"entities": len(entity_ids)}


@shared_task(name="stacos.obligations.materialise_roll_horizon", ignore_result=True)
def roll_horizon(as_of: str | None = None) -> dict[str, int]:
    """Nightly: push every tenant's horizon forward by a day.

    Platform-scoped, because enumerating tenants is by definition a cross-tenant
    operation — and audited for exactly that reason. The per-tenant work it
    enqueues is not: each of those re-derives its own scope from the tenant id it
    is handed.
    """
    with platform_scope(reason="nightly-horizon-roll"):
        tenant_ids = list(
            Tenant.objects.filter(
                type=Tenant.Type.ORGANISATION,
                status__in=[Tenant.Status.TRIAL, Tenant.Status.ACTIVE, Tenant.Status.PAST_DUE],
            ).values_list("id", flat=True)
        )

    for tenant_id in tenant_ids:
        materialise_tenant.apply_async(
            kwargs={
                "tenant_id": str(tenant_id),
                "as_of": as_of,
                "trigger": MaterialisationRun.Trigger.NIGHTLY,
            }
        )

    logger.info("materialise.horizon_rolled", tenants=len(tenant_ids))
    return {"tenants": len(tenant_ids)}
