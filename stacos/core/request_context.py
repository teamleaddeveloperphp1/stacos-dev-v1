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
from dataclasses import dataclass
from uuid import UUID

__all__ = ["RequestMeta", "bind_request_meta", "clear_request_meta", "current_request_meta"]


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


@contextlib.contextmanager
def request_meta(meta: RequestMeta) -> Iterator[RequestMeta]:
    """Bind metadata for a block — used by Celery tasks and management commands."""
    token = _current.set(meta)
    try:
        yield meta
    finally:
        _current.reset(token)
