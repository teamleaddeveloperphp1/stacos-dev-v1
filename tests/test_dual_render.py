"""
Every page in the application renders two ways.

One page is two templates: ``<app>/<name>.html`` extends the shell and its
``{% block main %}`` contains nothing but an include of
``<app>/_fragments/<name>_body.html``. A direct GET gets the page; an HTMX
request gets only the fragment. That is what makes deep links, the back button
and "open in new tab" work with no special handling.

A view that skips the second path answers a boosted click with a whole
``<!doctype html>`` document, which HTMX morphs into ``#main``. The page then
contains a second ``.app-shell`` inside the first: two sidebars, two ``#main``
elements, two ``#toast-stack``s, two ``#modal-container``s, two copies of
whatever step indicator the page had, and a re-executed ``Alpine.start()``. The
reports it produces do not sound like one bug — "blank page", "the sidebar stops
responding", "the setup steps are numbered twice" — which is why it went unfixed
on three pages at once.

``test_the_security_page_renders_both_ways`` in ``tests/test_htmx_navigation.py``
pinned this for one page after it happened there. This sweeps every page instead,
because the next occurrence will be on a page nobody has written yet.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

from stacos.accounts.models import User
from stacos.tenancy.models import Entity, Tenant
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


#: Routes that take no arguments and answer a GET with a page. A route with URL
#: parameters needs a fixture per parameter, which is what the module-level
#: feature tests already do; the argument-free ones are where the whole-document
#: mistake actually happens, because they are the ones reached from the sidebar.
def _argumentless_get_routes() -> list[str]:
    routes: list[str] = []

    def walk(resolver: URLResolver, prefix: str) -> None:
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                walk(pattern, prefix + str(pattern.pattern))
            elif isinstance(pattern, URLPattern):
                module = getattr(pattern.callback, "__module__", "") or ""
                route = prefix + str(pattern.pattern)
                if not module.startswith("stacos."):
                    continue
                if not route.startswith("app/"):
                    continue
                if "<" in route:
                    continue
                routes.append("/" + route)

    walk(get_resolver(), "")
    return sorted(set(routes))


#: Endpoints that are a fragment by nature and have no page form: a modal body, a
#: panel, a row. They answer an ordinary GET with the fragment too, deliberately.
#:
#: Listed rather than detected, so that a *new* fragment-only route fails this
#: sweep and somebody has to decide which kind it is. A modal that should have
#: been a page is a real mistake, and it is only visible at the moment the route
#: is added.
FRAGMENT_ONLY = {
    "/app/notifications/panel/",
    "/app/search/",
    # Modal bodies. Opened with hx-get into #modal-container from the page that
    # owns them; they have no standalone form and never appear in the address bar.
    "/app/documents/upload/",
    "/app/entities/new/",
    "/app/notices/new/",
    "/app/requests/new/",
    "/app/secretarial/meetings/new/",
}


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant, entity_a: Entity) -> Client:
    return sign_in(client, org_owner, step_up=True)


@pytest.mark.parametrize("path", _argumentless_get_routes())
def test_a_page_answers_a_boosted_click_with_a_fragment(signed_in: Client, path: str) -> None:
    """Not a whole second copy of the application.

    Skipped rather than asserted when the plain GET is not a page — a POST-only
    route, a redirect, or an endpoint that is a fragment by design.
    """
    page = signed_in.get(path)
    if page.status_code != 200 or b"<!doctype html>" not in page.content.lower():
        pytest.skip(f"{path} does not answer a plain GET with a page ({page.status_code})")
    if path in FRAGMENT_ONLY:
        pytest.skip(f"{path} is a fragment endpoint by design")

    fragment = signed_in.get(path, headers=HTMX)

    assert fragment.status_code == 200
    body = fragment.content
    assert b"<!doctype html>" not in body.lower(), (
        f"{path} returned a whole document to an HTMX caller; it would be morphed "
        f"into #main as a second application shell"
    )
    assert b'class="app-shell"' not in body, f"{path} nests a second shell inside #main"
    assert b'id="main"' not in body, f"{path} produces a duplicate #main"


@pytest.mark.parametrize("path", _argumentless_get_routes())
def test_a_direct_get_still_returns_the_whole_page(signed_in: Client, path: str) -> None:
    """The other half of the contract, and the reason deep links work.

    A view that returned only the fragment would break "open in new tab" and the
    address bar, which is the failure mode in the opposite direction.
    """
    response = signed_in.get(path)
    if response.status_code != 200:
        pytest.skip(f"{path} does not answer a plain GET with 200 ({response.status_code})")
    if path in FRAGMENT_ONLY:
        pytest.skip(f"{path} is a fragment endpoint by design")

    assert b"<!doctype html>" in response.content.lower(), (
        f"{path} answered a browser with a bare fragment"
    )


# ---------------------------------------------------------------------------
# The firm's own screens
#
# An organisation user cannot see these — the sweep above skips them with a 403 —
# so they need a caller who can. They are exactly the kind of page that gets
# missed: nobody on the product side opens them by accident.
# ---------------------------------------------------------------------------

PRACTICE_PAGES = ["/app/practice/", "/app/practice/profitability/"]


@pytest.fixture
def practice_partner(practice: Tenant) -> User:
    """A partner, not staff: profitability needs `practice.wip.view`.

    The margin on a client is deliberately not something everyone at the firm
    can see, so the sweep needs the role that can rather than a permission grant
    invented for the test.
    """
    from tests.conftest import _make_member

    return _make_member(
        practice, "partner@mehta.example", "Sunita Mehta", "+919800000201", "practice-partner"
    )


@pytest.mark.parametrize("path", PRACTICE_PAGES)
def test_the_practice_screens_render_both_ways(
    client: Client, practice_partner: User, engagement: object, path: str
) -> None:
    signed_in = sign_in(client, practice_partner, step_up=True)

    page = signed_in.get(path)
    fragment = signed_in.get(path, headers=HTMX)

    assert page.status_code == 200, f"{path} is unreachable for a practice user"
    assert fragment.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b'class="app-shell"' not in fragment.content


# ---------------------------------------------------------------------------
# The setup flow specifically
#
# It was the one views module in the project that never called
# `is_fragment_request`, and it is the flow a brand-new customer is now sent
# into, so it is worth asserting on directly rather than only through the sweep.
# ---------------------------------------------------------------------------

SETUP_PAGES = ["/app/start/", "/app/start/profile/", "/app/start/preview/"]


@pytest.fixture
def in_setup(client: Client, org_owner: User, org: Tenant) -> Client:
    """Signed in and part-way through the wizard, so every step renders."""
    signed_in = sign_in(client, org_owner, step_up=True)
    signed_in.post("/app/start/", {"pan": "AABCU9603R"})
    signed_in.post(
        "/app/start/profile/",
        {
            "name": "Nimbus Software",
            "entity_type": "PVT_LTD",
            "registered_office_state": "IN-KA",
        },
    )
    return signed_in


@pytest.mark.parametrize("path", SETUP_PAGES)
def test_every_setup_step_renders_both_ways(in_setup: Client, path: str) -> None:
    page = in_setup.get(path)
    fragment = in_setup.get(path, headers=HTMX)

    assert page.status_code == 200
    assert fragment.status_code == 200
    assert b"<!doctype html>" in page.content.lower()
    assert b"<!doctype html>" not in fragment.content.lower()
    assert b'class="app-shell"' not in fragment.content


@pytest.mark.parametrize("path", SETUP_PAGES)
def test_the_step_rail_is_numbered_once(in_setup: Client, path: str) -> None:
    """The reported "duplicate step numbers", asserted at its cause.

    The rail itself always emitted 1, 2, 3 exactly once. What produced a second
    set was the whole document being swapped into `#main`, bringing a second copy
    of everything with it.
    """
    body = in_setup.get(path).content.decode()

    rails = body.count('class="wizard__steps"')
    numbers = len(re.findall(r'class="wizard__step-number">(\d+)<', body))

    assert rails == 1, f"{path} rendered {rails} step rails"
    assert numbers == 3, f"{path} rendered {numbers} step numbers, expected 3"


def test_the_preview_declares_each_region_once(in_setup: Client) -> None:
    """Three duplicate id pairs used to be on the page before anything was clicked.

    Each region was declared by `preview_body.html` and then again by the
    `<c-oob>` at the root of the fragment it included — the same id twice, nested,
    with a meaningless `hx-swap-oob` on the inner one. Every answer submitted
    added another level.
    """
    body = in_setup.get("/app/start/preview/").content.decode()

    for region in ("onboarding-questions", "onboarding-preview", "onboarding-packs"):
        assert body.count(f'id="{region}"') == 1, f"{region} is declared more than once"

    assert "hx-swap-oob" not in body, (
        "an out-of-band marker in the first paint, where it means nothing"
    )
