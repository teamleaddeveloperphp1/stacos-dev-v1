"""
Binding a tenant scope to an API request.

`ScopeMiddleware` cannot do this job for the API, and the reason is structural
rather than an oversight: **DRF authenticates inside the view.** Middleware runs
before that, sees `AnonymousUser`, and correctly binds nothing. Every scoped
query in a DRF view would then raise, and — worse — anything reading through
``objects_unscoped`` would silently return nothing at all, because Row-Level
Security fails closed. That is the shape of bug that looks like "the API works,
it just says the user has no tenants".

So the API resolves its own scope, after authentication and before permissions
are checked, using the *same* resolver the web request path uses. Not a parallel
implementation — `resolve_scope_for_request` is the single place in STACOS where
cross-tenant access is granted, and there must go on being only one.

The ordering inside `initial()` is load-bearing:

1. authenticate, so there is a user to resolve a scope for;
2. open a transaction, because the RLS setting is transaction-local;
3. resolve and bind the scope, and publish its tenant set to PostgreSQL;
4. *then* let DRF check `permission_classes`, which read that scope.

Everything unwinds on the way out, in reverse, whether the handler returned or
raised.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from django.conf import settings
from django.db import transaction
from rest_framework.request import Request
from rest_framework.views import APIView

from stacos.core.rls import apply_rls_tenants
from stacos.core.scope import AccessScope, _current
from stacos.tenancy.scope_resolver import resolve_scope_for_request

__all__ = ["ScopedAPIView"]


@contextmanager
def _bound(scope: AccessScope | None) -> Iterator[None]:
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)


class ScopedAPIView(APIView):
    """An APIView that runs inside the caller's tenant scope.

    Every endpoint that touches tenant data inherits this. One that does not is
    not merely unscoped — it cannot read anything, because the manager raises
    without a bound scope. That is the intended failure: loud, immediate, and
    impossible to mistake for an empty result.
    """

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        # Forces DRF to run its authenticators now. Idempotent — `request.user`
        # is cached, so the `super().initial()` below does not repeat the work.
        self.perform_authentication(request)

        if getattr(request.user, "is_authenticated", False):
            if getattr(settings, "STACOS_RLS_ENABLED", True):
                self._scope_stack.enter_context(transaction.atomic())
            scope = resolve_scope_for_request(request._request)
            self._scope_stack.enter_context(_bound(scope))
            apply_rls_tenants(scope.readable_tenant_ids if scope else frozenset())
            request._request.access_scope = scope  # type: ignore[attr-defined]

        super().initial(request, *args, **kwargs)

    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        # The stack is opened here rather than in `initial` so that it unwinds
        # after the response is built — including DRF's own exception handling,
        # which runs inside `dispatch` and may itself touch scoped data while
        # rendering an error.
        with ExitStack() as stack:
            self._scope_stack = stack
            return super().dispatch(request, *args, **kwargs)
