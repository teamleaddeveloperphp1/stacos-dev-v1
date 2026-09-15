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
        "no_tenant": None,
        "user": SimpleNamespace(
            initials="AR",
            full_name="Anita Rao",
            email="anita@acme.example",
            is_authenticated=True,
        ),
        "donut_segments": [
            {
                "status": "overdue",
                "label": "Overdue",
                "value": 3,
                "pct": 30.0,
                "dasharray": "30 70",
                "offset": 0,
            },
            {
                "status": "on-track",
                "label": "Pending",
                "value": 7,
                "pct": 70.0,
                "dasharray": "70 30",
                "offset": -30,
            },
        ],
        "bar_rows": [
            {"label": "Indirect tax", "count": 12, "pct": 100.0, "color": "category-1"},
            {"label": "1–7 days late", "count": 3, "pct": 25.0, "color": "status-overdue"},
        ],
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
    """Between sign-up and the session's tenant binding taking effect, the shell
    still has to draw."""
    html = render_to_string(GALLERY, gallery_context)
    assert "No organisation" in html


# ===========================================================================
# 5. Reachability
#
# A view that renders, declares a permission and has a route can still be
# unreachable, because nothing anywhere links to it. Thirteen of them were, at
# once, including the only "Rebuild calendar" button in the product and a
# complete cap-table screen. Nothing else in the suite notices: every one of
# those routes had passing view tests, because a test calls `reverse()` and a
# user cannot.
# ===========================================================================

#: Namespaces whose routes are deliberately not reachable from a template.
UNLINKED_NAMESPACES: frozenset[str] = frozenset(
    {
        # The mobile API. Consumed by the app over HTTP; a template link would
        # make no sense.
        "api",
    }
)

#: Unnamespaced infrastructure routes, called by machines rather than people.
UNLINKED_BARE: frozenset[str] = frozenset({"healthz", "robots", "sitemap"})

#: Individual routes with no link, each with the reason it is deliberate.
#: Anything added here needs a reason that survives being read aloud.
#: The obligation detail page asks "is this filing completed?" and nothing else;
#: the named checklist it replaced is parked, not deleted. Its rows, its
#: services and these endpoints are all still here and still tested directly,
#: because the maker-checker flow is expected back for the firms that want it —
#: but nothing links to them, and an endpoint kept for a future UI should say so
#: here rather than look like a link somebody forgot.
_CHECKLIST_PARKED = (
    "The per-step checklist is not rendered on the detail page for now — see "
    "`templates/obligations/_fragments/status_questions.html`. The endpoint, "
    "its permission check and its tests are kept for when it returns."
)

UNLINKED_ROUTES: dict[str, str] = {
    "billing:invoice_issue": (
        "Raising a subscription invoice out of cycle is a vendor operation, not "
        "a customer one — the recurring path is automated. No system role holds "
        "`billing.invoice.issue`, so a button would be pressable by nobody. "
        "Same for `billing.invoice.void`, which is why the void control in the "
        "invoice panel stays hidden."
    ),
    "compliance:step_toggle": _CHECKLIST_PARKED,
    "compliance:step_block": _CHECKLIST_PARKED,
    "compliance:step_nudge": _CHECKLIST_PARKED,
}


def _route_names() -> set[str]:
    """Every named, routable STACOS view, fully qualified."""
    from django.urls import get_resolver
    from django.urls.resolvers import URLPattern, URLResolver

    found: set[str] = set()

    def walk(resolver: URLResolver, namespaces: list[str]) -> None:
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                walk(pattern, [*namespaces, pattern.namespace] if pattern.namespace else namespaces)
            elif isinstance(pattern, URLPattern) and pattern.name:
                module = getattr(pattern.callback, "__module__", "") or ""
                if module.startswith("stacos."):
                    found.add(":".join([*namespaces, pattern.name]) if namespaces else pattern.name)

    walk(get_resolver(), [])
    return found


def _referenced_names() -> set[str]:
    """Route names named by a template, or by Python that builds navigation.

    Both halves matter. Most links are ``{% url %}`` in a template, but the
    marketing navigation and the command palette are built from tuples of route
    names in Python, and a route reached only that way is still reachable.
    """
    root = TEMPLATES_DIR.parent
    referenced: set[str] = set()

    for path in TEMPLATES_DIR.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        referenced |= set(re.findall(r"\{%\s*url\s+['\"]([^'\"]+)['\"]", source))

    # A fully-qualified `namespace:name` never appears in a `urls.py` definition
    # (which names the route bare), so a literal match here cannot be the
    # declaration mistaking itself for a reference.
    quoted = re.compile(r"['\"]([a-z_]+:[a-z_]+)['\"]")
    for path in (root / "stacos").rglob("*.py"):
        if "migrations" in path.parts:
            continue
        referenced |= set(quoted.findall(path.read_text(encoding="utf-8")))

    return referenced


