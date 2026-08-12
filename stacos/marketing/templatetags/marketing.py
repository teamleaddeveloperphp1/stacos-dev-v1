"""
Template helpers for the public surface.

Only one tag, and it exists because a :class:`NavItem` may or may not carry
arguments: ``{% url %}`` cannot express "reverse this name, with these args if
there are any" without the template duplicating the loop for each case. The
navigation data itself arrives through
:func:`stacos.marketing.context_processors.marketing_chrome`.
"""

from __future__ import annotations

from django import template
from django.urls import reverse

from stacos.marketing.content import NavItem

register = template.Library()


@register.simple_tag(name="nav_url")
def nav_url(item: NavItem) -> str:
    """Resolve a :class:`NavItem` to a path."""
    return reverse(item.url_name, args=list(item.args))
