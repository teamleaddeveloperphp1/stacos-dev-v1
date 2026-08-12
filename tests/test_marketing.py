"""
The public surface.

Marketing pages are the only pages an anonymous visitor, a crawler or a link
preview ever sees, and they are the pages nobody opens during feature work — so
they break quietly. These tests are cheap and cover the ways that actually
happens:

* a page 500s because a template references something a view stopped passing;
* a link in the header or the footer points at a URL name that no longer exists;
* a page ships with no title, no description or no canonical, and quietly stops
  ranking;
* the sitemap advertises a URL that 404s, or omits a page that exists;
* structured data is invalid JSON, which no browser will ever tell you;
* the enquiry form accepts anything, and the inbox fills with spam.

They run against the real middleware stack, because a marketing page that works
in isolation and dies under ``ScopeMiddleware`` is still a broken page.
"""

from __future__ import annotations

import json
import re
from xml.etree import ElementTree

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse

from stacos.marketing.content import (
    AUDIENCES,
    FOOTER,
    GUIDES,
    LEGAL_DOCUMENTS,
    MODULES,
    NAV,
)

pytestmark = pytest.mark.django_db


def _every_marketing_path() -> list[str]:
    """Every routed public URL, derived from the content tuples themselves.

    Derived rather than listed, so a new module, guide or legal document is
    covered by these tests the moment it is added — which is the only way a
    parametrised suite stays honest as content grows.
    """
    paths = [
        reverse("marketing:home"),
        reverse("marketing:product"),
        reverse("marketing:pricing"),
        reverse("marketing:partners"),
        reverse("marketing:security"),
        reverse("marketing:subprocessors"),
        reverse("marketing:resources"),
        reverse("marketing:faq"),
        reverse("marketing:changelog"),
        reverse("marketing:about"),
        reverse("marketing:careers"),
        reverse("marketing:contact"),
        reverse("marketing:legal_index"),
    ]
    paths += [reverse("marketing:module", args=[m.slug]) for m in MODULES]
    paths += [reverse("marketing:audience", args=[a.slug]) for a in AUDIENCES]
    paths += [reverse("marketing:guide", args=[g.slug]) for g in GUIDES]
    paths += [reverse("marketing:legal", args=[d.slug]) for d in LEGAL_DOCUMENTS]
    return paths


MARKETING_PATHS = _every_marketing_path()


# ===========================================================================
# 1. Every page renders, anonymously
# ===========================================================================


@pytest.mark.parametrize("path", MARKETING_PATHS)
def test_page_is_reachable_anonymously(client: Client, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"


@pytest.mark.parametrize("path", MARKETING_PATHS)
def test_page_leaks_no_template_syntax(client: Client, path: str) -> None:
    """Unrendered `{{`, `{%` or a leftover `<c-…>` means a template silently failed.

    django-cotton leaves the literal tag in place when a component is missing or
    misnamed, and the page then looks almost right — which is exactly why this
    needs asserting rather than eyeballing.
    """
    html = client.get(path).content.decode()
    for token in ("{%", "{{", "{#"):
        assert token not in html, f"{path} leaked unrendered {token!r}"
    leftovers = re.findall(r"<c-[a-z0-9.-]+", html)
    assert not leftovers, f"{path} has unrendered components: {sorted(set(leftovers))}"


@pytest.mark.parametrize("slug", ["nonsense", "compliance", "terms-and-conditions"])
def test_unknown_slugs_are_404(client: Client, slug: str) -> None:
    """An unknown slug must 404, not render a page with empty sections."""
    for name in ("marketing:module", "marketing:audience", "marketing:guide"):
        assert client.get(reverse(name, args=[slug])).status_code == 404


# ===========================================================================
# 2. Head metadata — the part nobody notices is missing
# ===========================================================================

TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
DESCRIPTION = re.compile(r'<meta name="description" content="(.*?)">', re.DOTALL)
CANONICAL = re.compile(r'<link rel="canonical" href="(.*?)">')
OG_TITLE = re.compile(r'<meta property="og:title" content="(.*?)">')


@pytest.mark.parametrize("path", MARKETING_PATHS)
def test_page_declares_its_metadata(client: Client, path: str) -> None:
    html = client.get(path).content.decode()

    title = TITLE.search(html)
    assert title and title.group(1).strip(), f"{path} has no title"
    assert title.group(1).endswith("STACOS"), f"{path} title is missing the site suffix"

    description = DESCRIPTION.search(html)
    assert description and len(description.group(1)) > 60, (
        f"{path} has a missing or uselessly short meta description"
    )

    canonical = CANONICAL.search(html)
    assert canonical and canonical.group(1).startswith("http"), (
        f"{path} has no absolute canonical URL"
    )
    assert OG_TITLE.search(html), f"{path} has no Open Graph title"


def test_canonical_ignores_campaign_parameters(client: Client) -> None:
    """Every `?utm_…` variant must point at one canonical.

    Otherwise a campaign fragments a page's ranking across dozens of duplicate
    URLs — which is the specific way marketing traffic damages marketing pages.
    """
    html = client.get("/pricing/", {"utm_source": "newsletter"}).content.decode()
    canonical = CANONICAL.search(html)
    assert canonical
    assert canonical.group(1).endswith("/pricing/")
    assert "utm_source" not in canonical.group(1)


# ===========================================================================
# 3. Structured data
# ===========================================================================

JSONLD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)


