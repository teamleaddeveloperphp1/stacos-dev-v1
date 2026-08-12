"""
Public navigation and footer, on every render.

Registered as a context processor so the header, the mega menu and the five
footer columns are available to any template that extends ``layouts/public.html``
— including the error pages, which are rendered without going through a
marketing view and would otherwise draw a chrome-less page at exactly the moment
a visitor most needs a way out.

Safe to run everywhere because it touches **module constants only**: no query,
no request attribute, no settings lookup that can fail. A context processor runs
on every render in the product, including every HTMX fragment in the
application, so anything more expensive than this belongs in a view.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from stacos.marketing.content import COMPANY, FOOTER, NAV

__all__ = ["marketing_chrome"]


def marketing_chrome(request: HttpRequest) -> dict[str, Any]:  # noqa: ARG001
    return {
        "nav_sections": NAV,
        "footer_sections": FOOTER,
        "company": COMPANY,
    }
