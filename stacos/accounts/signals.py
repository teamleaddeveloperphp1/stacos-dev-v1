"""Auth signal handlers: audit trail and session bookkeeping."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver
from django.http import HttpRequest
from django.utils import timezone

from stacos.accounts.middleware import SESSION_STAMP_KEY
from stacos.accounts.models import User, UserSession
from stacos.core.audit import record_event
from stacos.core.models import AuditAction


@receiver(user_logged_in)
def on_login(sender: Any, request: HttpRequest, user: User, **kwargs: Any) -> None:
    request.session[SESSION_STAMP_KEY] = str(user.security_stamp)

    if request.session.session_key:
        UserSession.objects.update_or_create(
            user=user,
            session_key=request.session.session_key,
            defaults={
                "ip_address": request.META.get("REMOTE_ADDR"),
                "user_agent": request.headers.get("User-Agent", "")[:512],
                "last_seen_at": timezone.now(),
                "ended_at": None,
            },
        )

    User.objects.filter(pk=user.pk).update(last_seen_at=timezone.now())
    record_event(action=AuditAction.LOGIN, actor=user, obj=user)


@receiver(user_logged_out)
def on_logout(sender: Any, request: HttpRequest, user: User | None, **kwargs: Any) -> None:
    if user is None:
        return
    if request is not None and request.session.session_key:
        UserSession.objects.filter(
            user=user, session_key=request.session.session_key, ended_at__isnull=True
        ).update(ended_at=timezone.now(), ended_reason="signed out")
    record_event(action=AuditAction.LOGOUT, actor=user, obj=user)


@receiver(user_login_failed)
def on_login_failed(sender: Any, credentials: dict[str, Any], **kwargs: Any) -> None:
    """Record failed sign-ins without echoing the attempted password anywhere."""
    record_event(
        action=AuditAction.LOGIN_FAILED,
        object_type="accounts.User",
        object_label=str(credentials.get("username") or credentials.get("email") or "")[:255],
        context={"reason": "invalid credentials"},
    )
