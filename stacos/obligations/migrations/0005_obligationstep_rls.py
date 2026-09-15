"""Row-Level Security on the checklist.

Same rationale as ``0002_rls.py`` — a new tenant-scoped table is not picked up
automatically, and ``manage.py ensure_rls`` is what would fail CI if this were
missing.
"""

from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = ("obligations_obligationstep",)


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0004_alter_obligationevent_kind_obligationstep"),
    ]

    operations = [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]
