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

    ``nav_memberships`` honours that rule: it issues no query of its own. The
    tenant switcher in the shell needs every organisation the user belongs to,
    and the scope resolver already reads exactly that list during the bootstrap
    it has to perform anyway, leaving it on the request. Sorting a handful of
    rows that are already in memory is free; querying for them here would not be,
    and querying for them through the scoped manager would return only the
    tenant the user is currently in — which is not a switcher.

    Before this existed the shell passed ``nav_memberships`` and nothing set it,
    so it resolved to the empty string and every user, however many organisations
    they belonged to, was told they belonged to none.
    """
    scope = getattr(request, "access_scope", None)
    memberships = getattr(request, "user_memberships", None) or []
    return {
        "access_scope": scope,
        "current_tenant": getattr(request, "tenant", None),
        "current_entity": getattr(request, "entity", None),
        "request_id": getattr(request, "request_id", ""),
        "is_htmx": bool(getattr(request, "htmx", False)),
        "nav_memberships": sorted(memberships, key=lambda m: m.tenant.name),
        "DEBUG": settings.DEBUG,
    }