def test_every_route_is_reachable() -> None:
    """Every route is linked from somewhere, or explicitly declared unlinked."""
    referenced = _referenced_names()

    unreachable = sorted(
        name
        for name in _route_names()
        if name not in referenced
        and name not in UNLINKED_ROUTES
        and name not in UNLINKED_BARE
        and name.split(":")[0] not in UNLINKED_NAMESPACES
    )

    assert not unreachable, (
        f"These routes have no link anywhere, so no user can reach them: "
        f"{unreachable}. Either link them, or add each to UNLINKED_ROUTES with "
        f"the reason it is deliberate."
    )


def test_the_unlinked_allowlist_has_no_stale_entries() -> None:
    """An allowlist nobody prunes stops describing the application."""
    known = _route_names()
    stale = sorted(name for name in UNLINKED_ROUTES if name not in known)
    assert not stale, f"UNLINKED_ROUTES names routes that no longer exist: {stale}"

    referenced = _referenced_names()
    now_linked = sorted(name for name in UNLINKED_ROUTES if name in referenced)
    assert not now_linked, (
        f"These are listed as deliberately unlinked but something links them now: "
        f"{now_linked}. Remove them from UNLINKED_ROUTES."
    )


# ===========================================================================
# 6. Two mistakes that are cheap to make and expensive to find
# ===========================================================================


def test_every_layout_loads_the_webfonts() -> None:
    """All three layouts pull in the same two faces, from one partial.

    The product spent a long time naming a font nothing ever fetched, so every
    screen rendered in whatever the operating system offered and the application
    looked unfinished beside its own design work. Worse than looking unfinished
    is looking *inconsistent*: a sign-in page in a different face from the screen
    behind it reads as broken before the user has typed anything. One include,
    asserted in all three, is what prevents that drifting apart again.
    """
    for layout in ("app_shell.html", "auth.html", "public.html"):
        source = (TEMPLATES_DIR / "layouts" / layout).read_text(encoding="utf-8")
        assert 'include "layouts/_fonts.html"' in source, (
            f"layouts/{layout} does not include the webfont partial. Every layout "
            f"loads the same faces, or the product renders in two of them."
        )

    fonts = (TEMPLATES_DIR / "layouts" / "_fonts.html").read_text(encoding="utf-8")
    for family in ("Fraunces", "IBM+Plex+Sans", "IBM+Plex+Mono"):
        assert family in fonts, f"{family} is named in the tokens but not requested"
    assert "display=swap" in fonts, (
        "Without display=swap, text is invisible until the webfont arrives — "
        "which on a patchy mobile connection is the whole screen for seconds."
    )


def test_the_app_shell_does_not_push_urls() -> None:
    """`hx-push-url` on the shell is inherited by everything inside it.

    Every modal open, panel action, "Load more" and palette keystroke would push
    its own endpoint into browser history, and refreshing or pressing Back would
    land the user on a bare fragment. It is also unnecessary: HTMX pushes the URL
    for boosted links and forms on its own.
    """
    source = (TEMPLATES_DIR / "layouts" / "app_shell.html").read_text(encoding="utf-8")
    shell = source[source.index('<div class="app-shell"') :]
    shell = shell[: shell.index(">")]
    assert "hx-push-url" not in shell, (
        "The app shell must not set hx-push-url — see the comment above the "
        "element. Links that genuinely want an address change declare it "
        "individually."
    )


#: An element that fetches itself as soon as it appears. `hx-target` is not
#: quoted here because the attribute may sit on any line of a multi-line tag.
_SELF_LOADING_TRIGGER = re.compile(r'hx-trigger="[^"]*\b(?:load|revealed)\b')


def test_load_triggered_fetches_declare_their_own_target() -> None:
    """A box that fetches itself on load must say where the response goes.

    `hx-target` is inherited, and the shell sets it to `#main` so that boosted
    navigation replaces the page body. A lazily-loaded panel that declares no
    target of its own therefore resolves to `#main`, and because these panels
    swap `outerHTML`, the response replaces the entire main region with one
    card — hiding everything else on the page.

    The second-order damage is the one that reads as "the app froze": `#main`
    carries `hx-history-elt` and is the target of every link in the shell, so
    once it has been replaced the sidebar, the calendar link and the keyboard
    shortcuts all stop responding until a manual reload. It happened on the
    obligation detail page and on the entity detail page at once, and neither
    produced an error anywhere.
    """
    offenders: list[str] = []
    for path in _template_paths():
        source = (TEMPLATES_DIR / path).read_text(encoding="utf-8")
        for tag in re.finditer(r"<[a-zA-Z][^<>]*>", source, re.DOTALL):
            markup = tag.group(0)
            if not _SELF_LOADING_TRIGGER.search(markup):
                continue
            if "hx-target" in markup:
                continue
            line = source.count("\n", 0, tag.start()) + 1
            offenders.append(f"{path}:{line}")

    assert not offenders, (
        f"These fetch themselves on load without declaring hx-target: {offenders}. "
        f'They inherit the shell\'s hx-target="#main" and replace the whole main '
        f'region with themselves. Add hx-target="this".'
    )


