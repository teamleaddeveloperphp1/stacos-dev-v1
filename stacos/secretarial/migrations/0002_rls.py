"""Row-Level Security on the secretarial records.

The cap table in particular: who owns what is the most commercially sensitive
information a private company holds, and it must not be reachable by a query
that forgot its filter.
"""

from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = (
    "secretarial_meeting",
    "secretarial_meetingattendee",
    "secretarial_resolution",
    "secretarial_statutoryregister",
    "secretarial_shareholder",
    "secretarial_sharetransaction",
)


class Migration(migrations.Migration):
    dependencies = [
        ("secretarial", "0001_initial"),
        ("core", "0002_rls_and_audit_guard"),
    ]

    operations = [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]
