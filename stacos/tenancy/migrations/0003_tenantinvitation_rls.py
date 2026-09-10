"""Row-Level Security on colleague invitations.

Separate from the `CreateModel` migration because `makemigrations` cannot know
about it: the policy is raw SQL, and a tenant-scoped table without one is
protected only by the ORM manager. `manage.py ensure_rls` is the gate that
notices, and it runs in CI.
"""

from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = ("tenancy_tenantinvitation",)


class Migration(migrations.Migration):
    dependencies = [
        ("tenancy", "0002_tenantinvitation"),
        ("core", "0002_rls_and_audit_guard"),
    ]

    operations = [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]
