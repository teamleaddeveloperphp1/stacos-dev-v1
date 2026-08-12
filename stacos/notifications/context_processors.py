"""
The unread count in the app shell.

Runs on every render, including the marketing site and the sign-in page, so the
guard below is not defensive padding — it is the difference between a working
public site and an ``UnscopedQueryError`` on the home page. The notification
manager raises without a bound scope, deliberately, and a context processor is
exactly the sort of ambient code that would otherwise trip it.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from stacos.core.scope import current_scope

__all__ = ["notification_badge"]

#: Past this the badge says "99+". The exact figure stops being useful long
#: before it stops fitting.
BADGE_CAP = 99


def notification_badge(request: HttpRequest) -> dict[str, Any]:
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return {"notification_unread": 0}
    if current_scope() is None:
        # Signed in but outside a tenant — the tenant picker, an onboarding step.
        # There is nothing to count and the query would raise.
        return {"notification_unread": 0}

    from stacos.notifications.models import Notification

    count = Notification.objects.filter(recipient=user, read_at__isnull=True).count()
    return {
        "notification_unread": count,
        "notification_badge": "99+" if count > BADGE_CAP else str(count or ""),
    }