def test_no_links_point_at_a_bare_fragment() -> None:
    """`href="#"` means "go nowhere", and reads to a user as a broken button.

    Marketing templates are excluded: an anchor there is legitimately a
    scroll-to-section target.
    """
    offenders: list[str] = []
    for path in _template_paths():
        if path.startswith("marketing/"):
            continue
        source = (TEMPLATES_DIR / path).read_text(encoding="utf-8")
        offenders += [
            f"{path}:{i + 1}"
            for i, line in enumerate(source.splitlines())
            if re.search(r"""href=["']#["']""", line)
        ]
    assert not offenders, (
        f"Links pointing at a bare fragment: {offenders}. Either wire them up or "
        f"remove them — a button that does nothing is worse than no button."
    )


# ===========================================================================
# 7. The sidebar's active item
#
# The highlight is applied by `syncNav()` in assets/js/app.js, from the prefix
# each link declares. Two things have to hold for that to work, and neither is
# visible from the JavaScript alone: every nav link has to declare a prefix, and
# every prefix has to be a URL this project actually serves. Both are checked
# here because the JavaScript is not in this suite.
# ===========================================================================

_NAV_LINK = re.compile(r"<a\b[^>]*class=\"app-nav__link\"[^>]*>", re.DOTALL)


def _shell_source() -> str:
    return (TEMPLATES_DIR / "layouts" / "app_shell.html").read_text(encoding="utf-8")


def test_every_sidebar_link_declares_the_prefix_it_owns() -> None:
    """A link with no `data-nav-match` can never be highlighted.

    It is a silent failure: the page loads, the link works, and the sidebar just
    never marks it — which is exactly the state `practice:profitability` and
    `accounts:security` were already in before the highlight moved client-side.
    """
    links = _NAV_LINK.findall(_shell_source())
    assert links, "no .app-nav__link elements found — has the shell been restructured?"

    missing = [link for link in links if "data-nav-match" not in link]

    assert not missing, (
        f"{len(missing)} sidebar links declare no data-nav-match and can never be "
        f"highlighted: {missing}"
    )


def test_the_shell_does_not_also_compute_the_active_item() -> None:
    """One rule, in one place.

    A server-rendered `aria-current` and the client-side one drifted apart the
    moment they used different comparisons — which is what had happened: some
    links compared by `url_name`, some by `namespace`, two not at all. The
    server-side copy is gone and must stay gone.
    """
    source = _shell_source()
    markup = re.sub(r"\{% comment %\}.*?\{% endcomment %\}", "", source, flags=re.DOTALL)

    assert 'aria-current="page"' not in markup, (
        "The app shell renders aria-current again. syncNav() in assets/js/app.js "
        "is the single rule; a second copy here is how the highlight goes stale."
    )


def test_every_declared_nav_prefix_is_a_real_url() -> None:
    """A prefix that resolves to nothing highlights nothing, forever."""
    from django.urls import Resolver404, resolve

    unresolvable: list[str] = []
    for link in _NAV_LINK.findall(_shell_source()):
        match = re.search(r'data-nav-match="([^"]+)"', link)
        if match is None:
            continue
        try:
            resolve(match.group(1))
        except Resolver404:
            unresolvable.append(match.group(1))

    assert not unresolvable, (
        f"These data-nav-match prefixes resolve to no view: {unresolvable}. "
        f"A prefix nothing serves can never match the address bar."
    )


def test_no_form_or_link_replaces_the_whole_body() -> None:
    """`hx-target="body"` destroys the shell it is swapping inside.

    Everything the shell owns — Alpine's state, the sidebar, `#modal-container`,
    `#toast-stack`, the palette — is re-created from the response, and anything
    the response does not contain simply disappears. It is only ever reached for
    by a view that has no fragment path, which is a bug to fix at the view rather
    than to work around here.
    """
    offenders: list[str] = []
    for path in _template_paths():
        source = (TEMPLATES_DIR / path).read_text(encoding="utf-8")
        # Comments are blanked rather than removed, so line numbers still point
        # at the offending line. A comment explaining why this attribute is gone
        # is not an occurrence of it.
        markup = re.sub(
            r"\{% comment %\}.*?\{% endcomment %\}",
            lambda match: re.sub(r"[^\n]", " ", match.group(0)),
            source,
            flags=re.DOTALL,
        )
        offenders += [
            f"{path}:{i + 1}"
            for i, line in enumerate(markup.splitlines())
            if re.search(r"""hx-target=["']body["']""", line)
        ]

    assert not offenders, (
        f"These replace the entire document body: {offenders}. Give the view a "
        f"fragment render path instead — see stacos.core.htmx."
    )
