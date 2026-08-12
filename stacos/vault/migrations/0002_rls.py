"""Row-Level Security on the vault.

Document bytes are the most sensitive thing a tenant hands this product. Both the
metadata rows and the superseded version rows are covered — the version table is
easy to forget precisely because nothing queries it directly.
"""

from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = (
    "vault_document",
    "vault_documentversion",
    "vault_documentlink",
    "vault_documentdownload",
)


class Migration(migrations.Migration):
    dependencies = [
        ("vault", "0001_initial"),
        ("core", "0002_rls_and_audit_guard"),
    ]

    operations = [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]
