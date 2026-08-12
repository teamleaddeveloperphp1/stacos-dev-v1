"""
Fixtures for the browser tests.

Playwright's synchronous API runs an event loop, and Django refuses ORM calls
from inside one. The fixtures here only touch the database from the *test*
thread, never from a browser callback, so the guard is opted out of rather than
worked around — this is the documented approach for the sync Playwright API with
Django, and it is scoped to this package alone.
"""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

import pytest


@pytest.fixture(autouse=True)
def fail_on_console_errors(page):
    """Surface JavaScript errors and failed asset loads as test failures.

    Without this, a broken bundle or a 404 on app.js shows up only as "the thing
    I clicked did nothing" — which is a slow and confusing way to find out that
    no JavaScript ran at all.
    """
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
    assert not problems, "browser reported problems:\n  " + "\n  ".join(problems)


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
