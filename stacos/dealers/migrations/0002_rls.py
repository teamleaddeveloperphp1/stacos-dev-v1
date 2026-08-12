"""Row-Level Security on the channel programme.

A dealer sees their own ledger and nobody else's — including no other dealer's.
"""

from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = (
    "dealers_commissionplan",
    "dealers_commissionentry",
    "dealers_payout",
)


class Migration(migrations.Migration):
    dependencies = [
        ("dealers", "0001_initial"),
        ("core", "0002_rls_and_audit_guard"),
    ]

    operations = [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]
