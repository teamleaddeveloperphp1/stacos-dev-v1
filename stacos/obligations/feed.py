"""
The read-only ``.ics`` calendar subscription feed.

An external calendar app (Google Calendar, Outlook, Apple Calendar) polls the
feed URL directly, with no Django session and no cookies — so it cannot go
through the ordinary session-authenticated, ``AccessScope``-bound-by-middleware
path every other view in this app relies on. A :class:`CalendarFeedToken`
stands in for that session: resolving it yields a user, and this module
re-derives that user's own access scope fresh on every fetch
(``resolve_scope_for_membership``), the same way a real request would, rather
than baking a tenant or an entity list into the link the day it was generated.

Never a second due-date calculation: the feed is built from
:func:`stacos.obligations.queries.upcoming`, the same function the tenancy
dashboard's "coming up" card already uses.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import date, datetime, timedelta
from hashlib import sha256
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from stacos.accounts.models import User
from stacos.core.rls import rls_bootstrap
from stacos.core.scope import tenant_context
from stacos.obligations.models import CalendarFeedToken, ObligationInstance
from stacos.obligations.queries import upcoming
from stacos.tenancy.models import Membership
from stacos.tenancy.scope_resolver import resolve_scope_for_membership

__all__ = [
    "FEED_WINDOW_DAYS",
    "create_feed_token",
    "feed_events_for_user",
    "render_ics",
    "resolve_feed_token",
    "revoke_feed_token",
]

#: How far ahead the feed looks. Wide enough that a quarterly filer's calendar
#: is never empty between refreshes, narrow enough that a client syncing daily
#: is not carrying a year of speculative entries.
FEED_WINDOW_DAYS = 180


def _hash_secret(secret: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), secret.encode(), sha256).hexdigest()


@transaction.atomic
def create_feed_token(user: User, *, label: str = "") -> tuple[CalendarFeedToken, str]:
    """Issue a new subscription link, revoking whatever the user had before.

    One live token per user: a second "Subscribe" click is how someone who
    lost their old link gets a working one back, not a way to accumulate an
    unbounded list of links nobody remembers issuing.
    """
    CalendarFeedToken.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
    secret = secrets.token_urlsafe(32)
    token = CalendarFeedToken.objects.create(
        user=user, secret_hash=_hash_secret(secret), label=label
    )
    return token, f"{token.id}.{secret}"


def resolve_feed_token(raw: str) -> CalendarFeedToken | None:
    """The live token this link names, or ``None``.

    Constant-time comparison against the stored hash — the same reason
    ``stacos.accounts.devices.recognised_device`` compares this way — so a
    database disclosure or a timing side-channel cannot be used to forge a
    working link.
    """
    if not raw or "." not in raw:
        return None
    token_id, _, secret = raw.partition(".")
    try:
        UUID(token_id)
    except (ValueError, TypeError, AttributeError):
        # A hand-edited or garbage link, not a UUID at all — filtering a
        # UUIDField with one raises `ValidationError` deep inside the ORM
        # rather than matching nothing, the same trap `_parse_uuid` in
        # `stacos.obligations.views` guards against for the entity filter.
        return None
    try:
        token = CalendarFeedToken.objects.select_related("user").get(pk=token_id)
    except CalendarFeedToken.DoesNotExist:
        return None
    if not token.is_valid:
        return None
    if not hmac.compare_digest(token.secret_hash, _hash_secret(secret)):
        return None
    return token


def revoke_feed_token(token: CalendarFeedToken) -> None:
    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])


def feed_events_for_user(user: User, *, as_of: date) -> list[ObligationInstance]:
    """Every open obligation the token's owner can currently see, due soon.

    Resolved fresh on every fetch rather than cached on the token: a
    membership change, a new engagement or a role change is reflected the next
    time the calendar app polls, with nothing to reissue.
    """
    with transaction.atomic(), rls_bootstrap():
        membership = (
            Membership.objects_unscoped.filter(user=user, status=Membership.Status.ACTIVE)
            .select_related("tenant", "role")
            .order_by("created_at")
            .first()
        )
    if membership is None:
        return []

    scope = resolve_scope_for_membership(membership, reason="feed:ics")
    with tenant_context(
        tenant_ids=scope.readable_tenant_ids,
        principal_tenant_id=scope.principal_tenant_id,
        writable_tenant_ids=scope.writable_tenant_ids,
        entity_ids=scope.entity_ids,
        categories=scope.categories,
        permissions=scope.permissions,
        reason="feed:ics",
    ):
        entity_ids = list(scope.entity_ids) if scope.entity_ids is not None else None
        return list(
            upcoming(as_of=as_of, within_days=FEED_WINDOW_DAYS, entity_ids=entity_ids)
            .select_related("entity")
        )


def render_ics(obligations: list[ObligationInstance], *, calendar_name: str) -> bytes:
    """A minimal, correct VCALENDAR — one all-day VEVENT per obligation.

    Hand-rolled rather than a new dependency: the shape needed here (a handful
    of all-day events, no recurrence, no timezone conversion) is a small,
    stable slice of RFC 5545, and every field goes through ``_escape``/
    ``_fold`` so a comma or a long title in a compliance definition can never
    produce a feed a calendar app rejects.
    """
    now = timezone.now()
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//STACOS//Compliance Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
    ]
    for obligation in obligations:
        if obligation.due_date is None:
            continue
        lines.extend(_vevent(obligation, now=now))
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines).encode("utf-8") + b"\r\n"


def _vevent(obligation: ObligationInstance, *, now: datetime) -> list[str]:
    due = obligation.due_date
    assert due is not None  # narrowed by the caller
    summary = obligation.title
    if obligation.entity_id:
        summary = f"{summary} — {obligation.entity.name}"
    description_parts = [
        part
        for part in (obligation.scope_label, obligation.period_label or obligation.period_key)
        if part
    ]
    lines = [
        "BEGIN:VEVENT",
        f"UID:obligation-{obligation.pk}@stacos",
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART;VALUE=DATE:{_ymd(due)}",
        # RFC 5545 all-day events end on the day *after* the last day covered —
        # a single-day event's DTEND is DTSTART plus one, not DTSTART itself.
        f"DTEND;VALUE=DATE:{_ymd(due + timedelta(days=1))}",
        f"SUMMARY:{_escape(summary)}",
    ]
    if description_parts:
        lines.append(f"DESCRIPTION:{_escape(' · '.join(description_parts))}")
    lines.append("TRANSP:TRANSPARENT")
    lines.append("END:VEVENT")
    return lines


def _ymd(value: date) -> str:
    return value.strftime("%Y%m%d")


def _stamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 §3.1: fold a content line over 75 octets onto continuation
    lines starting with a single space, without splitting inside a multi-byte
    UTF-8 sequence."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts: list[str] = []
    start = 0
    limit = 75
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        while end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(encoded[start:end].decode("utf-8"))
        start = end
        limit = 74  # continuation lines carry a leading space
    return "\r\n ".join(parts)
