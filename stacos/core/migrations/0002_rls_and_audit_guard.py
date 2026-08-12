"""
Database-level enforcement of the two invariants the application layer promises.

**Row-Level Security** on every tenant-scoped table. The application's scoped
manager is the first line of defence; this is what catches raw SQL, ``.extra()``,
``RawSQL`` aggregates, and any future bug in the manager itself.

**Append-only audit.** ``UPDATE`` and ``DELETE`` on the audit log raise, so
"immutable" is a property PostgreSQL enforces rather than a convention the code
is trusted to observe. Retention jobs opt in explicitly with a session flag,
which makes deletion a visible, deliberate act.

New tenant-scoped models are **not** picked up automatically. ``manage.py
ensure_rls`` runs in CI and fails the build for anything missing, which is the
safety net that makes a static list here acceptable.
"""

from django.contrib.postgres.operations import (
    BtreeGistExtension,
    CreateExtension,
    TrigramExtension,
    UnaccentExtension,
)
from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

#: Every table under tenant isolation. Kept explicit rather than introspected,
#: because a migration must describe a fixed historical state.
RLS_TABLES = (
    "tenancy_entity",
    "tenancy_entityprofile",
    "tenancy_entityfactvalue",
    "tenancy_entityregistration",
    "tenancy_entitypremises",
    "tenancy_membership",
    "engagements_engagement",
)

AUDIT_APPEND_ONLY = """
CREATE OR REPLACE FUNCTION stacos_audit_append_only() RETURNS trigger AS $fn$
BEGIN
    -- Retention and archival set this flag deliberately; ordinary application
    -- code never does, so an accidental UPDATE or DELETE fails loudly.
    IF coalesce(current_setting('stacos.audit_maintenance', true), 'off') = 'on' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END IF;
    RAISE EXCEPTION
        'core_auditlog is append-only; % is not permitted', TG_OP
        USING HINT = 'Audit entries are evidence. Archive them, never edit them.';
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS core_auditlog_append_only ON core_auditlog;
CREATE TRIGGER core_auditlog_append_only
    BEFORE UPDATE OR DELETE ON core_auditlog
    FOR EACH ROW EXECUTE FUNCTION stacos_audit_append_only();
"""

AUDIT_APPEND_ONLY_REVERSE = """
DROP TRIGGER IF EXISTS core_auditlog_append_only ON core_auditlog;
DROP FUNCTION IF EXISTS stacos_audit_append_only();
"""


def _rls_operations() -> list[migrations.RunSQL]:
    return [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
        ("tenancy", "0001_initial"),
        ("engagements", "0001_initial"),
    ]

    operations = [
        # Extensions. `IF NOT EXISTS` semantics mean this succeeds without
        # elevated privileges when the installer has already created them.
        TrigramExtension(),      # pg_trgm — command palette fuzzy search
        UnaccentExtension(),     # accent-insensitive search
        BtreeGistExtension(),    # exclusion constraints over date ranges
        CreateExtension("pgcrypto"),
        *_rls_operations(),
        migrations.RunSQL(sql=AUDIT_APPEND_ONLY, reverse_sql=AUDIT_APPEND_ONLY_REVERSE),
    ]
