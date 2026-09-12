"""Row-Level Security on the practice's own records.

Scoped to the practice tenant, which is what keeps a client from ever seeing the
firm's estimate, its assignment or its margin on that client's own work."""

from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = (
    "practice_workitem",
    "practice_timeentry",
    "practice_ratecard",
)


class Migration(migrations.Migration):
    dependencies = [
        ("practice", "0001_initial"),
        ("core", "0002_rls_and_audit_guard"),
    ]

    operations = [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]
