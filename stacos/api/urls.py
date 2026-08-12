"""
API routes — mobile client and webhooks only.

Deliberately narrow. The web application renders HTML; building a JSON API for
the templates to consume is how a server-rendered product accidentally becomes a
badly-built SPA.
"""

from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from stacos.api import views

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
    path("me/", views.MeView.as_view(), name="me"),
]
