"""
Public marketing surface.

Same Django process as the application, different layout and URL namespace.
Anonymous, aggressively cached, and never touching tenant data — which is why
``/`` is excluded from scope resolution in ``ScopeMiddleware``.

Every view here is thin on purpose. The copy lives in
:mod:`stacos.marketing.content` and the head metadata is a
:class:`~stacos.marketing.seo.PageMeta`; a view's job is to pick the right value
and render it. That keeps a marketing change to a diff in one data file, and
keeps the pages structurally identical to one another — which is what lets a
single test assert the metadata contract across all of them.

Two ordering rules that are load-bearing:

* ``@public_view`` sits **above** ``@cache_page``. ``cache_page`` returns a
  wrapper that does not carry the attributes of the view it wraps, so the other
  order would make every page here look undeclared to
  ``manage.py check_view_permissions``.
* Only views that are a pure function of their URL are cached. The enquiry form
  is not, so it is not.
"""

from __future__ import annotations

from typing import Any

import structlog
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_http_methods

from stacos.core.permissions import public_view
from stacos.core.request_context import current_request_meta
from stacos.marketing import content, seo
from stacos.marketing.content import (
    ADD_ONS,
    AUDIENCES,
    CHANGELOG,
    COMPARISON_ROWS,
    FAQ_SECTIONS,
    GUIDES,
    LEGAL_DOCUMENTS,
    MODULES,
    OPEN_ROLES,
    PARTNER_TRACKS,
    PLANS,
    PRICING_FAQS,
    SUBPROCESSORS,
    Faq,
)
from stacos.marketing.forms import ContactForm
from stacos.marketing.seo import Breadcrumb, PageMeta

logger = structlog.get_logger(__name__)

#: Marketing HTML is a pure function of its URL, so it can be cached hard. Kept
#: modest anyway: a pricing correction should not take an hour to appear.
CACHE_SECONDS = 60 * 15

#: Enquiries per IP address per hour. High enough that a person who mistypes an
#: address and resubmits is unaffected; low enough that the inbox cannot be filled.
CONTACT_LIMIT = 5
CONTACT_WINDOW_SECONDS = 60 * 60

HOME_CRUMB = Breadcrumb(label="Home", url="/")

#: The five questions asked most often, answered on the home page itself. Drawn
#: from the FAQ page rather than written twice, so the two can never disagree.
HOME_FAQS: tuple[Faq, ...] = (
    FAQ_SECTIONS[0].faqs[0],
    FAQ_SECTIONS[0].faqs[1],
    FAQ_SECTIONS[1].faqs[0],
    FAQ_SECTIONS[2].faqs[0],
    PRICING_FAQS[0],
)


# ---------------------------------------------------------------------------
# Rendering helper
# ---------------------------------------------------------------------------


def _page(
    request: HttpRequest,
    template: str,
    meta: PageMeta,
    context: dict[str, Any] | None = None,
) -> HttpResponse:
    """Render a marketing page with its metadata attached.

    Structured data is finished here rather than in each view: every page that
    declares breadcrumbs gets a ``BreadcrumbList`` automatically, so no view can
    forget one and no template hand-writes JSON.
    """
    blocks = list(meta.structured_data)
    if meta.breadcrumbs:
        blocks.append(seo.breadcrumb_jsonld(meta.breadcrumbs))

    return render(
        request,
        template,
        {
            **(context or {}),
            "meta": meta,
            "jsonld_blocks": [seo.jsonld(block) for block in blocks],
            "canonical_url": seo.canonical_for(request),
            # Absolute, because a link preview is fetched by a third party that
            # has no base URL to resolve a relative path against.
            "og_image_url": seo.absolute(static("og-card.svg")),
        },
    )


# ===========================================================================
# Home
# ===========================================================================


@public_view
@cache_page(CACHE_SECONDS)
def home(request: HttpRequest) -> HttpResponse:
    meta = PageMeta(
        title="Compliance software for Indian businesses and their CAs",
        description=(
            "STACOS works out every statutory obligation your business has, generates "
            "the calendar with correct due dates, and lets your CA or CS work in the "
            "same place under scoped, revocable access."
        ),
        structured_data=(
            seo.organisation_jsonld(),
            seo.website_jsonld(),
            seo.product_jsonld(low_price="999", high_price="7999"),
            seo.faq_jsonld(HOME_FAQS),
        ),
    )
    return _page(
        request,
        "marketing/home.html",
        meta,
        {
            "plans": PLANS,
            "modules": MODULES,
            "audiences": AUDIENCES,
            "faqs": HOME_FAQS,
            "guides": GUIDES[:3],
        },
    )


