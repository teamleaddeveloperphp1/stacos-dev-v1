"""
Public URLs.

Included last in the root conf, so nothing here can shadow an application route.
Paths are chosen to be stable: these are the URLs that get linked to, indexed and
printed on things, and changing one later costs a redirect table forever.

Slugs are matched with ``<slug:…>`` and resolved against the tuples in
:mod:`stacos.marketing.content`; an unknown slug is a 404 rather than a page with
empty sections.
"""

from django.urls import path

from stacos.marketing import views

app_name = "marketing"

urlpatterns = [
    path("", views.home, name="home"),
    # --- Product ---
    path("product/", views.product, name="product"),
    path("product/<slug:slug>/", views.module, name="module"),
    # --- Solutions ---
    path("for/<slug:slug>/", views.audience, name="audience"),
    path("partners/", views.partners, name="partners"),
    # --- Commercial ---
    path("pricing/", views.pricing, name="pricing"),
    # --- Trust ---
    path("security/", views.security, name="security"),
    path("security/sub-processors/", views.subprocessors, name="subprocessors"),
    # --- Resources ---
    path("guides/", views.resources, name="resources"),
    path("guides/<slug:slug>/", views.guide, name="guide"),
    path("faq/", views.faq, name="faq"),
    path("whats-new/", views.changelog, name="changelog"),
    # --- Company ---
    path("about/", views.about, name="about"),
    path("careers/", views.careers, name="careers"),
    path("contact/", views.contact, name="contact"),
    # --- Legal ---
    path("legal/", views.legal_index, name="legal_index"),
    path("legal/<slug:slug>/", views.legal, name="legal"),
]
