"""
Request-cycle middleware.

Ordering matters and is fixed in ``config/settings/base.py``:

1. :class:`RequestContextMiddleware` runs early. It **clears** the log/audit
   context before binding, because binding without clearing leaks one request's
   tenant into the next on a reused worker thread.
2. Authentication, then the security-stamp and dual-OTP gates.
3. :class:`ScopeMiddleware` last, immediately before the view. It resolves what
   this actor may see, binds it, and pushes the same answer down to PostgreSQL
   as a Row-Level Security parameter.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse

from stacos.core.request_context import RequestMeta, bind_request_meta, clear_request_meta
from stacos.core.rls import apply_rls_tenants
from stacos.core.scope import AccessScope, _current

__all__ = ["RequestContextMiddleware", "ScopeMiddleware"]

#: Prefixes that never touch tenant data, so they skip scope resolution and the
#: RLS transaction entirely. Marketing pages are aggressively cached and must not
#: pay for a database round trip.
UNSCOPED_PREFIXES = (
    "/static/",
    "/media/",
    "/healthz",
    "/__debug__/",
)


class RequestContextMiddleware:
    """Assign a request id and bind ambient metadata for logs and audit rows."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Clear first, always. See the module docstring.
        clear_request_meta()

        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.request_id = request_id  # type: ignore[attr-defined]

        bind_request_meta(
            RequestMeta(
                request_id=request_id,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("User-Agent", "")[:512],
                path=request.path,
            )
        )

        try:
            response = self.get_response(request)
        finally:
            clear_request_meta()

        response["X-Request-ID"] = request_id
        return response


class ScopeMiddleware:
    """Resolve, bind and enforce the caller's access scope.

    Three things happen here, in order:

    1. The scope is resolved from the authenticated user, their selected tenant,
       and any active engagements. This is the only place cross-tenant access is
       granted, so it is the only place that has to be right.
    2. The scope is bound to the context, which is what makes
       ``Model.objects`` work at all.
    3. The same tenant set is pushed to PostgreSQL via ``set_config`` inside an
       explicit transaction, so Row-Level Security policies enforce the boundary
       even if application code is bypassed by raw SQL.

    The transaction is opened here rather than with ``ATOMIC_REQUESTS`` because
    middleware runs *outside* that transaction — a plain ``SET`` would then leak
    across pooled connections, which is worse than no RLS at all.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.rls_enabled = getattr(settings, "STACOS_RLS_ENABLED", True)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path.startswith(UNSCOPED_PREFIXES):
            request.access_scope = None  # type: ignore[attr-defined]
            return self.get_response(request)

        scope = self._resolve(request)
        request.access_scope = scope  # type: ignore[attr-defined]

        if scope is None:
            return self.get_response(request)

        token = _current.set(scope)
        try:
            if self.rls_enabled and not scope.bypass:
                with transaction.atomic():
                    self._apply_rls(scope)
                    return self.get_response(request)
            return self.get_response(request)
        finally:
            _current.reset(token)

    def _resolve(self, request: HttpRequest) -> AccessScope | None:
        """Build the scope, or ``None`` for anonymous/public requests.

        Imported lazily: this module is loaded before the app registry is ready.
        """
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None

        from stacos.tenancy.scope_resolver import resolve_scope_for_request

        return resolve_scope_for_request(request)

    @staticmethod
    def _apply_rls(scope: AccessScope) -> None:
        """Publish the readable tenant set to PostgreSQL for this transaction."""
        apply_rls_tenants(scope.readable_tenant_ids)


def _client_ip(request: HttpRequest) -> str | None:
    """Best-effort client IP.

    ``X-Forwarded-For`` is only trusted because the deployment terminates TLS at
    a known proxy; the left-most entry is the original client.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.META.get("REMOTE_ADDR")