# ===========================================================================
# Product
# ===========================================================================


@public_view
@cache_page(CACHE_SECONDS)
def product(request: HttpRequest) -> HttpResponse:
    meta = PageMeta(
        title="The platform",
        description=(
            "Compliance calendar, notices, document vault, statutory registers, "
            "scoped access for your CA, practice management, and an append-only "
            "audit trail — one system rather than six."
        ),
        breadcrumbs=(HOME_CRUMB, Breadcrumb(label="Product")),
    )
    return _page(
        request,
        "marketing/product.html",
        meta,
        {"modules": MODULES, "audiences": AUDIENCES},
    )


@public_view
@cache_page(CACHE_SECONDS)
def module(request: HttpRequest, slug: str) -> HttpResponse:
    item = content.module_by_slug(slug)
    if item is None:
        raise Http404(f"No product module named {slug!r}")

    structured: tuple[dict[str, Any], ...] = (seo.faq_jsonld(item.faqs),) if item.faqs else ()
    meta = PageMeta(
        title=item.name,
        description=f"{item.nav_summary} {item.headline}",
        breadcrumbs=(
            HOME_CRUMB,
            Breadcrumb(label="Product", url=reverse("marketing:product")),
            Breadcrumb(label=item.name),
        ),
        structured_data=structured,
    )
    others = tuple(m for m in MODULES if m.slug != slug)
    return _page(request, "marketing/module.html", meta, {"module": item, "others": others})


# ===========================================================================
# Solutions
# ===========================================================================


@public_view
@cache_page(CACHE_SECONDS)
def audience(request: HttpRequest, slug: str) -> HttpResponse:
    item = content.audience_by_slug(slug)
    if item is None:
        raise Http404(f"No audience page named {slug!r}")

    structured: tuple[dict[str, Any], ...] = (seo.faq_jsonld(item.faqs),) if item.faqs else ()
    meta = PageMeta(
        title=item.name,
        description=item.lede[:300],
        breadcrumbs=(
            HOME_CRUMB,
            Breadcrumb(label="Solutions", url=reverse("marketing:product")),
            Breadcrumb(label=item.name),
        ),
        structured_data=structured,
    )
    return _page(
        request,
        "marketing/audience.html",
        meta,
        {
            "audience": item,
            "modules": tuple(m for m in MODULES if m.slug in item.module_slugs),
            "plan": content.plan_by_code(item.plan_code),
            "others": tuple(a for a in AUDIENCES if a.slug != slug),
        },
    )


@public_view
@cache_page(CACHE_SECONDS)
def partners(request: HttpRequest) -> HttpResponse:
    meta = PageMeta(
        title="Partners",
        description=(
            "Bring your client book onto STACOS, resell it under your own brand, or "
            "build on the API. Three partner tracks, with terms for each."
        ),
        breadcrumbs=(HOME_CRUMB, Breadcrumb(label="Partners")),
    )
    return _page(request, "marketing/partners.html", meta, {"tracks": PARTNER_TRACKS})


# ===========================================================================
# Pricing
# ===========================================================================


@public_view
@cache_page(CACHE_SECONDS)
def pricing(request: HttpRequest) -> HttpResponse:
    meta = PageMeta(
        title="Pricing",
        description=(
            "Plans from ₹999 a month for one entity to Enterprise with custom roles, "
            "SSO and an API. Billed in INR, 14-day trial, no card required."
        ),
        breadcrumbs=(HOME_CRUMB, Breadcrumb(label="Pricing")),
        structured_data=(
            seo.product_jsonld(low_price="999", high_price="7999"),
            seo.faq_jsonld(PRICING_FAQS),
        ),
    )
    return _page(
        request,
        "marketing/pricing.html",
        meta,
        {
            "plans": PLANS,
            "add_ons": ADD_ONS,
            "comparison_rows": COMPARISON_ROWS,
            "faqs": PRICING_FAQS,
        },
    )


# ===========================================================================
# Trust
# ===========================================================================


@public_view
@cache_page(CACHE_SECONDS)
def security(request: HttpRequest) -> HttpResponse:
    """The page an enterprise procurement team asks for."""
    meta = PageMeta(
        title="Security and privacy",
        description=(
            "Tenant isolation enforced twice, dual-channel authentication, step-up on "
            "sensitive actions, an append-only audit trail, consented support access, "
            "and Indian data residency."
        ),
        breadcrumbs=(HOME_CRUMB, Breadcrumb(label="Security")),
    )
    return _page(request, "marketing/security.html", meta, {})


