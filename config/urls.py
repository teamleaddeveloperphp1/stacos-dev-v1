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
from django.contrib.sitemaps.views import sitemap
from django.templatetags.static import static as static_url
from django.urls import include, path
from django.views.generic.base import RedirectView

from stacos.core.views import healthz
from stacos.marketing.sitemaps import SITEMAPS
from stacos.marketing.views import robots_txt

urlpatterns = [
    # --- Operations ---
    path("healthz", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    # --- Crawler directives, at the roots crawlers actually request ---
    path("robots.txt", robots_txt, name="robots"),
    path("sitemap.xml", sitemap, {"sitemaps": SITEMAPS}, name="sitemap"),
    # Browsers request /favicon.ico regardless of the <link rel="icon"> tag.
    path(
        "favicon.ico",
        RedirectView.as_view(url=static_url("favicon.svg"), permanent=True),
        name="favicon",
    ),
    # --- Authentication ---
    path("auth/", include("stacos.accounts.urls", namespace="accounts")),
    path("accounts/", include("allauth.urls")),
    # --- Web application ---
    # Compliance first: `stacos.tenancy.urls` owns the bare `app/` route, so a
    # more specific prefix has to be registered before it to be reachable.
    path(
        "app/start/",
        include("stacos.tenancy.onboarding.urls", namespace="onboarding"),
    ),
    path("app/compliance/", include("stacos.obligations.urls", namespace="compliance")),
    path("app/notifications/", include("stacos.notifications.urls", namespace="notifications")),
    path("app/practice/", include("stacos.practice.urls", namespace="practice")),
    path("app/billing/", include("stacos.billing.urls", namespace="billing")),
    path("app/channel/", include("stacos.dealers.urls", namespace="dealers")),
    path("app/", include("stacos.tenancy.urls", namespace="app")),
    # --- API (mobile and webhooks only) ---
    path("api/v1/", include("stacos.api.urls", namespace="api")),
    # --- Marketing, last so it never shadows an application route ---
    path("", include("stacos.marketing.urls", namespace="marketing")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += [path("__debug__/", include("debug_toolbar.urls"))]
    # The long-poll endpoint the browser-reload script listens on. Development
    # only, and registered here rather than in `dev.py` because URLs have one
    # module.
    urlpatterns += [path("__reload__/", include("django_browser_reload.urls"))]
