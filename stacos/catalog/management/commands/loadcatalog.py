"""Load the YAML catalog into the database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from stacos.catalog.bundles import load_bundles
from stacos.catalog.loader import load_catalog, load_extensions
from stacos.catalog.snapshots import invalidate_catalog_cache


class Command(BaseCommand):
    help = "Load compliance definitions and government extensions from catalog/*.yaml."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--root",
            type=Path,
            default=None,
            help="Directory of definition YAML files. Defaults to catalog/definitions.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report without writing. What CI runs on a catalog PR.",
        )
        parser.add_argument(
            "--allow-republish",
            action="store_true",
            help=(
                "Overwrite a published version whose content changed. Off by default: "
                "the correct fix for a wrong rule is a new version with a new "
                "effective window, so last month's calendar stays explainable."
            ),
        )
        parser.add_argument("--skip-extensions", action="store_true", help="Definitions only.")
        parser.add_argument(
            "--skip-bundles", action="store_true", help="Skip the compliance packs."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        report = load_catalog(
            root=options["root"],
            dry_run=options["dry_run"],
            allow_republish=options["allow_republish"],
        )

        for code in report.created:
            self.stdout.write(self.style.SUCCESS(f"  + {code}"))
        for code in report.updated:
            self.stdout.write(f"  ~ {code}")

        # Packs load after definitions, in the same command, because a pack that
        # references a definition is only coherent alongside it. A pack failure
        # is reported but does not roll back the definitions — different people
        # author them, and a broken bundle must not block a rule change.
        if not options["skip_bundles"]:
            packs = load_bundles(dry_run=options["dry_run"])
            report.errors.extend(packs.errors)
            if packs.created or packs.updated:
                self.stdout.write(
                    f"  packs: {len(packs.created)} new, {len(packs.updated)} updated"
                )

        if not options["skip_extensions"] and not options["dry_run"]:
            extensions = load_extensions()
            report.errors.extend(extensions.errors)
            if extensions.created or extensions.updated:
                self.stdout.write(
                    f"  extensions: {len(extensions.created)} new, "
                    f"{len(extensions.updated)} updated"
                )

        self.stdout.write(self.style.MIGRATE_HEADING(f"Catalog: {report.summary()}"))

        if report.errors:
            for problem in report.errors:
                self.stderr.write(self.style.ERROR(f"  ! {problem}"))
            raise CommandError(f"{len(report.errors)} definition(s) failed to load.")

        if not options["dry_run"]:
            invalidate_catalog_cache()
