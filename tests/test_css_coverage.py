"""
Every class a template names must exist in the stylesheet that page loads.

A missing CSS rule is invisible to every other gate in this project. The
template compiles, the view returns 200, the accessibility assertions pass, the
query count is right — and the page renders as unstyled boxes. That is the one
class of defect the rest of the suite is structurally unable to see, because it
asserts *structure and behaviour*, not appearance.

Two things make this test worth its weight rather than a nuisance:

*Bundle awareness.* There are two stylesheets. ``app.css`` is the product,
``marketing.css`` is the public site. They never load together. A naive scan
that unions them passes while the marketing hero silently uses an app class (or
the reverse), so this walks the ``extends``/``include``/cotton graph out from
each layout and checks every template against the bundles that can actually
reach it.

*Interpolation awareness.* Half the interesting classes in this codebase are
built by the template — ``stat-tile--{{ row.status }}``. Stripping the tag
leaves the prefix ``stat-tile--``, and a prefix is checked by asking whether the
bundle defines *anything* beginning with it. That catches a whole modifier
family being absent, which is the failure that actually happens, without
demanding the test enumerate every status value.
"""

from __future__ import annotations

import re
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"
CSS = ROOT / "static" / "css"

#: Which stylesheet each layout pulls in. Asserted against the layouts
#: themselves below, so moving a `<link>` cannot silently invalidate the map.
LAYOUT_BUNDLES = {
    "layouts/app_shell.html": "app.css",
    "layouts/auth.html": "app.css",
    "layouts/public.html": "marketing.css",
}

#: Classes no stylesheet is expected to define.
#:
#: `htmx-*` are added and removed by htmx during a request and are hooks for its
#: own machinery; `is-*`/`js-*` are state flags toggled by Alpine. Styling them
#: is allowed, but not required — a hook with no appearance is legitimate.
IGNORED = re.compile(r"^(htmx-|js-|is-|no-js$|sr-describedby)")


# ---------------------------------------------------------------------------
# Reading the stylesheets
# ---------------------------------------------------------------------------

#: Each run of non-brace characters immediately before a `{`. In a compressed
#: stylesheet that is exactly the selector list (or an at-rule prelude);
#: declaration bodies end in `}` and so are never captured. Works through
#: nesting without needing a real parser.
_SELECTOR = re.compile(r"([^{}]+)\{")
_CLASS = re.compile(r"\.(-?[_a-zA-Z][\w-]*)")


def _compiled_classes(bundle: str) -> frozenset[str]:
    path = CSS / bundle
    if not path.exists():  # pragma: no cover - a build failure, not a test case
        pytest.fail(f"{bundle} has not been built. Run `npm run build:css`.")

    css = path.read_text(encoding="utf-8")
    names: set[str] = set()
    for selector in _SELECTOR.findall(css):
        names.update(_CLASS.findall(selector))
    return frozenset(names)


BUNDLES = {name: _compiled_classes(name) for name in ("app.css", "marketing.css")}


# ---------------------------------------------------------------------------
# Reading the templates
# ---------------------------------------------------------------------------

_TAG = re.compile(r"\{[{%#].*?[}%#]\}", re.DOTALL)
#: `class="…"`, but not `:class="…"` or `x-bind:class="…"` — those hold Alpine
#: expressions, whose contents are JavaScript rather than a class list.
_CLASS_ATTR = re.compile(r"(?<![-:\w])class\s*=\s*([\"'])(.*?)\1", re.DOTALL)
_EXTENDS = re.compile(r"\{%\s*extends\s+[\"']([^\"']+)[\"']")
_INCLUDE = re.compile(r"\{%\s*include\s+[\"']([^\"']+)[\"']")
_COTTON = re.compile(r"<c-([a-z0-9][a-z0-9._-]*)")


