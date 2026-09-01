"""
The notification screens: a list, a panel that drops out of the bell, and the
preferences form.

Everything here is scoped to the signed-in user by construction — the queryset
filters on ``recipient=request.user`` before anything else — so there is no
object-level check to forget. That is the right shape for this module: a
notification addressed to somebody else is not a permission question, it is a
row that should never appear in the query at all.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from stacos.core.htmx import Fragment, Toast, is_fragment_request, oob
from stacos.core.pagination import filters_querystring, keyset_page
from stacos.core.permissions import require_permission
from stacos.core.typing import current_user
from stacos.notifications.forms import PreferenceForm
from stacos.notifications.models import Notification, NotificationKind
from stacos.notifications.services import mark_all_read, preferences_for, unread_count

#: The bell shows a count, not a number of hundreds. Past this it says "99+",
#: which is both honest and the point at which the exact figure stops helping.
BADGE_CAP = 99


def _mine(request: HttpRequest) -> QuerySet[Notification]:
    return (
        Notification.objects.filter(recipient=current_user(request))
        .select_related("entity")
        .order_by("-created_at")
    )


#: One screen's worth. The list used to stop dead at a hundred rows with no way
#: to reach the rest — see stacos.core.pagination.
PAGE_SIZE = 50


@require_permission("notifications.view")
def notification_list(request: HttpRequest) -> HttpResponse:
    """Everything this user has been told, newest first."""
    queryset = _mine(request)

    unread_only = request.GET.get("filter") == "unread"
    if unread_only:
        queryset = queryset.filter(read_at__isnull=True)

    kind = request.GET.get("kind", "")
    if kind in NotificationKind.values:
        queryset = queryset.filter(kind=kind)

    page = keyset_page(
        queryset,
        order_by="created_at",
        cursor=request.GET.get("cursor", ""),
        page_size=PAGE_SIZE,
        descending=True,
    )
    context = {
        "notifications": page.rows,
        "page": page,
        "querystring": filters_querystring(request),
        "unread": unread_count(user=current_user(request)),
        "unread_only": unread_only,
        "kind": kind,
        "kinds": NotificationKind.choices,
    }

    # A cursor request is asking for more rows, not for the whole screen again.
    if request.GET.get("cursor") and is_fragment_request(request):
        return render(request, "notifications/_fragments/notification_rows.html", context)

    template = (
        "notifications/_fragments/list_body.html"
        if is_fragment_request(request)
        else "notifications/list.html"
    )
    return render(request, template, context)


@require_permission("notifications.view")
def panel(request: HttpRequest) -> HttpResponse:
    """The dropdown behind the bell: the ten most recent, unread first.

    Loaded on demand rather than rendered into every page. The count in the shell
    is one cheap aggregate; the panel is ten rows with entity joins, and paying
    for that on every request to render something most people never open is the
    definition of a slow app.
    """
    rows = list(_mine(request).order_by("read_at", "-created_at")[:10])
    return render(
        request,
        "notifications/_fragments/panel.html",
        {"notifications": rows, "unread": unread_count(user=current_user(request))},
    )


@require_permission("notifications.view")
@require_http_methods(["POST"])
def mark_read(request: HttpRequest, pk: str) -> HttpResponse:
    notification = _mine(request).filter(pk=pk).first()
    if notification is None:
        # Somebody else's notification is not a 403 — it is a row this user has
        # no way to know exists.
        raise Http404
    notification.mark_read()

    return oob(
        request,
        Fragment(
            "notifications/_fragments/row.html",
            {"notification": notification},
        ),
        also=[
            Fragment(
                "notifications/_fragments/badge.html",
                {"unread": unread_count(user=current_user(request))},
                oob_target="notification-badge",
            )
        ],
    )


@require_permission("notifications.view")
@require_http_methods(["POST"])
def mark_all(request: HttpRequest) -> HttpResponse:
    count = mark_all_read(user=current_user(request))
    return oob(
        request,
        Fragment("notifications/_fragments/badge.html", {"unread": 0}),
        toast=Toast(_("%(count)d notification(s) marked as read.") % {"count": count}),
        triggers={"stacos:notifications-changed": True},
    )


@require_permission("notifications.preferences.manage")
@require_http_methods(["GET", "POST"])
def preferences(request: HttpRequest) -> HttpResponse:
    """How this person wants to be told, in this tenant."""
    tenant = getattr(request, "tenant", None)
    if tenant is None:  # pragma: no cover - the scope middleware guarantees one
        raise Http404

    preference = preferences_for(tenant_id=tenant.pk, user=current_user(request))
    form = PreferenceForm(request.POST or None, instance=preference)

    if request.method == "POST" and form.is_valid():
        form.save()
        return oob(
            request,
            Fragment(
                "notifications/_fragments/preferences_form.html",
                {"form": PreferenceForm(instance=preference), "saved": True},
            ),
            toast=Toast(_("Saved.")),
        )

    template = (
        "notifications/_fragments/preferences_form.html"
        if is_fragment_request(request)
        else "notifications/preferences.html"
    )
    return render(
        request,
        template,
        {"form": form},
        status=422 if request.method == "POST" else 200,
    )


def badge_context(request: HttpRequest) -> dict[str, Any]:
    """The unread count for the app shell.

    A plain count query on a partial index. Cheap enough to run per request, and
    the alternative — a denormalised counter — has to be kept correct across
    every path that reads or raises a notification.
    """
    user = current_user(request)
    if user is None or not getattr(user, "is_authenticated", False):
        return {"notification_unread": 0}

    count = unread_count(user=user)
    return {
        "notification_unread": count,
        "notification_badge": "99+" if count > BADGE_CAP else str(count or ""),
    }
