"""
Narrowing helpers for Django's loosely-typed request attributes.

``request.user`` is typed as ``User | AnonymousUser`` everywhere, because Django
cannot know that a view is behind authentication. In STACOS almost every view
*is* — ``require_permission`` cannot pass without a bound scope, which requires an
authenticated user — so rather than sprinkling casts, views call these.

The assertion is not decorative: if a view is ever reached anonymously, failing
loudly at the top beats an ``AttributeError`` three frames down.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from django.http import HttpRequest

if TYPE_CHECKING:
    from stacos.accounts.models import User
    from stacos.core.scope import AccessScope

__all__ = ["current_user", "request_scope"]


def current_user(request: HttpRequest) -> User:
    """Return the authenticated user, narrowed from Django's union type."""
    user = request.user
    if not user.is_authenticated:
        raise PermissionError(
            "This view requires an authenticated user. Add @require_permission, "
            "or mark it @public_view if it is genuinely open."
        )
    # django-stubs narrows the union on `is_authenticated`, so no cast is needed.
    return user


def request_scope(request: HttpRequest) -> AccessScope:
    """Return the access scope bound to this request by ``ScopeMiddleware``."""
    scope = getattr(request, "access_scope", None)
    if scope is None:
        raise PermissionError("No access scope is bound to this request.")
    return cast("AccessScope", scope)
