"""
API routes — mobile client and webhooks only.

Deliberately narrow. The web application renders HTML; building a JSON API for
the templates to consume is how a server-rendered product accidentally becomes a
badly-built SPA.
"""

from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from stacos.api import mobile, views

app_name = "api"

urlpatterns = [
    # --- Schema ---
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "schema/swagger-ui/",
        SpectacularSwaggerView.as_view(url_name="api:schema"),
        name="swagger-ui",
    ),
    # --- Authentication ---
    path("auth/login/", views.MobileLoginView.as_view(), name="auth_login"),
    path("auth/verify/", views.MobileVerifyView.as_view(), name="auth_verify"),
    path("auth/refresh/", views.MobileTokenRefreshView.as_view(), name="auth_refresh"),
    path("auth/logout/", views.MobileLogoutView.as_view(), name="auth_logout"),
    # --- Session context ---
    # Which tenant a request acts in comes from the `X-Stacos-Tenant` header,
    # since a JWT request has no session to hold the switcher's choice. The ids
    # to choose from are the ones `/me/` returns.
    path("me/", views.MeView.as_view(), name="me"),
    # --- The calendar, and acting on it ---
    path("calendar/", mobile.CalendarView.as_view(), name="calendar"),
    path("obligations/<uuid:pk>/", mobile.ObligationDetailView.as_view(), name="obligation"),
    path(
        "obligations/<uuid:pk>/transition/",
        mobile.ObligationTransitionView.as_view(),
        name="obligation_transition",
    ),
    # The reason the app exists: a stamped challan photographed at the counter.
    path(
        "obligations/<uuid:pk>/evidence/",
        mobile.ObligationEvidenceView.as_view(),
        name="obligation_evidence",
    ),
    # --- Information requests ---
    path("requests/", mobile.RequestListView.as_view(), name="requests"),
    path("requests/<uuid:pk>/", mobile.RequestDetailView.as_view(), name="request"),
    path(
        "requests/items/<uuid:pk>/respond/",
        mobile.RequestItemRespondView.as_view(),
        name="request_item_respond",
    ),
    # --- Notifications ---
    path("notifications/", mobile.NotificationListView.as_view(), name="notifications"),
    path(
        "notifications/<uuid:pk>/read/",
        mobile.NotificationReadView.as_view(),
        name="notification_read",
    ),
]
