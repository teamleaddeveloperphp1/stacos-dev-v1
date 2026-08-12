"""
Template smoke tests.

Every template in this project is compiled, and every component is rendered
through the real loader chain, at least once here.

The reason is specific: a Django template error is a **runtime** error. Nothing —
not mypy, not ruff, not ``manage.py check`` — notices a mistyped tag, a component
default declared with the wrong syntax, or a multi-line ``{# … #}`` comment
(which Django does not support and silently renders as page text). The first
thing that notices is a 500, or visible comment prose, in front of a user.

These tests cost milliseconds and catch that entire class.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.template.loader import get_template, render_to_string
from django.test import RequestFactory

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
COMPONENTS_DIR = TEMPLATES_DIR / "components"
GALLERY = "_component_gallery.html"


def _template_paths() -> list[str]:
    return sorted(p.relative_to(TEMPLATES_DIR).as_posix() for p in TEMPLATES_DIR.rglob("*.html"))


# ===========================================================================
# 1. Every template compiles
# ===========================================================================


@pytest.mark.parametrize("path", _template_paths())
def test_template_compiles(path: str) -> None:
    """Loading a template parses it, catching every syntax error.

    A parse failure is unconditional — it breaks the page for everyone, on every
    request — so this is the cheapest high-value test in the suite.
    """
    get_template(path)


# ===========================================================================
# 2. Multi-line comments
# ===========================================================================

#: `{# … #}` that opens without closing on the same line. Django's lexer regex
#: has no DOTALL flag, so this is not treated as a comment at all: the prose
#: renders into the page and any tag inside it is parsed for real.
_UNCLOSED_COMMENT = re.compile(r"\{#(?![^\n]*#\})")


@pytest.mark.parametrize("path", _template_paths())
def test_no_multiline_hash_comments(path: str) -> None:
    """Multi-line comments must use ``{% comment %}``.

    ``{# … #}`` is single-line only. Spanning lines with it puts the comment text
    on the page — and if a template tag happens to sit inside, it is executed.
    """
    source = (TEMPLATES_DIR / path).read_text(encoding="utf-8")
    offenders = [
        i + 1 for i, line in enumerate(source.splitlines()) if _UNCLOSED_COMMENT.search(line)
    ]
    assert not offenders, (
        f"{path} lines {offenders}: `{{# #}}` is single-line only in Django. "
        f"Use `{{% comment %}} … {{% endcomment %}}` for anything spanning lines, "
        f"or the comment text renders into the page."
    )


# ===========================================================================
# 3. Every component renders
# ===========================================================================


@pytest.fixture
def gallery_context() -> dict[str, object]:
    today = date(2026, 8, 12)
    return {
        "request": RequestFactory().get("/"),
        "due_date": today + timedelta(days=20),
        "original_date": today - timedelta(days=10),
        "days_ahead": 20,
        "days_late": -10,
        "tenant": SimpleNamespace(id="t1", name="Acme Manufacturing", type="ORGANISATION"),
        "memberships": [],
        "no_tenant": None,
        "no_memberships": [],
        "user": SimpleNamespace(
            initials="AR",
            full_name="Anita Rao",
            email="anita@acme.example",
            is_authenticated=True,
        ),
    }


def test_component_gallery_renders(gallery_context: dict[str, object]) -> None:
    html = render_to_string(GALLERY, gallery_context)
    assert len(html) > 2000, "the gallery rendered suspiciously little"


def test_no_unrendered_cotton_tags(gallery_context: dict[str, object]) -> None:
    """A `<c-…>` tag left in the output means the component never ran.

    That is the failure mode when a component file is missing or misnamed: cotton
    leaves the literal tag in place and the page looks almost right.
    """
    html = render_to_string(GALLERY, gallery_context)
    leftovers = re.findall(r"<c-[a-z0-9-]+", html)
    assert not leftovers, f"components did not render: {sorted(set(leftovers))}"


def test_no_leaked_template_syntax(gallery_context: dict[str, object]) -> None:
    """No `{%`, `{{` or `{#` may survive into rendered output."""
    html = render_to_string(GALLERY, gallery_context)
    for token in ("{%", "{{", "{#"):
        assert token not in html, f"unrendered template syntax {token!r} leaked into the page"


def test_every_component_is_covered() -> None:
    """A new component must be added to the gallery.

    Without this the suite quietly stops covering the design system as it grows,
    which is precisely when the coverage starts to matter.
    """
    gallery = (Path(__file__).parent / "templates" / GALLERY).read_text(encoding="utf-8")
    on_disk = {p.stem.replace("_", "-") for p in COMPONENTS_DIR.glob("*.html")}
    missing = sorted(name for name in on_disk if f"<c-{name}" not in gallery)
    assert not missing, (
        f"Components with no gallery entry: {missing}. Add them to tests/templates/{GALLERY}."
    )


# ===========================================================================
# 4. The status vocabulary
# ===========================================================================

STATUSES = (
    "overdue",
    "due-soon",
    "on-track",
    "in-progress",
    "waiting",
    "complete",
    "na",
    "disputed",
)


@pytest.mark.parametrize("status", STATUSES)
def test_status_chip_renders_every_status(status: str, gallery_context: dict[str, object]) -> None:
    """Every status in the vocabulary renders.

    Asserted against the gallery rather than by rendering the component file
    directly, because a component only receives its `<c-vars>` defaults when it
    is invoked as a component.
    """
    html = render_to_string(GALLERY, gallery_context)
    assert f"status-chip--{status}" in html


def test_status_is_never_encoded_by_colour_alone(
    gallery_context: dict[str, object],
) -> None:
    """Colour alone fails for colour-blind users, and fails again in a printed
    PDF. Every chip carries an icon and a word as well as its colour class."""
    html = render_to_string(GALLERY, gallery_context)
    chips = re.findall(
        r'<span class="status-chip status-chip--[a-z-]+".*?</span>\s*</span>', html, re.DOTALL
    )
    assert chips, "no status chips found in the gallery"
    for chip in chips:
        assert "<svg" in chip, f"chip without an icon: {chip[:80]}"
        assert re.search(r"<span>\s*\S", chip), f"chip without a word: {chip[:80]}"


def test_due_badge_keeps_the_original_date_visible(gallery_context: dict[str, object]) -> None:
    """A government extension must not erase what the statute said.

    Indian due dates are extended by notification several times a year, and the
    client needs to see both the original and the extended date.
    """
    html = render_to_string(GALLERY, gallery_context)
    assert "due-badge__original" in html
    assert "2 Aug" in html, "the original date should be rendered struck through"


def test_tenant_switcher_survives_having_no_tenant(
    gallery_context: dict[str, object],
) -> None:
    """Mid-onboarding a user belongs to nothing, and the shell still has to draw."""
    html = render_to_string(GALLERY, gallery_context)
    assert "No organisation" in html
