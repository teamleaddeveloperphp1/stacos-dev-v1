"""
Fixtures for the browser tests.

Playwright's synchronous API runs an event loop, and Django refuses ORM calls
from inside one. The fixtures here only touch the database from the *test*
thread, never from a browser callback, so the guard is opted out of rather than
worked around — this is the documented approach for the sync Playwright API with
Django, and it is scoped to this package alone.
"""

from __future__ import annotations

import contextlib
import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

import pytest


@pytest.fixture(autouse=True)
def fail_on_console_errors(page, request):
    """Surface JavaScript errors and failed asset loads as test failures.

    Without this, a broken bundle or a 404 on app.js shows up only as "the thing
    I clicked did nothing" — which is a slow and confusing way to find out that
    no JavaScript ran at all.
    """
    # A test that asserts on error *handling* has to cause an error, and the
    # console will say so. `@pytest.mark.expect_console_errors("403")` names the
    # substrings that are expected; everything else still fails the test.
    marker = request.node.get_closest_marker("expect_console_errors")
    expected: tuple[str, ...] = marker.args if marker else ()

    problems: list[str] = []
    page.on(
        "console",
        lambda msg: (
            problems.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None
        ),
    )
    page.on("pageerror", lambda exc: problems.append(f"pageerror: {exc}"))
    page.on(
        "response",
        lambda r: (
            problems.append(f"{r.status} {r.url}")
            if r.status >= 400 and (".js" in r.url or ".css" in r.url)
            else None
        ),
    )
    yield

    # Let anything still in flight finish before the database fixture tears down.
    # Several pages load panels with `hx-trigger="load"`, and a request that
    # arrives after teardown has begun both fails noisily and holds a connection
    # the flush then deadlocks against — which surfaces as an unrelated test
    # erroring, not this one.
    # Suppressed rather than asserted: the page may already be closed, and this
    # is tidy-up, not the thing under test.
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle")

    unexpected = [
        problem for problem in problems if not any(expected in problem for expected in expected)
    ]
    assert not unexpected, "browser reported problems:\n  " + "\n  ".join(unexpected)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    """A desktop viewport, and locale/timezone pinned.

    Dates render through Django's Indian locale, so a browser claiming another
    timezone would make assertions about rendered dates flap.
    """
    return {
        **browser_context_args,
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-IN",
        "timezone_id": "Asia/Kolkata",
    }


@pytest.fixture
def reference_data(db) -> None:
    """Reload the platform reference data this test needs.

    ``tests/conftest.py`` loads the jurisdiction pack and the compliance catalog
    once per session, before the per-test transaction opens. That is the right
    trade for the ordinary suite — but these tests run with
    ``django_db(transaction=True)``, which truncates every table between tests,
    so by the time a browser test runs the catalog may already have been wiped by
    an earlier one. Materialisation then quietly produces nothing and the test
    fails claiming the calendar is empty, which it is.

    Request this *before* any fixture that materialises a register.
    """
    from django.core.management import call_command

    from stacos.catalog.models import ComplianceDefinition
    from stacos.core.scope import platform_scope

    with platform_scope(reason="test-fixture"):
        already_loaded = ComplianceDefinition.objects.exists()

    if not already_loaded:
        call_command("sync_system_roles", verbosity=0)
        call_command("loadpack", "IN", verbosity=0)
        call_command("loadcatalog", verbosity=0)
