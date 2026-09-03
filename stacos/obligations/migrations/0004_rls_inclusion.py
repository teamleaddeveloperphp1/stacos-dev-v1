"""
Row-Level Security on the opt-in table.

``ObligationInclusion`` is tenant-scoped and therefore needs a policy for the
same reason every other scoped table does: the manager catches mistakes in
Django code, and this catches everything the ORM never sees.

Kept as its own migration rather than edited into ``0002_rls`` because that file
describes a fixed historical state — a migration whose meaning changes every time
a model is added is not a migration.
"""

from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = ("obligations_obligationinclusion",)


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0003_obligationinclusion_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]
