"""Semantic validation of the catalog. Runs on every catalog pull request."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from stacos.catalog.bundles import iter_bundles, parse_bundle
from stacos.catalog.loader import CatalogError, DefinitionDocument, iter_documents, parse_document
from stacos.catalog.validation import Level, validate_catalog


class Command(BaseCommand):
    help = (
        "Check the catalog for dead rules, undiscriminating rules, unresolvable "
        "dates and stale statutory reviews."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--root", type=Path, default=None)
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Treat warnings as errors. What CI uses on main.",
        )
        parser.add_argument(
            "--as-of",
            type=date.fromisoformat,
            default=None,
            help=(
                "Date to validate against, for reproducible runs. Defaults to today, "
                "which is correct for CI and wrong for a golden-file test."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        documents: list[DefinitionDocument] = []
        parse_errors: list[str] = []

        for path, raw in iter_documents(options["root"]):
            try:
                documents.append(parse_document(path, raw))
            except CatalogError as exc:
                parse_errors.append(str(exc))

        if parse_errors:
            for problem in parse_errors:
                self.stderr.write(self.style.ERROR(f"  ! {problem}"))
            raise CommandError(f"{len(parse_errors)} file(s) failed to parse.")

        bundles = []
        for path, raw in iter_bundles():
            try:
                bundles.append(parse_bundle(path, raw))
            except CatalogError as exc:
                parse_errors.append(str(exc))
        if parse_errors:
            for problem in parse_errors:
                self.stderr.write(self.style.ERROR(f"  ! {problem}"))
            raise CommandError(f"{len(parse_errors)} bundle(s) failed to parse.")

        as_of = options["as_of"] or date.today()  # noqa: DTZ011 - a date, not an instant
        findings = validate_catalog(
            documents, as_of=as_of, strict=options["strict"], bundles=bundles
        )

        errors = [f for f in findings if f.level is Level.ERROR]
        warnings = [f for f in findings if f.level is Level.WARNING]

        for finding in warnings:
            self.stdout.write(self.style.WARNING(f"  {finding}"))
        for finding in errors:
            self.stderr.write(self.style.ERROR(f"  {finding}"))

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Validated {len(documents)} definitions and {len(bundles)} packs: "
                f"{len(errors)} errors, {len(warnings)} warnings."
            )
        )

        if errors:
            raise CommandError(f"{len(errors)} catalog error(s).")
