"""
Ambient request metadata.

Service functions and Celery tasks need the request id, IP and acting-as user for
audit rows, but threading a ``request`` argument through every service signature
just to record who did something is how a codebase ends up with HTTP concepts in
its domain layer. So the metadata lives in a contextvar, bound by middleware.

Bound with ``contextvars``, and — critically — **cleared at the start of every
request**. Binding without clearing leaks one request's ``tenant_id`` into the
next on a reused worker thread, which in a compliance product means audit entries
attributed to the wrong customer.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from uuid import UUID

from django.http import HttpRequest

__all__ = [
    "RequestMeta",
    "bind_request_meta",
    "bind_user_label",
    "clear_request_meta",
    "client_ip",
    "current_request_meta",
]


def client_ip(request: HttpRequest) -> str | None:
    """Best-effort client IP address.

    ``X-Forwarded-For`` is trusted only because the deployment terminates TLS at
    a known proxy; the left-most entry is the original client. Lives here rather
    than in the middleware because rate limiting and audit both need it, and
    neither should import a middleware module to get it.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    remote: str | None = request.META.get("REMOTE_ADDR")
    return remote


@dataclass(frozen=True, slots=True)
class RequestMeta:
    request_id: str = ""
    ip_address: str | None = None
    user_agent: str = ""
    user_id: UUID | None = None
    user_label: str = ""
    #: Set when a platform user is acting on a tenant user's behalf.
    impersonator_id: UUID | None = None
    path: str = ""


_EMPTY = RequestMeta()
_current: ContextVar[RequestMeta] = ContextVar("stacos_request_meta", default=_EMPTY)


def current_request_meta() -> RequestMeta:
    return _current.get()


def clear_request_meta() -> None:
    """Reset to empty. Called at the start of every request, before binding."""
    _current.set(_EMPTY)


def bind_request_meta(meta: RequestMeta) -> Token[RequestMeta]:
    return _current.set(meta)


def bind_user_label(label: str) -> None:
    """Name the actor for the audit trail when there is no ``User`` row.

    An outside contact answering through a responder link is a real actor with
    real consequences — they supplied the bank statement the filing rests on —
    and ``record_event`` would otherwise write that row with no actor at all.
    ``AuditLog.actor`` stays null because there is no user; ``actor_label`` says
    who it was.
    """
    current = _current.get()
    _current.set(replace(current, user_label=label[:255]))


@contextlib.contextmanager
def request_meta(meta: RequestMeta) -> Iterator[RequestMeta]:
    """Bind metadata for a block — used by Celery tasks and management commands."""
    token = _current.set(meta)
    try:
        yield meta
    finally:
        _current.reset(token)
