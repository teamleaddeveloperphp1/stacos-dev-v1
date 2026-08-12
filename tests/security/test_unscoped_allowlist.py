"""
Guard the tenant-scoping escape hatch.

``Model.objects_unscoped`` sees every tenant's rows. There are legitimate uses —
the bootstrap query that establishes a scope cannot itself be scoped — but each
one has to be a deliberate, reviewed decision rather than a convenient way past
an exception.

So every use is counted against ``tests/unscoped_allowlist.txt``. A new one fails
the build until somebody writes down why it is safe.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = REPO_ROOT / "tests" / "unscoped_allowlist.txt"
#: Requires a leading dot, so `Model.objects_unscoped` counts as a use while
#: ``objects_unscoped`` mentioned in prose does not.
USAGE = re.compile(r"\.objects_unscoped\b")

#: Directories that legitimately mention the name without using the hatch.
EXCLUDED = ("migrations", "tests", "__pycache__")


def _load_allowlist() -> dict[str, int]:
    allowed: dict[str, int] = {}
    for line in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        path, _, count = line.rpartition(":")
        allowed[path.strip()] = int(count)
    return allowed


def _count_usages() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in (REPO_ROOT / "stacos").rglob("*.py"):
        if any(part in EXCLUDED for part in path.parts):
            continue
        # The definition itself, and the error message that names it, are not uses.
        if path.name in {"managers.py", "models.py"} and path.parent.name == "core":
            continue
        hits = len(USAGE.findall(path.read_text(encoding="utf-8")))
        if hits:
            counts[path.relative_to(REPO_ROOT).as_posix()] = hits
    return counts


def test_every_unscoped_usage_is_allowlisted() -> None:
    allowed = _load_allowlist()
    actual = _count_usages()

    undeclared = {p: n for p, n in actual.items() if p not in allowed}
    assert not undeclared, (
        "New uses of `objects_unscoped` found in:\n"
        + "\n".join(f"  {p} ({n})" for p, n in sorted(undeclared.items()))
        + "\n\nThis manager bypasses tenant isolation entirely. If the use is "
        "genuinely necessary, add it to tests/unscoped_allowlist.txt with a "
        "reason. If it is not, bind a scope with `tenant_context(...)` instead."
    )


def test_allowlisted_counts_have_not_grown() -> None:
    """A file already on the list must not quietly gain more uses."""
    allowed = _load_allowlist()
    actual = _count_usages()

    grown = {p: (allowed[p], n) for p, n in actual.items() if p in allowed and n > allowed[p]}
    assert not grown, "Files on the allowlist gained additional unscoped queries:\n" + "\n".join(
        f"  {p}: allowed {a}, found {n}" for p, (a, n) in sorted(grown.items())
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Keeps the list honest as code is removed."""
    allowed = _load_allowlist()
    actual = _count_usages()

    stale = sorted(p for p in allowed if p not in actual)
    if stale:
        pytest.fail(
            "tests/unscoped_allowlist.txt lists files that no longer use "
            "`objects_unscoped`:\n" + "\n".join(f"  {p}" for p in stale)
        )
