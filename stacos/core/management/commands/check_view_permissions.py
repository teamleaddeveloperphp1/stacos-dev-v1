"""
Fail the build when a routable view does not declare its required permissions.

This is a thin wrapper around the ``stacos.E001``/``stacos.E003`` system checks so
CI can run it as its own step and report cleanly. The real work is in
:mod:`stacos.core.checks`, which walks Django's URL resolver rather than grepping
source — decorators applied at ``as_view()`` time are invisible to a grep.

A green result proves a declaration *exists*, not that it is *correct*, and says
nothing about object-level scoping. Treat it as a lint. The security control is
``tests/security/``, which acts as one tenant and demands another's rows.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from stacos.core.checks import check_model_tenancy, check_view_permissions


class Command(BaseCommand):
    help = "Verify every application view declares its required permissions."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--list",
            action="store_true",
            help="List every view and the permissions it declares.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["list"]:
            self._list_views()

        errors = [*check_view_permissions(), *check_model_tenancy()]

        if not errors:
            self.stdout.write(
                self.style.SUCCESS("All views declare permissions; all models declare tenancy.")
            )
            return

        for error in errors:
            self.stdout.write(self.style.ERROR(f"[{error.id}] {error.msg}"))
            if error.hint:
                self.stdout.write(f"        {error.hint}")

        raise CommandError(f"{len(errors)} declaration problem(s) found.")

    def _list_views(self) -> None:
        from stacos.core.checks import OWNED_MODULE_PREFIX, iter_app_views
        from stacos.core.permissions import PERMISSION_ATTR, PUBLIC_ATTR

        for route, callback in iter_app_views():
            module = getattr(callback, "__module__", "") or ""
            if not module.startswith(OWNED_MODULE_PREFIX):
                continue
            name = getattr(callback, "__qualname__", callback.__class__.__name__)
            if getattr(callback, PUBLIC_ATTR, False):
                declared = "PUBLIC"
            else:
                codes = getattr(callback, PERMISSION_ATTR, None)
                declared = ", ".join(codes) if codes else "(none)"
            self.stdout.write(f"  /{route:<50} {module}.{name}\n      {declared}")
        self.stdout.write("")