@public_view
@cache_page(CACHE_SECONDS)
def subprocessors(request: HttpRequest) -> HttpResponse:
    meta = PageMeta(
        title="Sub-processors",
        description=(
            "The third parties that process customer data on our behalf, what each "
            "one does, and where it is located."
        ),
        breadcrumbs=(
            HOME_CRUMB,
            Breadcrumb(label="Security", url=reverse("marketing:security")),
            Breadcrumb(label="Sub-processors"),
        ),
    )
    return _page(
        request,
        "marketing/subprocessors.html",
        meta,
        {"subprocessors": SUBPROCESSORS},
    )


# ===========================================================================
# Resources
# ===========================================================================


@public_view
@cache_page(CACHE_SECONDS)
def resources(request: HttpRequest) -> HttpResponse:
    meta = PageMeta(
        title="Guides",
        description=(
            "Long-form writing on compliance operations: what a calendar should "
            "contain, evidence that survives an audit, multi-state growth, and how to "
            "evaluate compliance software."
        ),
        breadcrumbs=(HOME_CRUMB, Breadcrumb(label="Guides")),
    )
    topics = tuple(dict.fromkeys(item.topic for item in GUIDES))
    return _page(request, "marketing/resources.html", meta, {"guides": GUIDES, "topics": topics})


@public_view
@cache_page(CACHE_SECONDS)
def guide(request: HttpRequest, slug: str) -> HttpResponse:
    item = content.guide_by_slug(slug)
    if item is None:
        raise Http404(f"No guide named {slug!r}")

    structured: list[dict[str, Any]] = [
        seo.article_jsonld(
            headline=item.title,
            description=item.summary,
            url=reverse("marketing:guide", args=[item.slug]),
            published=item.published.isoformat(),
            modified=item.updated.isoformat(),
        )
    ]
    if item.faqs:
        structured.append(seo.faq_jsonld(item.faqs))

    meta = PageMeta(
        title=item.title,
        description=item.summary,
        breadcrumbs=(
            HOME_CRUMB,
            Breadcrumb(label="Guides", url=reverse("marketing:resources")),
            Breadcrumb(label=item.title),
        ),
        og_type="article",
        structured_data=tuple(structured),
        published=item.published.isoformat(),
        modified=item.updated.isoformat(),
    )
    related = tuple(g for g in GUIDES if g.slug in item.related_slugs)
    return _page(request, "marketing/guide.html", meta, {"guide": item, "related": related})


@public_view
@cache_page(CACHE_SECONDS)
def faq(request: HttpRequest) -> HttpResponse:
    every_faq = tuple(item for section in FAQ_SECTIONS for item in section.faqs)
    meta = PageMeta(
        title="Frequently asked questions",
        description=(
            "What STACOS does and does not do, how access for your CA works, where "
            "your data lives, what it costs, and how to get started."
        ),
        breadcrumbs=(HOME_CRUMB, Breadcrumb(label="FAQ")),
        structured_data=(seo.faq_jsonld(every_faq),),
    )
    return _page(request, "marketing/faq.html", meta, {"sections": FAQ_SECTIONS})


@public_view
@cache_page(CACHE_SECONDS)
def changelog(request: HttpRequest) -> HttpResponse:
    meta = PageMeta(
        title="What's new",
        description=(
            "Everything that has shipped in STACOS, newest first — because “what "
            "changed, and when” is itself a compliance question."
        ),
        breadcrumbs=(HOME_CRUMB, Breadcrumb(label="What's new")),
    )
    return _page(request, "marketing/changelog.html", meta, {"entries": CHANGELOG})


# ===========================================================================
# Company
# ===========================================================================


@public_view
@cache_page(CACHE_SECONDS)
def about(request: HttpRequest) -> HttpResponse:
    meta = PageMeta(
        title="About",
        description=(
            "Why STACOS exists: businesses and the professionals who serve them work "
            "from different copies of the same list. We built one workspace, with a "
            "record that holds up."
        ),
        breadcrumbs=(HOME_CRUMB, Breadcrumb(label="About")),
        structured_data=(seo.organisation_jsonld(),),
    )
    return _page(request, "marketing/about.html", meta, {})


@public_view
@cache_page(CACHE_SECONDS)
def careers(request: HttpRequest) -> HttpResponse:
    meta = PageMeta(
        title="Careers",
        description=(
            "Open roles at STACOS in engineering, compliance content, design and "
            "customer success. Mumbai and remote within India."
        ),
        breadcrumbs=(HOME_CRUMB, Breadcrumb(label="Careers")),
    )
    return _page(request, "marketing/careers.html", meta, {"roles": OPEN_ROLES})


