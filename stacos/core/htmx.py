"""
The dual-render convention, and out-of-band responses.

Everything about how STACOS *feels* rests on two habits established here.

**One page is two templates.** ``obligations/list.html`` extends the app shell
and its ``{% block main %}`` contains nothing but an include of
``obligations/_fragments/list_body.html``. A direct GET renders the page; an
HTMX request renders only the fragment. Because the full page still exists, deep
links, the back button and "open in new tab" work with no special handling — the
thing most HTMX apps get wrong.

**One request, several regions.** Closing an obligation should update the row,
the sidebar counter, the header badge and the activity feed. Doing that with four
requests is slow and races; :func:`oob` does it in one response using HTMX's
out-of-band swaps.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.template.loader import render_to_string

__all__ = ["Fragment", "HtmxFragmentMixin", "Toast", "is_fragment_request", "oob", "trigger"]


def is_fragment_request(request: HttpRequest) -> bool:
    """True when the response should be a fragment rather than a full page.

    A boosted request (``hx-boost`` on the nav) is still a fragment request: the
    shell is already on the page and only ``#main`` is being replaced. What must
    *not* be treated as a fragment is a history-restore request, where HTMX
    repopulates its cache and expects the same content it originally stored.
    """
    htmx = getattr(request, "htmx", None)
    if not htmx:
        return False
    return not bool(getattr(htmx, "history_restore_request", False))


@dataclass(slots=True)
class Fragment:
    """A template plus its context, ready to render."""

    template: str
    context: dict[str, Any] = field(default_factory=dict)
    #: DOM id to swap out of band. ``None`` means this is the main response body.
    oob_target: str | None = None
    #: HTMX swap strategy for the out-of-band swap.
    swap: str = "outerHTML"

    def render(self, request: HttpRequest) -> str:
        html = render_to_string(self.template, self.context, request=request)
        if self.oob_target is None:
            return html
        return _wrap_oob(html, target=self.oob_target, swap=self.swap)


@dataclass(slots=True)
class Toast:
    """A transient message. Delivered as an HTMX trigger, not as a DOM swap."""

    message: str
    level: str = "success"  # success | info | warning | danger
    title: str = ""

    def as_payload(self) -> dict[str, str]:
        return {"message": self.message, "level": self.level, "title": self.title}


def oob(
    request: HttpRequest,
    main: Fragment | str | None = None,
    *,
    also: Sequence[Fragment] = (),
    toast: Toast | None = None,
    triggers: dict[str, Any] | None = None,
    status: int = 200,
    retarget: str | None = None,
    reswap: str | None = None,
) -> HttpResponse:
    """Build one response that updates several regions.

    :param main: the primary swap, targeted by the triggering element.
    :param also: additional fragments, each with an ``oob_target``.
    :param toast: a message, delivered via ``HX-Trigger`` so the shell's toast
        container renders it without a DOM swap fighting the main one.
    :param triggers: extra client-side events, e.g. ``{"obligationClosed": {...}}``.
    :param retarget: override the client's target (``HX-Retarget``) — useful for
        rendering a validation error into a form that was not the trigger.

    >>> return oob(
    ...     request,
    ...     Fragment("obligations/_fragments/row.html", {"obligation": obligation}),
    ...     also=[Fragment("core/_fragments/badge.html", {"count": n}, oob_target="nav-badge")],
    ...     toast=Toast("Obligation closed."),
    ... )
    """
    parts: list[str] = []

    if isinstance(main, str):
        parts.append(main)
    elif main is not None:
        parts.append(main.render(request))

    for fragment in also:
        if fragment.oob_target is None:
            raise ValueError(
                f"Fragment {fragment.template!r} passed in `also=` needs an oob_target; "
                f"only the `main` fragment may be swapped normally."
            )
        parts.append(fragment.render(request))

    response = HttpResponse("\n".join(parts), status=status)

    events: dict[str, Any] = dict(triggers or {})
    if toast is not None:
        events["stacos:toast"] = toast.as_payload()
    if events:
        response["HX-Trigger"] = json.dumps(events)
    if retarget:
        response["HX-Retarget"] = retarget
    if reswap:
        response["HX-Reswap"] = reswap

    return response


def trigger(response: HttpResponse, name: str, payload: Any = None) -> HttpResponse:
    """Add a client-side event to an existing response, preserving any already set."""
    existing: dict[str, Any] = {}
    if response.has_header("HX-Trigger"):
        try:
            existing = json.loads(response["HX-Trigger"])
        except (ValueError, TypeError):
            existing = {response["HX-Trigger"]: None}
    existing[name] = payload
    response["HX-Trigger"] = json.dumps(existing)
    return response


def _wrap_oob(html: str, *, target: str, swap: str) -> str:
    """Wrap rendered HTML so HTMX swaps it out of band.

    A wrapper div is used rather than requiring every component to carry
    ``hx-swap-oob`` itself, so the same partial can be rendered either normally
    or out of band without knowing which.
    """
    return f'<div id="{target}" hx-swap-oob="{swap}:#{target}">{html}</div>'


class HtmxFragmentMixin:
    """Give a class-based view both render paths.

    The fragment template is derived by convention, so nobody wires it by hand:

        ``obligations/list.html``  ->  ``obligations/_fragments/list_body.html``

    Override ``fragment_template_name`` when a view needs something else.
    """

    # Not ClassVar: Django's TemplateResponseMixin declares template_name as an
    # instance variable, and a view may override the fragment per instance.
    template_name: str = ""
    fragment_template_name: str = ""
    #: Some views (a modal body, a table row) only ever render as a fragment.
    fragment_only: bool = False

    request: HttpRequest

    def get_template_names(self) -> list[str]:
        if self.fragment_only:
            return [self.get_fragment_template_name()]
        if is_fragment_request(self.request):
            return [self.get_fragment_template_name()]
        return [self.get_page_template_name()]

    def get_page_template_name(self) -> str:
        if not self.template_name:
            raise ValueError(f"{type(self).__name__} must set `template_name`.")
        return self.template_name

    def get_fragment_template_name(self) -> str:
        if self.fragment_template_name:
            return self.fragment_template_name
        return derive_fragment_template(self.get_page_template_name())


def derive_fragment_template(page_template: str) -> str:
    """``app/list.html`` -> ``app/_fragments/list_body.html``."""
    prefix, _, filename = page_template.rpartition("/")
    stem = filename.removesuffix(".html")
    return f"{prefix}/_fragments/{stem}_body.html" if prefix else f"_fragments/{stem}_body.html"
