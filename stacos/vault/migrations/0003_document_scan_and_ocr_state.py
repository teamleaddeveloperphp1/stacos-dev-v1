"""
`is_scanned` becomes a state machine.

A boolean could say "scanned" and "not scanned", which turned out to be the
wrong two facts. The three that matter are *clean*, *quarantined* and *the
scanner could not be reached*, and the last one is neither of the others: it must
be retried, not released and not condemned.

The ordering below is deliberate. Adding the new column, carrying the old value
across, and only then dropping the old one means an already-cleared file stays
downloadable through the deployment. Letting the auto-generated order stand —
drop, then add with a PENDING default — would silently revoke every existing
document's clearance and leave a firm unable to open its own files until the
sweep worked through them.
"""

from django.db import migrations, models


def carry_clearance_forward(apps, schema_editor):
    """`is_scanned = True` meant a scanner had passed the file. That is CLEAN."""
    Document = apps.get_model("vault", "Document")
    Document.objects.filter(is_scanned=True).update(scan_state="CLEAN")


def restore_boolean(apps, schema_editor):
    Document = apps.get_model("vault", "Document")
    Document.objects.filter(scan_state="CLEAN").update(is_scanned=True)


class Migration(migrations.Migration):

    dependencies = [
        ("vault", "0002_rls"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="scan_state",
            field=models.CharField(
                choices=[
                    ("PENDING", "Waiting to be scanned"),
                    ("CLEAN", "Scanned, no threat found"),
                    ("INFECTED", "Threat found — quarantined"),
                    ("ERROR", "Scanner unavailable"),
                ],
                db_index=True,
                default="PENDING",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="scanned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="ocr_state",
            field=models.CharField(
                choices=[
                    ("PENDING", "Not yet processed"),
                    ("DONE", "Text extracted"),
                    ("EMPTY", "Processed, no text found"),
                    ("SKIPPED", "Not a file text can be read from"),
                ],
                db_index=True,
                default="PENDING",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="document",
            name="ocr_engine",
            field=models.CharField(blank=True, max_length=20),
        ),
        # Widened from 40: an engine name plus a signature does not fit, and
        # truncating the signature destroys the only useful thing in the field.
        migrations.AlterField(
            model_name="document",
            name="scan_result",
            field=models.CharField(blank=True, max_length=120),
        ),
        migrations.RunPython(carry_clearance_forward, restore_boolean),
        migrations.RemoveField(
            model_name="document",
            name="is_scanned",
        ),
    ]