def _template_paths() -> list[str]:
    return sorted(p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*.html"))


def _cotton_path(tag: str) -> str:
    """`<c-marketing.hero>` → `components/marketing/hero.html`."""
    return "components/" + tag.replace(".", "/").replace("-", "_") + ".html"


_ANY_TAG = re.compile(r"\{%\s*(\w+)[^%]*?%\}")


def _branches(value: str, depth: int = 0) -> list[str]:
    """Every concrete string a conditional class attribute can render.

    `status-chip--{% if error %}overdue{% else %}on-track{% endif %}` produces
    two real class names, and naive tag-stripping produces neither — it yields
    the fragment `status-chip--` plus two bare words that were never classes.
    Expanding the branches is what makes this test able to check the conditional
    modifiers, which are most of the ones worth checking.
    """
    opener = re.search(r"\{%\s*if\b[^%]*?%\}", value)
    if opener is None or depth > 4:
        return [value]

    # Walk forward to this `if`'s own `endif`, noting the branch points that
    # belong to it rather than to a nested conditional.
    cuts: list[tuple[int, int]] = [(opener.start(), opener.end())]
    level = 0
    end: tuple[int, int] | None = None
    for tag in _ANY_TAG.finditer(value, opener.end()):
        name = tag.group(1)
        if name == "if":
            level += 1
        elif name == "endif":
            if level == 0:
                end = (tag.start(), tag.end())
                break
            level -= 1
        elif name in {"elif", "else"} and level == 0:
            cuts.append((tag.start(), tag.end()))

    if end is None:  # unbalanced; leave it to the template compile test
        return [value]

    head, tail = value[: opener.start()], value[end[1] :]
    bounds = [*cuts, end]
    out: list[str] = []
    for (_, start), (stop, _) in pairwise(bounds):
        out.extend(_branches(head + value[start:stop] + tail, depth + 1))
    return out


def _classes_used(source: str) -> set[str]:
    """The class tokens a template names, over all its conditional branches.

    A token left with a trailing `-` is the constant prefix of an interpolated
    name; it is kept as-is and matched by prefix later.
    """
    tokens: set[str] = set()
    for _, value in _CLASS_ATTR.findall(source):
        for branch in _branches(value):
            for token in _TAG.sub(" ", branch).split():
                if token and not IGNORED.match(token):
                    tokens.add(token)
    return tokens


def _dependencies(source: str) -> set[str]:
    return (
        set(_EXTENDS.findall(source))
        | set(_INCLUDE.findall(source))
        | {_cotton_path(tag) for tag in _COTTON.findall(source) if tag != "vars"}
    )


SOURCES = {path: (TEMPLATES / path).read_text(encoding="utf-8") for path in _template_paths()}


# ---------------------------------------------------------------------------
# Which bundle can reach which template
# ---------------------------------------------------------------------------


def _bundles_by_template() -> dict[str, frozenset[str]]:
    """Propagate each layout's stylesheet down the graph of templates.

    Edges run parent → child: a page that extends `app_shell` renders inside it,
    and everything that page includes renders inside it too. A component used by
    both sites is reachable from both bundles and must satisfy both.
    """
    reach: dict[str, set[str]] = defaultdict(set)
    for layout, bundle in LAYOUT_BUNDLES.items():
        reach[layout].add(bundle)

    # A page's own bundle comes from what it extends, so seed from there first.
    changed = True
    while changed:
        changed = False
        for path, source in SOURCES.items():
            parents = set(_EXTENDS.findall(source))
            inherited = {b for parent in parents for b in reach.get(parent, ())}
            if inherited - reach[path]:
                reach[path] |= inherited
                changed = True

    # Then push downwards through includes and components.
    changed = True
    while changed:
        changed = False
        for path, source in SOURCES.items():
            if not reach[path]:
                continue
            for child in _dependencies(source):
                if child in SOURCES and reach[path] - reach[child]:
                    reach[child] |= reach[path]
                    changed = True

    # Fragments an HTMX view renders directly have no parent in the graph: the
    # view is their caller. They belong to their app, which is the directory
    # they sit in, so borrow the bundle of that app's pages.
    for path in SOURCES:
        if reach[path]:
            continue
        app = path.split("/")[0]
        siblings = {
            b for other, bundles in reach.items() if other.startswith(f"{app}/") for b in bundles
        }
        reach[path] = siblings or {"app.css"}

    return {path: frozenset(bundles) for path, bundles in reach.items()}


REACHABLE = _bundles_by_template()


def _defined(name: str, bundle: str) -> bool:
    classes = BUNDLES[bundle]
    if name in classes:
        return True
    if name.endswith("-"):
        # An interpolated name: `stat-tile--{{ status }}`. Satisfied if the
        # bundle defines any member of that family.
        return any(known.startswith(name) for known in classes)
    # A BEM block whose own selector carries no declarations — `.mega` exists
    # only to scope `.mega__menu`, and Sass emits nothing for an empty rule. The
    # block is real if the bundle knows any of its elements or modifiers.
    return any(known.startswith((f"{name}__", f"{name}--")) for known in classes)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("layout", "bundle"), sorted(LAYOUT_BUNDLES.items()))
def test_layout_loads_the_expected_bundle(layout: str, bundle: str) -> None:
    """The map above is a claim about the layouts; hold it to it."""
    assert f"css/{bundle}" in SOURCES[layout]


@pytest.mark.parametrize("path", sorted(SOURCES))
def test_every_class_used_is_defined_in_a_reachable_bundle(path: str) -> None:
    used = _classes_used(SOURCES[path])
    if not used:
        pytest.skip("no class attributes")

    bundles = REACHABLE[path]
    missing = sorted(name for name in used if not any(_defined(name, bundle) for bundle in bundles))

    assert not missing, (
        f"{path} uses classes that {' / '.join(sorted(bundles))} does not define: "
        f"{', '.join(missing)}. Either add the rule to the right SCSS partial and "
        f"run `npm run build:css`, or use a class that exists."
    )


def test_stylesheets_are_current() -> None:
    """The committed CSS must be no older than the SCSS it is built from.

    Without this, every assertion above tests a stale artefact: a developer adds
    a class to a partial, forgets to build, and the suite passes green against
    yesterday's stylesheet.
    """
    scss = max(p.stat().st_mtime for p in (ROOT / "assets" / "scss").rglob("*.scss"))
    for bundle in BUNDLES:
        built = (CSS / bundle).stat().st_mtime
        assert built >= scss - 1, (
            f"{bundle} is older than the SCSS sources. Run `npm run build:css`."
        )