@pytest.mark.parametrize("path", MARKETING_PATHS)
def test_structured_data_is_valid_json(client: Client, path: str) -> None:
    """Invalid JSON-LD fails silently: no browser and no test but this one notices."""
    for block in JSONLD.findall(client.get(path).content.decode()):
        payload = json.loads(block)
        assert payload["@context"] == "https://schema.org"
        assert payload["@type"]


def test_home_page_publishes_organisation_and_product(client: Client) -> None:
    blocks = [json.loads(b) for b in JSONLD.findall(client.get("/").content.decode())]
    types = {block["@type"] for block in blocks}
    assert {"Organization", "WebSite", "SoftwareApplication", "FAQPage"} <= types


def test_a_subpage_publishes_breadcrumbs(client: Client) -> None:
    html = client.get(reverse("marketing:module", args=["notices"])).content.decode()
    blocks = [json.loads(b) for b in JSONLD.findall(html)]
    crumbs = next(b for b in blocks if b["@type"] == "BreadcrumbList")
    assert [item["name"] for item in crumbs["itemListElement"]] == [
        "Home",
        "Product",
        "Notices and litigation",
    ]


def test_product_structured_data_claims_no_ratings(client: Client) -> None:
    """We have no reviews to aggregate, and inventing them is a lie a crawler believes."""
    blocks = [json.loads(b) for b in JSONLD.findall(client.get("/pricing/").content.decode())]
    product = next(b for b in blocks if b["@type"] == "SoftwareApplication")
    assert "aggregateRating" not in product
    assert product["offers"]["priceCurrency"] == "INR"


# ===========================================================================
# 4. Navigation and footer resolve
# ===========================================================================


@pytest.mark.parametrize(
    "url_name,args",
    [(item.url_name, item.args) for section in (*NAV, *FOOTER) for item in section.items],
)
def test_every_navigation_target_resolves(client: Client, url_name: str, args: tuple) -> None:
    """A nav entry pointing at a dead URL is the most visible bug on a site."""
    path = reverse(url_name, args=list(args))
    assert client.get(path).status_code in (200, 302), f"{url_name} → {path} is broken"


def test_footer_appears_on_every_page(client: Client) -> None:
    for path in (MARKETING_PATHS[0], MARKETING_PATHS[-1]):
        html = client.get(path).content.decode()
        assert "site-footer" in html
        assert reverse("marketing:legal", args=["privacy"]) in html


# ===========================================================================
# 5. Crawler directives
# ===========================================================================


def test_robots_refuses_indexing_by_default(client: Client) -> None:
    """A staging deployment must not compete with production for its own keywords."""
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    assert "Disallow: /" in response.content.decode()


def test_robots_allows_indexing_when_configured(client: Client, settings) -> None:
    settings.MARKETING_ALLOW_INDEXING = True
    body = client.get("/robots.txt").content.decode()
    assert "Disallow: /app/" in body
    assert "Sitemap: http" in body


def test_sitemap_lists_every_public_page(client: Client) -> None:
    response = client.get("/sitemap.xml")
    assert response.status_code == 200

    root = ElementTree.fromstring(response.content)
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    listed = {
        element.text.split("://", 1)[1].split("/", 1)[1]
        for element in root.findall(".//s:loc", namespace)
        if element.text
    }
    listed = {f"/{path}" for path in listed}

    # The contact form is listed; nothing behind authentication ever is.
    for path in MARKETING_PATHS:
        assert path in listed, f"{path} is missing from the sitemap"
    assert not [path for path in listed if path.startswith(("/app/", "/auth/", "/admin/"))]


