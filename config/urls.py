"""
Root URL configuration.

Three surfaces, one Django process, distinguished by URL namespace and layout
rather than by deployment:

===============  ==============================  ==================================
Surface          Prefix                          Notes
===============  ==============================  ==================================
Marketing        ``/``                           Public, cached, no authentication
Web application  ``/app/``                       Authenticated, tenant-scoped
API              ``/api/v1/``                    Mobile client and webhooks only
===============  ==============================  ==================================

Splitting marketing onto its own domain later is a routing change; no application
code depends on the arrangement.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from stacos.core.views import healthz

urlpatterns = [
    # --- Operations ---
    path("healthz", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    # --- Authentication ---
    path("auth/", include("stacos.accounts.urls", namespace="accounts")),
    path("accounts/", include("allauth.urls")),
    # --- Web application ---
    path("app/", include("stacos.tenancy.urls", namespace="app")),
    # --- API (mobile and webhooks only) ---
    path("api/v1/", include("stacos.api.urls", namespace="api")),
    # --- Marketing, last so it never shadows an application route ---
    path("", include("stacos.marketing.urls", namespace="marketing")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
