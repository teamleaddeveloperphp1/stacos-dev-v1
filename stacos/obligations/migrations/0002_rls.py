"""Row-Level Security on the register.

The scoped manager is the first line of defence and it is the one that catches
mistakes during development. This is what catches everything the ORM cannot see:
raw SQL in a report, a ``RawSQL`` aggregate, a future bug in the manager itself.

New tenant-scoped models are **not** picked up automatically — ``manage.py
ensure_rls`` runs in CI and fails the build for anything missing, which is the
safety net that makes a static list in a migration acceptable. A migration has to
describe a fixed historical state, so introspecting the model registry here would
mean this file changed meaning every time a model was added."""

from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = (
    "obligations_obligationinstance",
    "obligations_obligationevent",
    "obligations_obligationsuppression",
    "obligations_obligationinclusion",
    "obligations_entityevent",
    "obligations_materialisationrun",
)


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0001_initial"),
        ("core", "0002_rls_and_audit_guard"),
    ]

    operations = [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]
