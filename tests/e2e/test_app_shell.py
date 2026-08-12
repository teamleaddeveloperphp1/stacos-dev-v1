"""
Browser tests for the things only a browser can prove.

Everything else in this suite runs through Django's test client, which is faster
and covers routing, permissions and rendering. These four tests exist because
what they check is *client-side behaviour*: does the shell actually stay put
during navigation, does the palette respond to the keyboard, does the modal open
and close. A view test cannot answer any of those, and getting them wrong is
invisible until someone clicks.

Run with:  uv run pytest tests/e2e -m e2e
Needs:     uv run playwright install chromium
"""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from playwright.sync_api import Page, expect

from stacos.accounts.middleware import SESSION_VERIFIED_KEY
from stacos.core.scope import platform_scope
from stacos.tenancy.models import Entity

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

User = get_user_model()


@pytest.fixture
def signed_in_page(page: Page, live_server, org_owner: User, entity_a: Entity) -> Page:
    """A browser session already past the dual-OTP gate.

    The gate itself is covered by the view tests, which can read the codes out of
    the memory backends. Repeating it here would only make these tests slower and
    more fragile.
    """
    from django.conf import settings
    from django.contrib.sessions.backends.db import SessionStore

    session = SessionStore()
    session[SESSION_VERIFIED_KEY] = True
    session["_auth_user_id"] = str(org_owner.pk)
    session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    session["_auth_user_hash"] = org_owner.get_session_auth_hash()
    session["stacos_security_stamp"] = str(org_owner.security_stamp)
    session.create()

    page.goto(f"{live_server.url}/healthz")
    page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": session.session_key or "",
                "url": live_server.url,
            }
        ]
    )
    page.goto(f"{live_server.url}/app/")
    return page


def test_navigation_never_reloads_the_shell(signed_in_page: Page, live_server) -> None:
    """The claim the whole architecture rests on.

    A marker is planted on the shell element; if navigation reloads the document
    the marker is gone. This is the difference between "a website" and "an app",
    and it is otherwise very easy to lose without anyone noticing.
    """
    page = signed_in_page
    page.evaluate("document.querySelector('.app-sidebar').dataset.stacosMarker = 'alive'")

    page.click("a[href='/app/entities/']")
    expect(page).to_have_url(f"{live_server.url}/app/entities/")
    expect(page.locator("h1")).to_contain_text("Entities")

    marker = page.evaluate("document.querySelector('.app-sidebar').dataset.stacosMarker")
    assert marker == "alive", "the shell re-rendered — navigation is not swapping #main"


def test_command_palette_opens_navigates_and_closes(
    signed_in_page: Page, live_server, entity_a: Entity
) -> None:
    """Ctrl-K, type, Enter — the whole point of the palette.

    Written after the palette rendered correctly but did nothing on click: its
    items were plain links outside the boosted region, so choosing "Entities"
    from the entities page was a full reload to the same URL, indistinguishable
    from nothing happening.
    """
    page = signed_in_page

    page.keyboard.press("Control+k")
    palette = page.locator(".palette")
    expect(palette).to_be_visible()

    # Search reaches the server and returns tenant-scoped results.
    page.fill(".palette__input", entity_a.name[:6])
    expect(page.locator(f"[data-palette-item]:has-text('{entity_a.name}')")).to_be_visible()

    # Enter opens the highlighted row.
    page.keyboard.press("Enter")
    expect(palette).to_be_hidden()
    expect(page).to_have_url(f"{live_server.url}/app/entities/{entity_a.pk}/")
    expect(page.locator("h1")).to_contain_text(entity_a.name)


def test_command_palette_closes_on_escape(signed_in_page: Page) -> None:
    page = signed_in_page
    page.keyboard.press("Control+k")
    expect(page.locator(".palette")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".palette")).to_be_hidden()


def test_entity_can_be_created_from_the_modal(signed_in_page: Page, live_server) -> None:
    """Open the modal, fill it, submit, and see the row appear without a reload."""
    page = signed_in_page
    page.goto(f"{live_server.url}/app/entities/")

    page.click("button:has-text('Add entity')")
    modal = page.locator(".modal.show")
    expect(modal).to_be_visible()

    page.fill("#id_name", "Playwright Ventures Pvt Ltd")
    page.select_option("#id_entity_type", "PVT_LTD")
    page.click("button[type=submit]:has-text('Add entity')")

    expect(modal).to_be_hidden()
    expect(page.locator("#entity-rows")).to_contain_text("Playwright Ventures Pvt Ltd")

    # A platform scope is needed even for `objects_unscoped`: that manager
    # bypasses the application filter, but Row-Level Security still applies at
    # the database and this thread has no tenant bound.
    with platform_scope(reason="e2e-assertion"):
        assert Entity.objects_unscoped.filter(name="Playwright Ventures Pvt Ltd").exists()


def test_invalid_form_keeps_the_modal_open(signed_in_page: Page, live_server) -> None:
    """A validation failure must not discard what the user typed."""
    page = signed_in_page
    page.goto(f"{live_server.url}/app/entities/")

    page.click("button:has-text('Add entity')")
    page.fill("#id_legal_name", "Something Memorable Private Limited")
    page.click("button[type=submit]:has-text('Add entity')")

    expect(page.locator(".modal.show")).to_be_visible()
    expect(page.locator("#id_legal_name")).to_have_value("Something Memorable Private Limited")
