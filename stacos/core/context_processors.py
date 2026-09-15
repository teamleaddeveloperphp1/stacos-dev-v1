"""Template context available on every page."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest


def stacos_context(request: HttpRequest) -> dict[str, Any]:
    """Expose the current scope and tenant to templates.

    Deliberately thin. Anything that needs a query belongs in a view or a
    fragment-cached component — a context processor runs on every render,
    including every HTMX fragment, so a query here is a query on every
    interaction in the product.
    """
    scope = getattr(request, "access_scope", None)
    return {
        "access_scope": scope,
        "current_tenant": getattr(request, "tenant", None),
        "current_entity": getattr(request, "entity", None),
        "request_id": getattr(request, "request_id", ""),
        "is_htmx": bool(getattr(request, "htmx", False)),
        "DEBUG": settings.DEBUG,
    }
