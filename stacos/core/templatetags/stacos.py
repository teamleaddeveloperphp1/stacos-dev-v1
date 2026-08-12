"""
Template filters and tags registered as builtins, so no ``{% load %}`` is needed.

Kept small on purpose. Presentation logic belongs in cotton components; this
module holds only the formatting and permission primitives those components need.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from django import template
from django.http import HttpRequest
from django.utils.safestring import SafeString, mark_safe

from stacos.core.formatters import DigitGrouping, format_compact, format_currency, format_number

register = template.Library()


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------


@register.filter(name="inr")
def inr(value: Decimal | float | int | str | None, decimals: int = 2) -> str:
    """``{{ amount|inr }}`` -> ``₹1,23,45,678.00``."""
    return format_currency(value, decimals=int(decimals))


@register.filter(name="num")
def num(value: Decimal | float | int | str | None, decimals: int = 0) -> str:
    """``{{ count|num }}`` -> ``1,23,45,678`` (Indian grouping)."""
    return format_number(value, decimals=int(decimals))


@register.filter(name="minor")
def minor(value: int | None) -> Decimal:
    """Paise to rupees: ``{{ invoice.total_minor|minor|inr }}``.

    Billing stores every amount as an integer in minor units, so nothing can
    hand a float to the database. Rendering therefore needs exactly one place
    that divides by a hundred, and this is it — a template doing its own
    arithmetic is how a rounding difference reaches an invoice.
    """
    if value is None:
        return Decimal("0.00")
    try:
        return (Decimal(int(value)) / 100).quantize(Decimal("0.01"))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0.00")


@register.filter(name="bps")
def bps(value: int | None) -> str:
    """Basis points as a percentage: ``{{ rate_bps|bps }}`` -> ``18``.

    Rates are stored in basis points so "12.5%" is exact rather than a float
    that drifts by a rupee somewhere nobody looks.
    """
    if value is None:
        return "0"
    quotient = Decimal(int(value)) / 100
    return f"{quotient.normalize():f}"


@register.filter(name="compact")
def compact(value: Decimal | float | int | str | None) -> str:
    """``{{ turnover|compact }}`` -> ``₹8.00 Cr``."""
    return format_compact(value)


@register.filter(name="western")
def western(value: Decimal | float | int | str | None, decimals: int = 0) -> str:
    """Thousands grouping, for non-Indian jurisdictions."""
    return format_number(value, grouping=DigitGrouping.WESTERN, decimals=int(decimals))


@register.filter(name="abs")
def absolute(value: Any) -> Any:
    """Magnitude, for rendering a signed countdown as "12 days late".

    Django has no built-in ``abs``. Returns the value untouched if it is not a
    number, so a component can pass an optional attribute through without
    guarding first.
    """
    try:
        return abs(value)
    except TypeError:
        return value


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


@register.simple_tag(takes_context=True)
def can(context: template.Context, permission: str) -> bool:
    """``{% can 'finance.view' as may_see_money %}``.

    Reads the already-resolved scope, so it costs nothing — permission
    resolution happens once per request in the middleware, not per template tag.
    """
    request: HttpRequest | None = context.get("request")
    scope = getattr(request, "access_scope", None) if request else None
    return bool(scope and scope.has_permission(permission))


@register.simple_tag(takes_context=True)
def mask(context: template.Context, permission: str, value: Any, placeholder: str = "—") -> Any:
    """Show ``value`` only to holders of ``permission``.

    Field-level masking lives here so it is applied the same way everywhere: a
    user without ``finance.view`` sees an obligation's title and due date but a
    dash where the amount would be.
    """
    return value if can(context, permission) else placeholder


# ---------------------------------------------------------------------------
# HTMX helpers
# ---------------------------------------------------------------------------


@register.simple_tag(name="json_attr")
def json_attr(value: Any) -> SafeString:
    """Serialise a value for an Alpine ``x-data`` attribute."""
    return mark_safe(json.dumps(value).replace("'", "&#39;"))  # noqa: S308


@register.filter(name="fragment_of")
def fragment_of(page_template: str) -> str:
    """``obligations/list.html`` -> ``obligations/_fragments/list_body.html``."""
    from stacos.core.htmx import derive_fragment_template

    return derive_fragment_template(page_template)
