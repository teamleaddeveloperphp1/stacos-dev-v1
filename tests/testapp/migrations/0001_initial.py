"""
Throwaway models for the tenant-isolation suite.

A real migration rather than `--run-syncdb`, because unmigrated apps are synced
*before* migrations run — the foreign key below would reference a table that does
not exist yet — and because these tables must be protected by Row-Level Security
like every other scoped table, or the RLS test would be asserting against tables
nobody ever protected.
"""

import django.db.models.deletion
import stacos.core.ids
from django.db import migrations, models

from stacos.core.rls import disable_rls_statements, enable_rls_statements

RLS_TABLES = ("testapp_scopedthing", "testapp_entityscopedthing")


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("tenancy", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name='EntityScopedThing',
            fields=[
                ('id', models.UUIDField(default=stacos.core.ids.uuid7, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('label', models.CharField(max_length=100)),
                ('entity', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='tenancy.entity')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='%(app_label)s_%(class)s_set', to='tenancy.tenant')),
            ],
        ),
        migrations.CreateModel(
            name='ScopedThing',
            fields=[
                ('id', models.UUIDField(default=stacos.core.ids.uuid7, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('label', models.CharField(max_length=100)),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='%(app_label)s_%(class)s_set', to='tenancy.tenant')),
            ],
        ),
        *[
            migrations.RunSQL(
                sql="\n".join(enable_rls_statements(table)),
                reverse_sql="\n".join(disable_rls_statements(table)),
            )
            for table in RLS_TABLES
        ],
    ]
