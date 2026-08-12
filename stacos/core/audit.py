"""
Audit recording.

One entry point, ``record_event()``, so audit rows look the same wherever they
come from. Call it from service functions and state transitions — not from
templates or views, where the "before" state has usually already been lost.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db import models

from stacos.core.models import AuditAction, AuditLog
from stacos.core.request_context import current_request_meta
from stacos.core.scope import current_scope

if TYPE_CHECKING:
    from stacos.accounts.models import User

__all__ = ["diff_fields", "record_event", "record_platform_scope_entry"]

#: Never write these into ``before``/``after``. The audit log is exported to
#: tenants and read by support; secrets must not travel with it.
REDACTED_FIELDS = frozenset(
    {
        "password",
        "otp_code",
        "email_code_hash",
        "phone_code_hash",
        "token",
        "token_hash",
        "secret",
        "secret_hash",
        "api_key",
        "portal_password",
        "client_secret",
    }
)
REDACTED = "[redacted]"


def record_event(
    *,
    action: str | AuditAction,
    actor: User | None = None,
    tenant_id: UUID | None = None,
    entity_id: UUID | None = None,
    obj: models.Model | None = None,
    object_type: str = "",
    object_id: str = "",
    object_label: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> AuditLog:
    """Write one audit entry.

    Request metadata (IP, user agent, request id) and the acting-as user are
    picked up from the ambient request context rather than passed in, so service
    functions do not need a ``request`` argument just to be auditable.
    """
    if obj is not None:
        object_type = object_type or obj._meta.label
        object_id = object_id or str(obj.pk)
        object_label = object_label or str(obj)[:255]
        if tenant_id is None:
            tenant_id = getattr(obj, "tenant_id", None)
        if entity_id is None:
            entity_id = getattr(obj, "entity_id", None)

    meta = current_request_meta()
    scope = current_scope()

    if tenant_id is None and scope is not None:
        tenant_id = scope.principal_tenant_id

    actor_label = ""
    if actor is not None and actor.is_authenticated:
        actor_label = getattr(actor, "audit_label", None) or str(actor)
    elif meta.user_label:
        actor_label = meta.user_label

    return AuditLog.objects.create(
        tenant_id=tenant_id,
        entity_id=entity_id,
        actor=actor if (actor is not None and actor.is_authenticated) else None,
        acting_as_id=meta.impersonator_id,
        actor_label=actor_label[:255],
        action=str(action),
        object_type=object_type[:100],
        object_id=str(object_id)[:64],
        object_label=object_label[:255],
        before=_redact(before),
        after=_redact(after),
        context=context or {},
        ip_address=meta.ip_address,
        user_agent=(meta.user_agent or "")[:512],
        request_id=meta.request_id or "",
        scope_reason=(scope.reason if scope else "")[:120],
    )


def record_platform_scope_entry(*, reason: str, actor_id: UUID | None = None) -> None:
    """Record that someone opened a scope that can see every tenant.

    Deliberately noisy. Unrestricted access to all customer data should always
    leave a trail somebody can be asked about.
    """
    with contextlib.suppress(Exception):
        AuditLog.objects.create(
            action=AuditAction.PLATFORM_ACCESS,
            actor_id=actor_id,
            object_type="platform",
            object_label=reason[:255],
            context={"reason": reason},
        )


def diff_fields(
    instance: models.Model,
    *,
    fields: list[str],
    original: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(before, after)`` for the named fields.

    Pass ``original`` — usually captured with ``model_to_dict`` before mutating —
    to get a true before/after pair. Without it you get the current values as
    ``after`` and an empty ``before``, which is right for creations.
    """
    after = {f: _serialise(getattr(instance, f, None)) for f in fields}
    if original is None:
        return {}, after
    before = {f: _serialise(original.get(f)) for f in fields}
    changed = {f for f in fields if before.get(f) != after.get(f)}
    return (
        {f: v for f, v in before.items() if f in changed},
        {f: v for f, v in after.items() if f in changed},
    )


def _redact(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return payload
    return {
        key: (REDACTED if key.lower() in REDACTED_FIELDS else _serialise(value))
        for key, value in payload.items()
    }


def _serialise(value: Any) -> Any:
    """Coerce a value into something JSON can hold."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, models.Model):
        return str(value.pk)
    if isinstance(value, list | tuple | set | frozenset):
        return [_serialise(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _serialise(v) for k, v in value.items()}
    return str(value)
