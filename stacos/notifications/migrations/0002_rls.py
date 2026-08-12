"""Row-Level Security on notifications.

A notification body quotes the thing it is about — an entity's name, a notice
reference, an amount outstanding. That makes this table a summary of a tenant's
compliance position in plain text, and one of the more attractive things in the
schema to read across a tenant boundary.

The delivery table is covered for the same reason and is easier to overlook: it
holds the recipient's email address and phone number alongside the notification
it belongs to.
"""

from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = (
    "notifications_notification",
    "notifications_delivery",
    "notifications_notificationpreference",
)


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0001_initial"),
        ("core", "0002_rls_and_audit_guard"),
    ]

    operations = [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]