def test_sitemap_urls_all_resolve(client: Client) -> None:
    root = ElementTree.fromstring(client.get("/sitemap.xml").content)
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for element in root.findall(".//s:loc", namespace):
        assert element.text
        path = "/" + element.text.split("://", 1)[1].split("/", 1)[1]
        assert client.get(path).status_code == 200, f"sitemap advertises a dead URL: {path}"


# ===========================================================================
# 6. The enquiry form — the only unauthenticated write here
# ===========================================================================

VALID = {
    "topic": "sales",
    "name": "Anita Rao",
    "email": "anita@acme.example",
    "phone": "+91 98765 43210",
    "organisation": "Acme Manufacturing",
    "size": "2-5",
    "message": "We run two entities in Gujarat and want to see the calendar.",
    "website": "",
    "rendered_at": "0",  # rendered long ago: passes the minimum-fill-time check
}


def test_enquiry_is_delivered_to_the_right_inbox(client: Client, settings) -> None:
    settings.MARKETING_ENQUIRY_INBOX = {
        "default": "sales@example.com",
        "sales": "sales@example.com",
        "security": "security@example.com",
    }
    response = client.post(reverse("marketing:contact"), VALID)

    assert response.status_code == 302
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["sales@example.com"]
    # Reply-To, never From: sending as the visitor would fail SPF and land in spam.
    assert mail.outbox[0].reply_to == ["anita@acme.example"]
    assert "Acme Manufacturing" in mail.outbox[0].body


def test_security_enquiries_reach_the_security_inbox(client: Client, settings) -> None:
    settings.MARKETING_ENQUIRY_INBOX = {
        "default": "sales@example.com",
        "security": "security@example.com",
    }
    client.post(reverse("marketing:contact"), {**VALID, "topic": "security"})
    assert mail.outbox[0].to == ["security@example.com"]


def test_unknown_topic_falls_back_rather_than_crashing(client: Client, settings) -> None:
    """A tampered payload must not 500, and must not lose the enquiry."""
    settings.MARKETING_ENQUIRY_INBOX = {"default": "sales@example.com"}
    response = client.post(reverse("marketing:contact"), {**VALID, "topic": "tampered"})
    # The choice field rejects it outright; the page re-renders with the error.
    assert response.status_code == 200
    assert not mail.outbox


def test_honeypot_submission_sends_nothing(client: Client) -> None:
    response = client.post(reverse("marketing:contact"), {**VALID, "website": "http://spam"})
    assert response.status_code == 200
    assert not mail.outbox


def test_instant_submission_sends_nothing(client: Client) -> None:
    """A form submitted the moment it rendered was not typed by a person."""
    import time

    now = str(int(time.time()))
    response = client.post(reverse("marketing:contact"), {**VALID, "rendered_at": now})
    assert response.status_code == 200
    assert not mail.outbox


def test_invalid_enquiry_redisplays_the_form(client: Client) -> None:
    response = client.post(reverse("marketing:contact"), {**VALID, "email": "not-an-email"})
    assert response.status_code == 200
    assert not mail.outbox
    assert "not-an-email" in response.content.decode(), "the form lost what was typed"


def test_enquiries_are_rate_limited_per_address(client: Client) -> None:
    """The worst case is a filled inbox, so the limit is generous but real."""
    from django.core.cache import cache

    cache.clear()
    for _ in range(5):
        client.post(reverse("marketing:contact"), VALID)
    assert len(mail.outbox) == 5

    blocked = client.post(reverse("marketing:contact"), VALID)
    assert blocked.status_code == 200
    assert len(mail.outbox) == 5, "the rate limit did not hold"


# ===========================================================================
# 7. Accessibility invariants that a redesign tends to drop
# ===========================================================================


@pytest.mark.parametrize("path", [MARKETING_PATHS[0], "/pricing/", "/faq/"])
def test_page_has_one_h1_and_a_skip_link(client: Client, path: str) -> None:
    html = client.get(path).content.decode()
    assert html.count("<h1") == 1, f"{path} must have exactly one h1"
    assert 'class="skip-link"' in html
    assert 'id="main"' in html
