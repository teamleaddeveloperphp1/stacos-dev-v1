"""
Verify that every tenant-scoped table is actually under Row-Level Security.

Run in CI. A new model can otherwise ship with a `tenant_id` column, a correct
manager, and no database-level protection at all — and nothing would notice until
a raw query leaked something.

``--sql`` prints the statements needed to fix any gaps, ready to paste into a
migration. The command deliberately does **not** apply them itself: schema
changes belong in the migration graph, not in an imperative command someone runs
by hand on production.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from stacos.core.rls import enable_rls_statements, iter_scoped_models, table_has_rls


class Command(BaseCommand):
    help = "Verify Row-Level Security is enabled on every tenant-scoped table."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--sql",
            action="store_true",
            help="Print the SQL needed to fix any gaps, for pasting into a migration.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        gaps: list[tuple[str, str, list[str]]] = []
        checked = 0

        for model in sorted(iter_scoped_models(), key=lambda m: m._meta.label):
            table = model._meta.db_table
            checked += 1
            enabled, forced, policy = table_has_rls(table)

            problems = []
            if not enabled:
                problems.append("RLS not enabled")
            if not forced:
                problems.append("RLS not forced (the table owner would bypass it)")
            if not policy:
                problems.append("isolation policy missing")

            if problems:
                gaps.append((model._meta.label, table, problems))
            else:
                self.stdout.write(f"  ok   {model._meta.label} ({table})")

        if not gaps:
            self.stdout.write(
                self.style.SUCCESS(f"\n{checked} tenant-scoped tables, all protected.")
            )
            return

        self.stdout.write("")
        for label, table, problems in gaps:
            self.stdout.write(self.style.ERROR(f"  FAIL {label} ({table}): {'; '.join(problems)}"))

        if options["sql"]:
            self.stdout.write("\n-- Add to a migration:\n")
            for _, table, _ in gaps:
                for statement in enable_rls_statements(table):
                    self.stdout.write(statement)

        raise CommandError(
            f"{len(gaps)} of {checked} tenant-scoped tables are not protected by "
            f"Row-Level Security. Re-run with --sql to get the fix."
        )
