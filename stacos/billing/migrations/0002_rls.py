"""Row-Level Security on billing.

`billing.Plan` is deliberately absent: it is the published price list and is the
same for every customer. Everything that names an amount a specific customer owes
is covered."""

from django.db import migrations

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = (
    "billing_subscription",
    "billing_invoice",
    "billing_invoiceline",
    "billing_payment",
)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0001_initial"),
        ("core", "0002_rls_and_audit_guard"),
    ]

    operations = [
        migrations.RunSQL(
            sql="\n".join(enable_rls_statements(table)),
            reverse_sql="\n".join(disable_rls_statements(table)),
        )
        for table in RLS_TABLES
    ]
