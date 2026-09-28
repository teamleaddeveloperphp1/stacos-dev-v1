"""
Write the Roles & Permissions guide from the role definitions.

    python manage.py roles_guide            # regenerate docs/roles-and-permissions.html
    python manage.py roles_guide --check    # fail if the checked-in copy is stale

Open the output in a browser and print to PDF (A4) for a customer copy. Never
edit the output by hand: change ``stacos.tenancy.system_roles``, the permission
registry or ``stacos.tenancy.roles_guide`` and run this again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from stacos.tenancy.roles_guide import GUIDE_PATH, render_guide


class Command(BaseCommand):
    help = "Generate the Roles & Permissions guide (HTML, printable to PDF)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--output", default=str(GUIDE_PATH), help="Where to write the guide.")
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit non-zero if the file on disk differs from what would be generated.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        path = Path(options["output"])
        html = render_guide()

        if options["check"]:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != html:
                raise CommandError(
                    f"{path} is out of date with the role definitions. "
                    "Run `python manage.py roles_guide` and commit the result."
                )
            self.stdout.write(self.style.SUCCESS(f"{path} is up to date."))
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote {path}."))
