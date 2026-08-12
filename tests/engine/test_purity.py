"""
The engine's architectural constraints, enforced mechanically.

Conventions that are only written down get broken in month four. These are the
three that the whole testing strategy depends on, so they are assertions.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import stacos.engine

ENGINE_ROOT = pathlib.Path(stacos.engine.__file__).parent

#: Importing any of these would make the engine untestable without a database
#: and drag framework concerns into the domain layer.
BANNED_IMPORTS = {"django", "celery", "psycopg", "redis", "rest_framework"}

#: Reading the clock makes golden-file testing impossible: the same input would
#: produce different output tomorrow. Every entry point takes `as_of` instead.
BANNED_CALLS = {"today", "now", "utcnow"}


def engine_modules() -> list[pathlib.Path]:
    return sorted(ENGINE_ROOT.rglob("*.py"))


@pytest.mark.parametrize("path", engine_modules(), ids=lambda p: p.name)
def test_engine_imports_no_framework(path: pathlib.Path) -> None:
    """The engine must run with nothing but the standard library."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    offenders = sorted(imported & BANNED_IMPORTS)
    assert not offenders, (
        f"{path.name} imports {offenders}. The engine is pure Python so that a whole "
        f"calendar generation can be unit-tested with no database."
    )


@pytest.mark.parametrize("path", engine_modules(), ids=lambda p: p.name)
def test_engine_never_reads_the_clock(path: pathlib.Path) -> None:
    """No ``date.today()`` or ``datetime.now()`` anywhere in the engine."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in BANNED_CALLS
        ):
            offenders.append(f"line {node.lineno}: .{node.func.attr}()")

    assert not offenders, (
        f"{path.name} reads the clock ({'; '.join(offenders)}). Take `as_of` as an "
        f"argument — golden-file tests cannot exist otherwise."
    )


def test_no_jurisdiction_literals_in_application_code() -> None:
    """No `if industry == "pharma"`, anywhere outside catalog data.

    The brief's rule is absolute: applicability lives in data so a platform
    administrator can add a state or a sector without a deploy. This is the check
    that keeps it true.
    """
    import re

    root = pathlib.Path(stacos.engine.__file__).parents[1]
    banned = re.compile(
        r"""["'](pharma|textile|manufacturing|IN-[A-Z]{2}-[A-Z0-9-]+)["']\s*(==|!=|\bin\b)"""
    )

    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if any(part in {"migrations", "tests", "catalog_data"} for part in path.parts):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if banned.search(line):
                offenders.append(f"{path.name}:{number}")

    assert not offenders, (
        f"Jurisdiction or sector literals compared in code: {offenders}. "
        f"These belong in the catalog, not in application logic."
    )
