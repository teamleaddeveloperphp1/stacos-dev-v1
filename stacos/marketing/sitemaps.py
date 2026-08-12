"""
The sitemap.

Enumerated from the same tuples that build the navigation, so a page cannot
exist in the header and be missing from the sitemap — which is the usual way a
new page goes uncrawled for a month.

Only the public surface appears. ``/app/`` is authenticated and every response
there is behind a sign-in redirect; listing it would advertise URLs that return
nothing useful to a crawler.

``lastmod`` is real where we have it (guides and legal documents carry dates) and
omitted where we do not. A fabricated ``lastmod`` on every page is worse than
none: crawlers learn to ignore the field.
"""

from __future__ import annotations

from datetime import date

from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from stacos.marketing.content import AUDIENCES, GUIDES, LEGAL_DOCUMENTS, MODULES

__all__ = ["SITEMAPS"]

#: https everywhere except a local development server, which has no certificate.
PROTOCOL = "http" if settings.DEBUG else "https"


class StaticSitemap(Sitemap[tuple[str, float]]):
    """Pages with no slug of their own.

    Priority is a hint about relative importance *within this site*, so only the
    ordering carries meaning: the home page and the two commercial pages outrank
    the company pages.
    """

    protocol = PROTOCOL
    changefreq = "monthly"

    PAGES: tuple[tuple[str, float], ...] = (
        ("marketing:home", 1.0),
        ("marketing:pricing", 0.9),
        ("marketing:product", 0.9),
        ("marketing:security", 0.8),
        ("marketing:faq", 0.7),
        ("marketing:resources", 0.7),
        ("marketing:partners", 0.6),
        ("marketing:about", 0.6),
        ("marketing:contact", 0.6),
        ("marketing:careers", 0.5),
        ("marketing:changelog", 0.5),
        ("marketing:subprocessors", 0.4),
        ("marketing:legal_index", 0.3),
    )

    def items(self) -> list[tuple[str, float]]:
        return list(self.PAGES)

    def location(self, item: tuple[str, float]) -> str:
        return reverse(item[0])

    def priority(self, item: tuple[str, float]) -> float:
        return item[1]


class ModuleSitemap(Sitemap[str]):
    protocol = PROTOCOL
    changefreq = "monthly"
    priority = 0.8

    def items(self) -> list[str]:
        return [module.slug for module in MODULES]

    def location(self, item: str) -> str:
        return reverse("marketing:module", args=[item])


class AudienceSitemap(Sitemap[str]):
    protocol = PROTOCOL
    changefreq = "monthly"
    priority = 0.8

    def items(self) -> list[str]:
        return [audience.slug for audience in AUDIENCES]

    def location(self, item: str) -> str:
        return reverse("marketing:audience", args=[item])


class GuideSitemap(Sitemap[tuple[str, date]]):
    protocol = PROTOCOL
    changefreq = "yearly"
    priority = 0.6

    def items(self) -> list[tuple[str, date]]:
        return [(guide.slug, guide.updated) for guide in GUIDES]

    def location(self, item: tuple[str, date]) -> str:
        return reverse("marketing:guide", args=[item[0]])

    def lastmod(self, item: tuple[str, date]) -> date:
        return item[1]


class LegalSitemap(Sitemap[tuple[str, date]]):
    protocol = PROTOCOL
    changefreq = "yearly"
    priority = 0.3

    def items(self) -> list[tuple[str, date]]:
        return [(document.slug, document.effective) for document in LEGAL_DOCUMENTS]

    def location(self, item: tuple[str, date]) -> str:
        return reverse("marketing:legal", args=[item[0]])

    def lastmod(self, item: tuple[str, date]) -> date:
        return item[1]


SITEMAPS = {
    "pages": StaticSitemap,
    "product": ModuleSitemap,
    "solutions": AudienceSitemap,
    "guides": GuideSitemap,
    "legal": LegalSitemap,
}
