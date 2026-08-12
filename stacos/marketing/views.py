"""
Public marketing surface.

Same Django process as the application, different layout and URL namespace.
Anonymous, aggressively cached, and never touching tenant data — which is why
``/`` is excluded from scope resolution in ``ScopeMiddleware``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.cache import cache_page

from stacos.core.permissions import public_view


@dataclass(frozen=True, slots=True)
class Plan:
    code: str
    name: str
    tagline: str
    monthly_inr: Decimal
    annual_inr: Decimal
    included_entities: int
    included_users: int
    features: tuple[str, ...] = field(default_factory=tuple)
    highlight: bool = False
    cta: str = "Start free trial"

    @property
    def annual_saving_pct(self) -> int:
        """How much the annual commitment saves against paying monthly."""
        if not self.monthly_inr:
            return 0
        full = self.monthly_inr * 12
        return round((full - self.annual_inr) / full * 100)


PLANS: tuple[Plan, ...] = (
    Plan(
        code="starter",
        name="Starter",
        tagline="One entity, every due date, nothing missed.",
        monthly_inr=Decimal("999"),
        annual_inr=Decimal("9990"),
        included_entities=1,
        included_users=3,
        features=(
            "Automatic compliance calendar",
            "Due-date reminders by email and in-app",
            "Document vault with 5 GB storage",
            "Invite your CA or CS",
            "Mobile app",
        ),
    ),
    Plan(
        code="business",
        name="Business",
        tagline="Multiple entities, notices, and information requests.",
        monthly_inr=Decimal("3499"),
        annual_inr=Decimal("34990"),
        included_entities=3,
        included_users=10,
        features=(
            "Everything in Starter",
            "Up to 3 entities, multi-state",
            "Notice tracker with deadline countdown",
            "Information requests with reminders",
            "Statutory registers and minutes",
            "WhatsApp and SMS reminders",
            "Full audit trail export",
        ),
        highlight=True,
    ),
    Plan(
        code="practice",
        name="Practice",
        tagline="Run the whole firm: clients, work, time and billing.",
        monthly_inr=Decimal("7999"),
        annual_inr=Decimal("79990"),
        included_entities=25,
        included_users=15,
        features=(
            "Everything in Business",
            "Client book and portfolio risk board",
            "Work allocation and capacity heatmap",
            "Time tracking, WIP and billing",
            "Return preparation and maker-checker",
            "Practice-wide checklist templates",
            "Profitability by client and engagement",
        ),
    ),
    Plan(
        code="enterprise",
        name="Enterprise",
        tagline="Custom roles, SSO, API, and a data processing agreement.",
        monthly_inr=Decimal("0"),
        annual_inr=Decimal("0"),
        included_entities=0,
        included_users=0,
        features=(
            "Everything in Practice",
            "Unlimited entities and users",
            "Custom roles and department scoping",
            "SAML single sign-on",
            "API access and webhooks",
            "Data processing agreement and DPDP support",
            "Dedicated success manager",
        ),
        cta="Talk to sales",
    ),
)

ADD_ONS = (
    ("Additional entity", "₹399 / month", "Beyond your plan's included entities."),
    ("Additional user", "₹199 / month", "Beyond your plan's included seats."),
    ("Notice auto-retrieval", "₹999 / month", "Where the authority permits it."),
    ("WhatsApp reminders", "₹499 / month", "Per entity, DLT-registered templates."),
    ("White label", "₹4,999 / month", "Your logo and subdomain, for practices and dealers."),
)

COMPARISON_ROWS = (
    ("Compliance calendar", "1 entity", "3 entities", "25 entities", "Unlimited"),
    ("Users included", "3", "10", "15", "Unlimited"),
    ("Notice tracker", False, True, True, True),
    ("Information requests", False, True, True, True),
    ("Return preparation", False, False, True, True),
    ("Time tracking and billing", False, False, True, True),
    ("Custom roles", False, False, False, True),
    ("SAML single sign-on", False, False, False, True),
    ("API access", False, False, False, True),
)


@public_view
@cache_page(60 * 15)
def home(request: HttpRequest) -> HttpResponse:
    return render(request, "marketing/home.html", {"plans": PLANS})


@public_view
@cache_page(60 * 15)
def pricing(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "marketing/pricing.html",
        {"plans": PLANS, "add_ons": ADD_ONS, "comparison_rows": COMPARISON_ROWS},
    )


@public_view
def security(request: HttpRequest) -> HttpResponse:
    """The page an enterprise procurement team asks for."""
    return render(request, "marketing/security.html", {})
