"""Row-Level Security on information requests."""

from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = (
    "rfi_informationrequest",
    "rfi_requestitem",
    "rfi_requestevent",
)


class Migration(migrations.Migration):
    dependencies = [
        ("rfi", "0001_initial"),
        ("core", "0002_rls_and_audit_guard"),
    ]

    operations = [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]
