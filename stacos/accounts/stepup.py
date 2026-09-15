"""
Step-up re-authentication.

Some actions need proof that the person at the keyboard is still the account
holder, not someone who walked up to an unlocked laptop: approving a return,
changing a role, changing a payment method, exporting data, granting or revoking
an engagement.

Freshness is tracked in the session. It is deliberately *not* a permission — a
user may hold ``return.approve`` all day and still be asked to re-authenticate
before each approval window.

**Currently switched off**: ``STEP_UP_ENABLED`` defaults to ``False``, so
:func:`step_up_is_fresh` short-circuits to ``True`` and nothing ever raises
:class:`~stacos.core.exceptions.StepUpRequired`. The decorators, the prompt view
and its tests are all kept working so the switch is the only thing that has to
change to bring it back. See the setting's comment in ``config/settings/base.py``.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from stacos.core.exceptions import StepUpRequired

__all__ = ["mark_step_up_complete", "require_step_up", "step_up_is_fresh"]

SESSION_KEY = "stacos_step_up_at"
F = TypeVar("F", bound=Callable[..., Any])


def step_up_is_fresh(request: HttpRequest, *, max_age_seconds: int | None = None) -> bool:
    """True when the user re-authenticated recently enough.

    Also true — unconditionally — when ``STEP_UP_ENABLED`` is off, which is the
    default. Disabling is done here rather than at each call site on purpose:
    this is the single funnel both :func:`require_step_up` and
    ``permissions._enforce_step_up`` pass through, so one check cannot leave a
    second, forgotten path still prompting.
    """
    if not getattr(settings, "STEP_UP_ENABLED", False):
        return True

    stamp = request.session.get(SESSION_KEY)
    if not stamp:
        return False

    max_age: int = max_age_seconds or int(getattr(settings, "STEP_UP_MAX_AGE_SECONDS", 600))
    try:
        completed_at = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False

    return (timezone.now() - completed_at).total_seconds() < max_age


def mark_step_up_complete(request: HttpRequest) -> None:
    """Record that the user has just re-authenticated."""
    request.session[SESSION_KEY] = timezone.now().isoformat()
    request.session.modified = True


def require_step_up(max_age_seconds: int | None = None) -> Callable[[F], F]:
    """Demand fresh re-authentication before a view runs.

    Sensitive *permissions* trigger this automatically through
    :func:`~stacos.core.permissions.require_permission`. Use this decorator for
    views whose sensitivity is not captured by a permission — a bulk delete, or a
    confirmation screen that itself reveals something.
    """

    def decorator(view: F) -> F:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if not step_up_is_fresh(request, max_age_seconds=max_age_seconds):
                raise StepUpRequired(next_url=request.get_full_path())
            return view(request, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
