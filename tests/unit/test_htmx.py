"""The dual-render convention and out-of-band responses."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.test import RequestFactory

from stacos.core.htmx import (
    Fragment,
    HtmxFragmentMixin,
    Toast,
    derive_fragment_template,
    is_fragment_request,
    oob,
    trigger,
)


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        ("obligations/list.html", "obligations/_fragments/list_body.html"),
        ("tenancy/entity_detail.html", "tenancy/_fragments/entity_detail_body.html"),
        ("dashboard.html", "_fragments/dashboard_body.html"),
    ],
)
def test_fragment_paths_are_derived_by_convention(page: str, expected: str) -> None:
    """Derived rather than hand-wired, so nobody has to remember to add it."""
    assert derive_fragment_template(page) == expected


def test_plain_request_is_not_a_fragment_request() -> None:
    request = RequestFactory().get("/app/")
    assert not is_fragment_request(request)


def test_htmx_request_is_a_fragment_request() -> None:
    request = RequestFactory().get("/app/")
    request.htmx = SimpleNamespace(history_restore_request=False)
    assert is_fragment_request(request)


def test_history_restore_is_not_a_fragment_request() -> None:
    """HTMX repopulating its history cache expects the same content it stored.

    Returning a bare fragment there produces a page with no shell after pressing
    Back — a subtle bug that only shows up once the history cache has evicted.
    """
    request = RequestFactory().get("/app/")
    request.htmx = SimpleNamespace(history_restore_request=True)
    assert not is_fragment_request(request)


class _View(HtmxFragmentMixin):
    template_name = "tenancy/entity_list.html"

    def __init__(self, request) -> None:
        self.request = request


def test_mixin_returns_the_page_for_a_direct_hit() -> None:
    view = _View(RequestFactory().get("/app/entities/"))
    assert view.get_template_names() == ["tenancy/entity_list.html"]


def test_mixin_returns_the_fragment_for_an_htmx_request() -> None:
    request = RequestFactory().get("/app/entities/")
    request.htmx = SimpleNamespace(history_restore_request=False)
    view = _View(request)
    assert view.get_template_names() == ["tenancy/_fragments/entity_list_body.html"]


def test_mixin_honours_an_explicit_override() -> None:
    request = RequestFactory().get("/app/entities/")
    request.htmx = SimpleNamespace(history_restore_request=False)
    view = _View(request)
    view.fragment_template_name = "custom/thing.html"
    assert view.get_template_names() == ["custom/thing.html"]


def test_oob_carries_a_toast_as_a_trigger_not_a_swap() -> None:
    """Delivering a toast as a DOM swap fights the main swap for the same
    region; an HX-Trigger event does not."""
    request = RequestFactory().get("/app/")
    response = oob(request, "<div>row</div>", toast=Toast("Saved.", level="success"))

    assert response.status_code == 200
    payload = json.loads(response["HX-Trigger"])
    assert payload["stacos:toast"]["message"] == "Saved."
    assert payload["stacos:toast"]["level"] == "success"


def test_oob_requires_a_target_for_additional_fragments() -> None:
    """A fragment in `also=` with no target would be swapped into the main
    region and silently replace the wrong thing."""
    request = RequestFactory().get("/app/")
    with pytest.raises(ValueError, match="oob_target"):
        oob(request, "<div/>", also=[Fragment("some/template.html", {})])


def test_trigger_preserves_existing_events() -> None:
    from django.http import HttpResponse

    response = HttpResponse("")
    trigger(response, "first", {"a": 1})
    trigger(response, "second", {"b": 2})

    payload = json.loads(response["HX-Trigger"])
    assert payload == {"first": {"a": 1}, "second": {"b": 2}}


def test_retarget_and_reswap_headers() -> None:
    request = RequestFactory().get("/app/")
    response = oob(request, "<div/>", retarget="#form", reswap="outerHTML")
    assert response["HX-Retarget"] == "#form"
    assert response["HX-Reswap"] == "outerHTML"
