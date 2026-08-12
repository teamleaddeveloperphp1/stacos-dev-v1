"""
Search and social metadata for the public surface.

The marketing pages are the only pages a crawler, a link preview or an AI
retriever ever sees — everything behind ``/app/`` is authenticated and
``noindex``. So the metadata is not decoration here; it is the entire machine
readable description of the product.

Two decisions worth stating:

* **Every page constructs a** :class:`PageMeta`. A view that forgets one gets
  the site defaults rather than an empty ``<title>``, and a test asserts that
  every routed marketing page produces a title, a description and a canonical.
* **Structured data is built in Python, not hand-written in templates.** JSON-LD
  typed by hand in a template is how a site ends up serving invalid JSON to
  crawlers and never noticing; here it is a dict, serialised once, escaped for
  ``<script>`` embedding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.urls import reverse
from django.utils.safestring import SafeString, mark_safe

from stacos.marketing.content import COMPANY, Faq

__all__ = [
    "Breadcrumb",
    "PageMeta",
    "absolute",
    "article_jsonld",
    "breadcrumb_jsonld",
    "faq_jsonld",
    "jsonld",
    "organisation_jsonld",
    "product_jsonld",
]


@dataclass(frozen=True, slots=True)
class Breadcrumb:
    label: str
    url: str = ""


@dataclass(frozen=True, slots=True)
class PageMeta:
    """Everything the ``<head>`` and the breadcrumb trail need.

    :param title: page title *without* the site suffix — the layout adds it.
    :param description: 150–160 characters, written as a sentence a person would
        read, because that is what appears under the result.
    :param robots: overridden only for pages that should not be indexed.
    :param structured_data: a list of JSON-LD dicts, emitted as one script each.
    """

    title: str
    description: str
    breadcrumbs: tuple[Breadcrumb, ...] = ()
    og_type: str = "website"
    robots: str = "index,follow,max-image-preview:large"
    structured_data: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    published: str = ""
    modified: str = ""


def absolute(path: str) -> str:
    """Turn a root-relative path into an absolute URL.

    Built from ``settings.BASE_URL`` rather than from the request, so a canonical
    link is stable regardless of which host name a proxy used to reach us —
    which is the whole point of a canonical link.
    """
    if path.startswith(("http://", "https://")):
        return path
    return f"{settings.BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def canonical_for(request: HttpRequest) -> str:
    """The canonical URL for the current page, query string discarded.

    Marketing URLs carry no meaningful query parameters, so every ``?utm_…``
    variant of a page must point at the same canonical or campaign traffic
    fragments the page's ranking across dozens of duplicates.
    """
    return absolute(request.path)


def jsonld(payload: dict[str, Any]) -> SafeString:
    """Serialise one JSON-LD block for embedding in a ``<script>``.

    ``<`` is escaped because a string in the payload containing ``</script>``
    would otherwise close the tag and turn structured data into an injection
    point.
    """
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return mark_safe(encoded.replace("<", "\\u003C"))  # noqa: S308


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def organisation_jsonld() -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "Organization",
        "@id": absolute("/#organisation"),
        "name": COMPANY.product_name,
        "legalName": COMPANY.legal_name,
        "url": absolute("/"),
        "logo": absolute("/static/favicon.svg"),
        "foundingDate": str(COMPANY.founded),
        "email": COMPANY.support_email,
        "address": {
            "@type": "PostalAddress",
            "streetAddress": COMPANY.address_lines[1],
            "addressLocality": "Mumbai",
            "postalCode": "400069",
            "addressRegion": "Maharashtra",
            "addressCountry": "IN",
        },
        "sameAs": [COMPANY.linkedin_url, COMPANY.x_url, COMPANY.youtube_url],
        "contactPoint": [
            {
                "@type": "ContactPoint",
                "contactType": "sales",
                "email": COMPANY.sales_email,
                "areaServed": "IN",
                "availableLanguage": ["en", "hi"],
            },
            {
                "@type": "ContactPoint",
                "contactType": "customer support",
                "email": COMPANY.support_email,
                "areaServed": "IN",
            },
        ],
    }


def website_jsonld() -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "@id": absolute("/#website"),
        "url": absolute("/"),
        "name": COMPANY.product_name,
        "publisher": {"@id": absolute("/#organisation")},
        "inLanguage": "en-IN",
    }


def product_jsonld(*, low_price: str, high_price: str) -> dict[str, Any]:
    """SoftwareApplication with a price range rather than invented ratings.

    Deliberately carries no ``aggregateRating``: we have no reviews to
    aggregate, and inventing them is both a policy violation and a lie.
    """
    return {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": COMPANY.product_name,
        "applicationCategory": "BusinessApplication",
        "applicationSubCategory": "Regulatory compliance management",
        "operatingSystem": "Web, Android, iOS",
        "url": absolute("/"),
        "publisher": {"@id": absolute("/#organisation")},
        "offers": {
            "@type": "AggregateOffer",
            "priceCurrency": "INR",
            "lowPrice": low_price,
            "highPrice": high_price,
            "offerCount": 4,
            "url": absolute(reverse("marketing:pricing")),
        },
    }


def breadcrumb_jsonld(crumbs: tuple[Breadcrumb, ...]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": index,
                "name": crumb.label,
                **({"item": absolute(crumb.url)} if crumb.url else {}),
            }
            for index, crumb in enumerate(crumbs, start=1)
        ],
    }


def faq_jsonld(faqs: tuple[Faq, ...]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": faq.question,
                "acceptedAnswer": {"@type": "Answer", "text": faq.answer},
            }
            for faq in faqs
        ],
    }


def article_jsonld(
    *,
    headline: str,
    description: str,
    url: str,
    published: str,
    modified: str,
) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": headline,
        "description": description,
        "url": absolute(url),
        "datePublished": published,
        "dateModified": modified,
        "author": {"@id": absolute("/#organisation")},
        "publisher": {"@id": absolute("/#organisation")},
        "inLanguage": "en-IN",
    }
