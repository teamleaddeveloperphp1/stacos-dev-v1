"""
The Roles & Permissions guide cannot drift from the code.

The previous guide was written by hand and, within a week, promised modules this
fork did not have and missed permissions it did. This one is generated; these
tests fail when the committed copy is stale, or when a role holds a permission
the guide has nowhere to show.
"""

from __future__ import annotations

from django.core.management import call_command

from stacos.core.permissions import permission_registry
from stacos.tenancy.roles_guide import BASICS, CAPABILITIES, GUIDE_PATH, render_guide
from stacos.tenancy.system_roles import SYSTEM_ROLES


def test_the_committed_guide_matches_the_role_definitions() -> None:
    assert GUIDE_PATH.exists(), "run `python manage.py roles_guide`"
    assert GUIDE_PATH.read_text(encoding="utf-8") == render_guide(), (
        "docs/roles-and-permissions.html is stale. Run `python manage.py roles_guide`."
    )


def test_the_check_flag_passes_on_a_fresh_guide() -> None:
    call_command("roles_guide", "--check", verbosity=0)


def test_every_permission_a_role_holds_appears_in_the_guide() -> None:
    covered = set(BASICS) | {
        code for _title, rows in CAPABILITIES for row in rows for code in row.codes
    }
    held = {code for spec in SYSTEM_ROLES for code in permission_registry.expand(spec.permissions)}
    assert sorted(held - covered) == []


def test_every_capability_names_a_registered_permission() -> None:
    unknown = sorted(
        code
        for _title, rows in CAPABILITIES
        for row in rows
        for code in row.codes
        if code not in permission_registry
    )
    assert unknown == []


def test_the_guide_describes_only_modules_that_exist() -> None:
    html = render_guide()
    for absent in ("Secretarial", "Notices", "Working papers", "cap table"):
        assert absent not in html, absent
