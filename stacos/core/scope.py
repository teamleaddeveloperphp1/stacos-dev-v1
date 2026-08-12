"""
The access scope: what the current actor may see and change, right now.

STACOS is shared-schema multi-tenant. Every tenant-owned row carries a
``tenant_id``, and correctness depends entirely on never issuing a query without
a filter on it. Rather than trusting developers to remember, the scope is bound
to the execution context and the default manager *raises* when it is absent.

Two dimensions matter, not one. An engagement grants a practice access to
specific **entities** and specific compliance **categories** of a client — never
to a whole organisation tenant. So a scope carries both a tenant set and an
optional entity set, and models declare which fields they are filtered on.

``contextvars`` rather than ``threading.local``: the binding has to survive
async views and the ASGI Server-Sent Events stream, where a single thread
interleaves many requests.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from uuid import UUID

from stacos.core.exceptions import UnscopedQueryError

__all__ = [
    "AccessScope",
    "current_scope",
    "platform_scope",
    "require_scope",
    "scope_bound",
    "tenant_context",
]


@dataclass(frozen=True, slots=True)
class AccessScope:
    """An immutable description of the current actor's reach.

    :param principal_tenant_id: the tenant whose context the user is "in" — the
        one selected in the tenant switcher. Writes default here.
    :param readable_tenant_ids: every tenant whose rows may be read. For an
        organisation user this is just their own; for a practice user it is the
        practice plus every organisation tenant they hold an active engagement
        with.
    :param writable_tenant_ids: a subset of the readable set. Engagements are
        frequently read-only or category-limited, so these are tracked apart.
    :param entity_ids: ``None`` means "every entity of the readable tenants".
        A set means the actor is scoped to exactly those entities — a plant HR
        user on one factory, or a practice engaged for two of a group's eight
        subsidiaries.
    :param categories: compliance categories the actor may touch, or ``None``
        for all. A department user sees ``SAFETY_FIRE`` and nothing financial.
    :param permissions: resolved permission codes, so views do not re-query.
    :param reason: why this scope exists — ``"request"``, ``"celery:<task>"``,
        ``"platform:<reason>"``. Appears in audit rows and logs.
    :param bypass: disables tenant filtering entirely. Platform administration
        only, always audited, never reachable from a tenant-facing view.
    """

    principal_tenant_id: UUID | None = None
    readable_tenant_ids: frozenset[UUID] = field(default_factory=frozenset)
    writable_tenant_ids: frozenset[UUID] = field(default_factory=frozenset)
    entity_ids: frozenset[UUID] | None = None
    categories: frozenset[str] | None = None
    permissions: frozenset[str] = field(default_factory=frozenset)
    reason: str = "unknown"
    bypass: bool = False

    def can_read(self, tenant_id: UUID) -> bool:
        return self.bypass or tenant_id in self.readable_tenant_ids

    def can_write(self, tenant_id: UUID) -> bool:
        return self.bypass or tenant_id in self.writable_tenant_ids

    def has_permission(self, code: str) -> bool:
        return self.bypass or code in self.permissions

    def narrowed_to(self, *, entity_ids: Iterable[UUID]) -> AccessScope:
        """Return a copy restricted to a subset of the current entities.

        Used when a view is already operating on one entity: narrowing the scope
        means a mistake inside that view cannot reach a sibling entity.
        """
        requested = frozenset(entity_ids)
        allowed = requested if self.entity_ids is None else requested & self.entity_ids
        return replace(self, entity_ids=allowed)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        entities = "all" if self.entity_ids is None else len(self.entity_ids)
        return (
            f"<AccessScope reason={self.reason!r} "
            f"tenants={len(self.readable_tenant_ids)} entities={entities} "
            f"bypass={self.bypass}>"
        )


_current: ContextVar[AccessScope | None] = ContextVar("stacos_access_scope", default=None)


def current_scope() -> AccessScope | None:
    """Return the bound scope, or ``None`` if nothing is bound."""
    return _current.get()


def scope_bound() -> bool:
    return _current.get() is not None


def require_scope() -> AccessScope:
    """Return the bound scope or raise.

    Use in service functions that must not run unscoped, so the failure happens
    at the top of the call rather than deep inside a queryset.
    """
    scope = _current.get()
    if scope is None:
        raise UnscopedQueryError(
            "No AccessScope is bound. Wrap this work in `tenant_context(...)`, "
            "or `platform_scope(reason=...)` if it is genuinely platform-wide."
        )
    return scope


@contextlib.contextmanager
def tenant_context(
    *,
    tenant_ids: Iterable[UUID] | UUID,
    reason: str,
    principal_tenant_id: UUID | None = None,
    writable_tenant_ids: Iterable[UUID] | None = None,
    entity_ids: Iterable[UUID] | None = None,
    categories: Iterable[str] | None = None,
    permissions: Iterable[str] = (),
) -> Iterator[AccessScope]:
    """Bind an access scope for the duration of the block.

    The normal way to scope work outside the request cycle: management commands,
    Celery tasks, data migrations and tests.

    >>> with tenant_context(tenant_ids=tenant.id, reason="backfill"):
    ...     Entity.objects.count()
    """
    readable = frozenset([tenant_ids] if isinstance(tenant_ids, UUID) else tenant_ids)
    writable = frozenset(writable_tenant_ids) if writable_tenant_ids is not None else readable

    scope = AccessScope(
        principal_tenant_id=principal_tenant_id or next(iter(readable), None),
        readable_tenant_ids=readable,
        writable_tenant_ids=writable,
        entity_ids=frozenset(entity_ids) if entity_ids is not None else None,
        categories=frozenset(categories) if categories is not None else None,
        permissions=frozenset(permissions),
        reason=reason,
    )
    token: Token[AccessScope | None] = _current.set(scope)
    _sync_rls(tenant_ids=readable)
    try:
        yield scope
    finally:
        _current.reset(token)
        _sync_rls(tenant_ids=frozenset())


@contextlib.contextmanager
def platform_scope(*, reason: str, actor_id: UUID | None = None) -> Iterator[AccessScope]:
    """Bind a scope that sees every tenant. Platform administration only.

    Every entry writes an audit row. This is deliberately noisy: unrestricted
    access to all customer data should leave a trail that someone can be asked
    about later. Never call this from a tenant-facing view — support access runs
    through consented, time-boxed impersonation instead.
    """
    scope = AccessScope(reason=f"platform:{reason}", bypass=True)
    token: Token[AccessScope | None] = _current.set(scope)
    _sync_rls(bypass=True)
    try:
        _record_platform_access(reason=reason, actor_id=actor_id)
        yield scope
    finally:
        _current.reset(token)
        _sync_rls(bypass=False)


def _sync_rls(
    *,
    tenant_ids: frozenset[UUID] | None = None,
    bypass: bool | None = None,
) -> None:
    """Mirror the bound scope into PostgreSQL's Row-Level Security settings.

    Keeping the two layers in step here — rather than expecting every call site
    to remember — is what makes RLS a genuine backstop instead of a thing that
    is only correct on the request path.

    Deliberately failure-tolerant: this module is imported during migrations,
    management commands and pure unit tests where no usable connection exists,
    and an RLS bookkeeping failure must not be the reason those cannot run. The
    database still fails *closed* in that case, because an unset setting matches
    no rows.
    """
    try:
        from stacos.core.rls import apply_rls_tenants, set_rls_bypass

        if bypass is not None:
            set_rls_bypass(bypass)
        if tenant_ids is not None:
            apply_rls_tenants(tenant_ids)
    except Exception:  # pragma: no cover - no database available
        return


def _record_platform_access(*, reason: str, actor_id: UUID | None) -> None:
    """Write the audit row for a platform-scope entry.

    Imported lazily and failure-tolerant: this runs during migrations and
    management commands where the audit table may not exist yet, and an audit
    failure must never be the reason a platform operation cannot proceed.
    """
    try:
        from stacos.core.audit import record_platform_scope_entry
    except Exception:  # pragma: no cover - import-time safety only
        return
    with contextlib.suppress(Exception):
        record_platform_scope_entry(reason=reason, actor_id=actor_id)
