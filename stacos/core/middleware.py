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
from urllib.parse import quote

import structlog
from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from stacos.core.exceptions import PermissionDenied, StepUpRequired, UnscopedQueryError
from stacos.core.request_context import (
    RequestMeta,
    bind_request_meta,
    clear_request_meta,
    client_ip,
)
from stacos.core.rls import apply_rls_tenants
from stacos.core.scope import AccessScope, _current

logger = structlog.get_logger(__name__)

__all__ = ["AuthorizationExceptionMiddleware", "RequestContextMiddleware", "ScopeMiddleware"]

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
                ip_address=client_ip(request),
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

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            request.access_scope = None  # type: ignore[attr-defined]
            return self.get_response(request)

        if not self.rls_enabled:
            return self._serve(request, self._resolve(request))

        # The transaction opens *before* the scope is resolved, because resolving
        # it reads RLS-protected tables and has to lift the policy briefly to do
        # so (see `rls_bootstrap`). The setting is transaction-local, so there
        # has to be a transaction for it to be local to.
        with transaction.atomic():
            scope = self._resolve(request)
            self._apply_rls(scope)
            return self._serve(request, scope)

    def _serve(self, request: HttpRequest, scope: AccessScope | None) -> HttpResponse:
        request.access_scope = scope  # type: ignore[attr-defined]
        if scope is None:
            return self.get_response(request)

        token = _current.set(scope)
        try:
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
    def _apply_rls(scope: AccessScope | None) -> None:
        """Publish the readable tenant set to PostgreSQL for this transaction.

        An unresolved scope publishes the empty set, so the policy fails closed
        rather than leaving whatever the bootstrap left behind.
        """
        apply_rls_tenants(scope.readable_tenant_ids if scope else frozenset())


class AuthorizationExceptionMiddleware:
    """Turn authorisation failures into responses instead of 500s.

    ``require_permission`` raises rather than returning, so that a view cannot
    accidentally continue past a failed check. Something has to catch those
    raises and decide what the user should see, and doing it in one place is
    what keeps the answer consistent:

    * **Not signed in** → the sign-in page, with ``next`` preserved. An anonymous
      visitor hitting an app URL is an ordinary event, not an error.
    * **Signed in, lacking the permission** → 403. Deliberately *not* a redirect:
      bouncing an authenticated user to a login page they are already past is
      the single most confusing thing an app can do.
    * **Needs step-up** → the re-authentication screen, returning here after.

    HTMX requests get ``HX-Redirect`` rather than a 302, because HTMX follows a
    redirect and swaps the result into the target — which would inject a login
    form into the middle of a dashboard.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_exception(self, request: HttpRequest, exception: Exception) -> HttpResponse | None:
        if isinstance(exception, StepUpRequired):
            target = f"{reverse('accounts:step_up')}?next={quote(exception.next_url or request.get_full_path())}"
            return self._redirect(request, target)

        if isinstance(exception, PermissionDenied):
            user = getattr(request, "user", None)
            if user is None or not user.is_authenticated:
                target = f"{reverse('accounts:login')}?next={quote(request.get_full_path())}"
                return self._redirect(request, target)

            logger.info(
                "authz.denied",
                permission=exception.permission,
                path=request.path,
                user_id=str(user.pk),
            )
            return render(
                request,
                "errors/403.html",
                {"permission": exception.permission},
                status=403,
            )

        if isinstance(exception, UnscopedQueryError):
            # A programming error, not a user error. Let it surface as a 500 so
            # it is impossible to ignore — but log it with enough context to fix.
            logger.error("tenancy.unscoped_query", path=request.path, detail=str(exception))
            return None

        return None

    @staticmethod
    def _redirect(request: HttpRequest, target: str) -> HttpResponse:
        if getattr(request, "htmx", False):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = target
            return response
        return redirect(target)
