"""
Reconcile the system roles in the database with the definitions in code.

Run on every deploy. Adding a permission to a bundle then reaches existing
tenants without editing migration history, which is the reason these are not
data migrations.

Customer-defined roles are never touched — only rows with ``is_system=True``.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from stacos.core.permissions import permission_registry
from stacos.tenancy.models import Role
from stacos.tenancy.system_roles import SYSTEM_ROLES


class Command(BaseCommand):
    help = "Create or update the built-in system roles."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--dry-run", action="store_true", help="Show changes without applying.")

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = options["dry_run"]
        created = updated = unchanged = 0

        for spec in SYSTEM_ROLES:
            # A typo in a permission code would otherwise ship a role that
            # silently grants less than intended.
            unknown = sorted(c for c in spec.permissions if c not in permission_registry)
            if unknown:
                self.stderr.write(
                    self.style.ERROR(
                        f"Role {spec.code!r} references unregistered permissions: "
                        f"{', '.join(unknown)}"
                    )
                )
                raise SystemExit(1)

            permissions = sorted(permission_registry.expand(spec.permissions))

            role = Role.objects.filter(
                tenant__isnull=True, code=spec.code, tenant_type=spec.tenant_type
            ).first()

            if role is None:
                created += 1
                self.stdout.write(f"  + {spec.tenant_type:<13} {spec.code}")
                if not dry_run:
                    Role.objects.create(
                        code=spec.code,
                        name=spec.name,
                        description=spec.description,
                        tenant_type=spec.tenant_type,
                        permissions=permissions,
                        is_system=True,
                        rank=spec.rank,
                    )
                continue

            changed = (
                role.name != spec.name
                or role.description != spec.description
                or sorted(role.permissions) != permissions
                or role.rank != spec.rank
            )
            if not changed:
                unchanged += 1
                continue

            updated += 1
            added = set(permissions) - set(role.permissions)
            removed = set(role.permissions) - set(permissions)
            self.stdout.write(f"  ~ {spec.tenant_type:<13} {spec.code}")
            if added:
                self.stdout.write(f"      + {', '.join(sorted(added))}")
            if removed:
                self.stdout.write(f"      - {', '.join(sorted(removed))}")

            if not dry_run:
                role.name = spec.name
                role.description = spec.description
                role.permissions = permissions
                role.rank = spec.rank
                role.save(update_fields=["name", "description", "permissions", "rank"])

        summary = f"{created} created, {updated} updated, {unchanged} unchanged"
        if dry_run:
            self.stdout.write(self.style.WARNING(f"\nDry run — nothing written. {summary}."))
            transaction.set_rollback(True)
        else:
            self.stdout.write(self.style.SUCCESS(f"\nSystem roles synchronised: {summary}."))
