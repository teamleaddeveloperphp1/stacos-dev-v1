"""
PostgreSQL Row-Level Security: the second line of defence.

The first line is :class:`~stacos.core.managers.TenantScopedManager`, which makes
an unscoped ORM query raise. RLS catches what the ORM cannot: hand-written raw
SQL, a `.extra()` call, an aggregate built with `RawSQL`, or a future bug in the
manager itself.

**How it works.** Every request publishes its readable tenant set into a
transaction-local PostgreSQL setting, ``stacos.tenant_ids``. Each scoped table
carries a policy that compares ``tenant_id`` against it. Because the setting is
transaction-local, it cannot leak between pooled connections.

**Why ``FORCE``.** Without ``FORCE ROW LEVEL SECURITY`` the table owner bypasses
policies entirely — and in the test environment the owner *is* the connecting
role, which would make every RLS test silently pass. FORCE keeps the tests
meaningful.

**The bypass escape hatch, honestly.** Data migrations and platform
administration genuinely need to see every row, so the policy also passes when
``stacos.bypass_rls`` is ``on``. Any role that can execute ``SET`` can therefore
turn RLS off, which means RLS here defends against *application bugs* — a
forgotten filter in raw SQL — and not against an attacker who already has
arbitrary SQL execution. That attacker has won regardless; the honest claim is
the narrower one.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from uuid import UUID

from django.conf import settings
from django.db import connection, models

__all__ = [
    "apply_rls_tenants",
    "clear_rls_tenants",
    "disable_rls_statements",
    "enable_rls_statements",
    "iter_scoped_models",
    "set_rls_bypass",
    "table_has_rls",
]

POLICY_NAME = "stacos_tenant_isolation"
TENANT_GUC = "stacos.tenant_ids"
BYPASS_GUC = "stacos.bypass_rls"


def enable_rls_statements(table: str, *, tenant_column: str = "tenant_id") -> list[str]:
    """SQL to put a table under tenant isolation. Use inside a ``RunSQL`` migration.

    ``nullif(..., '')`` matters: an unset or empty setting yields ``NULL``, and
    ``= ANY(NULL)`` is ``NULL``, so the policy fails **closed**. A table with no
    tenant bound returns nothing rather than everything.
    """
    predicate = (
        f"(coalesce(current_setting('{BYPASS_GUC}', true), 'off') = 'on'"
        f" OR {tenant_column} = ANY ("
        f"string_to_array(nullif(current_setting('{TENANT_GUC}', true), ''), ',')::uuid[]"
        f"))"
    )
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
        f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table};",
        f"CREATE POLICY {POLICY_NAME} ON {table} USING {predicate} WITH CHECK {predicate};",
    ]


def disable_rls_statements(table: str) -> list[str]:
    """Reverse of :func:`enable_rls_statements`, for migration rollback."""
    return [
        f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table};",
        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;",
    ]


def iter_scoped_models() -> Iterator[type[models.Model]]:
    """Every concrete model that should be under RLS."""
    from django.apps import apps

    from stacos.core.models import TenantScopedModel

    for model in apps.get_models():
        if not issubclass(model, TenantScopedModel):
            continue
        if model._meta.abstract or model._meta.proxy or not model._meta.managed:
            continue
        yield model


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def apply_rls_tenants(tenant_ids: Iterable[UUID]) -> None:
    """Publish the readable tenant set for the current transaction.

    ``set_config(..., is_local => true)`` is used rather than ``SET LOCAL``
    because it accepts a bound parameter. Outside a transaction the setting
    applies only to the implicit single-statement transaction and is immediately
    discarded — so callers must already be inside ``transaction.atomic()``.
    """
    if not getattr(settings, "STACOS_RLS_ENABLED", True):
        return
    value = ",".join(sorted(str(t) for t in tenant_ids))
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT set_config('{TENANT_GUC}', %s, true)", [value])


def clear_rls_tenants() -> None:
    """Reset the tenant set, so subsequent queries see nothing."""
    if not getattr(settings, "STACOS_RLS_ENABLED", True):
        return
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT set_config('{TENANT_GUC}', '', true)")


def set_rls_bypass(enabled: bool) -> None:
    """Turn the platform bypass on or off for the current transaction."""
    if not getattr(settings, "STACOS_RLS_ENABLED", True):
        return
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT set_config('{BYPASS_GUC}', %s, true)", ["on" if enabled else "off"])


def table_has_rls(table: str) -> tuple[bool, bool, bool]:
    """Return ``(rls_enabled, rls_forced, policy_present)`` for a table."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relrowsecurity,
                   c.relforcerowsecurity,
                   EXISTS (
                       SELECT 1 FROM pg_policy p
                       WHERE p.polrelid = c.oid AND p.polname = %s
                   )
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = %s AND n.nspname = current_schema()
            """,
            [POLICY_NAME, table],
        )
        row = cursor.fetchone()
    if row is None:
        return (False, False, False)
    return (bool(row[0]), bool(row[1]), bool(row[2]))
