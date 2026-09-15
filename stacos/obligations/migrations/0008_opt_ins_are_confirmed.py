"""An obligation somebody added by hand is confirmed by the act of adding it.

``confirmed_by_user`` recorded a second, human "yes, this applies" for an
obligation the planner would never confirm on its own. There is nothing left for
it to record: the planner now confirms an opt-in outright, because "unconfirmed"
means nobody has decided and a person choosing to add something has decided.

The rows already on a register still say otherwise until their entity is next
materialised, and in the meantime each would offer a Confirm control that no
longer leads anywhere. They are exactly the rows that are unconfirmed with no
missing fact behind them — the shape the old opt-in dialog was routed by — so
they can be settled here rather than waiting for a rebuild.

Written as SQL rather than through the historical model deliberately: an
``ObligationInstance`` manager raises without a bound ``AccessScope``, and a
migration runs as the schema owner with no tenant in context.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("obligations", "0007_obligationinstance_acknowledgement_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            UPDATE obligations_obligationinstance
               SET confirmed = TRUE,
                   updated_at = NOW()
             WHERE confirmed = FALSE
               AND cardinality(missing_facts) = 0
            """,
            # Irreversible in fact: which of these rows was an opt-in and which
            # the planner had just confirmed is not recoverable afterwards. The
            # next materialisation run recomputes `confirmed` for every row from
            # the rules and the inclusions, which is the real way back.
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RemoveField(
            model_name="obligationinstance",
            name="confirmed_by_user",
        ),
    ]