def _contact_meta() -> PageMeta:
    return PageMeta(
        title="Contact",
        description=(
            "Talk to sales, get support as a customer, send a security questionnaire, "
            "or ask about partnering. One form, routed to the right inbox."
        ),
        breadcrumbs=(HOME_CRUMB, Breadcrumb(label="Contact")),
    )


def _enquiry_allowed(request: HttpRequest) -> bool:
    """Per-IP rate limit for enquiries.

    Cache-based and therefore best-effort: a cache flush resets the window. That
    is an acceptable trade for a form whose worst case is unwanted email, and it
    keeps an unauthenticated request off the database entirely.
    """
    ip_address = current_request_meta().ip_address or request.META.get("REMOTE_ADDR") or "unknown"
    key = f"stacos:contact:{ip_address}"
    if (cache.get_or_set(key, 0, CONTACT_WINDOW_SECONDS) or 0) >= CONTACT_LIMIT:
        return False
    try:
        cache.incr(key)
    except ValueError:  # pragma: no cover — the key expired between get and incr
        cache.set(key, 1, CONTACT_WINDOW_SECONDS)
    return True


@public_view
@require_http_methods(["GET", "POST"])
def contact(request: HttpRequest) -> HttpResponse:
    """The enquiry form. Not cached, and the only unauthenticated write here."""
    form = ContactForm(request.POST or None)

    def render_form() -> HttpResponse:
        return _page(request, "marketing/contact.html", _contact_meta(), {"form": form})

    if request.method != "POST":
        return render_form()

    if not _enquiry_allowed(request):
        messages.error(
            request,
            _("Too many enquiries from this connection. Please email us directly instead."),
        )
        return render_form()

    if not form.is_valid():
        return render_form()

    topic = form.cleaned_data["topic"]
    inbox = settings.MARKETING_ENQUIRY_INBOX.get(topic, settings.MARKETING_ENQUIRY_INBOX["default"])
    EmailMessage(
        subject=f"[STACOS enquiry] {topic} — {form.cleaned_data['name']}",
        body=form.as_email_body(),
        to=[inbox],
        # Reply-To rather than From: sending as the visitor's own address would
        # fail SPF and land the enquiry in spam — the one outcome nobody wants.
        reply_to=[form.cleaned_data["email"]],
    ).send(fail_silently=False)
    logger.info("marketing.enquiry", topic=topic, inbox=inbox)

    messages.success(
        request,
        _("Thank you — your enquiry is with us. We reply on working days, usually within one."),
    )
    return redirect(f"{reverse('marketing:contact')}?sent=1")


# ===========================================================================
# Legal
# ===========================================================================


@public_view
@cache_page(CACHE_SECONDS)
def legal_index(request: HttpRequest) -> HttpResponse:
    meta = PageMeta(
        title="Legal",
        description=(
            "Terms of service, privacy policy, DPDP notice, cookie policy, "
            "cancellation and refunds, acceptable use, service levels and the data "
            "processing addendum."
        ),
        breadcrumbs=(HOME_CRUMB, Breadcrumb(label="Legal")),
    )
    return _page(request, "marketing/legal/index.html", meta, {"documents": LEGAL_DOCUMENTS})


@public_view
@cache_page(CACHE_SECONDS)
def legal(request: HttpRequest, slug: str) -> HttpResponse:
    document = content.legal_by_slug(slug)
    if document is None:
        raise Http404(f"No legal document named {slug!r}")

    meta = PageMeta(
        title=document.title,
        description=document.summary,
        breadcrumbs=(
            HOME_CRUMB,
            Breadcrumb(label="Legal", url=reverse("marketing:legal_index")),
            Breadcrumb(label=document.title),
        ),
        modified=document.effective.isoformat(),
    )
    return _page(
        request,
        document.template,
        meta,
        {"document": document, "documents": LEGAL_DOCUMENTS},
    )


# ===========================================================================
# Crawler directives
#
# Served by the application rather than as a static file so they cannot drift
# from the URLs above, and so a staging deployment refuses indexing by
# configuration rather than by someone remembering to swap a file.
# ===========================================================================


@public_view
def robots_txt(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "marketing/robots.txt",
        {
            "sitemap_url": seo.absolute(reverse("sitemap")),
            "allow_indexing": settings.MARKETING_ALLOW_INDEXING,
        },
        content_type="text/plain; charset=utf-8",
    )
