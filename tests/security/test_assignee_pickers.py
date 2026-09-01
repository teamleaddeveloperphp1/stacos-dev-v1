"""
The assignee pickers must not name other customers' staff.

``User`` is deliberately global — one person, one login, many tenants — so a
``ModelForm`` left to build ``assigned_to`` or ``reviewer`` for itself produces a
``<select>`` over every user on the platform. That is a disclosure (other
customers' staff names, readable by anyone who can open the form) and a write
hole as well: the POST validated, so a record could genuinely be assigned to
somebody at an unrelated company.

``tests/security/test_tenant_isolation.py`` already covers the ``Membership``
model, but these pickers enumerate ``User`` directly and so went straight past
that guarantee. These tests assert on the *rendered* form, which is the surface
the disclosure actually happened on.
"""

from __future__ import annotations

from typing import Any

import pytest

from stacos.core.scope import tenant_context
from stacos.notices.forms import NoticeForm
from stacos.practice.forms import WorkItemForm
from stacos.requests.forms import RequestForm

pytestmark = [pytest.mark.django_db, pytest.mark.isolation]


def _rendered(form: Any, field: str) -> str:
    return str(form[field])


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (RequestForm, "assigned_to"),
        (NoticeForm, "assigned_to"),
        (WorkItemForm, "assigned_to"),
        (WorkItemForm, "reviewer"),
    ],
    ids=["request-assigned", "notice-assigned", "work-assigned", "work-reviewer"],
)
def test_the_picker_names_nobody_from_another_tenant(
    factory: Any, field: str, org: Any, org_owner: Any, other_org: Any, rival_owner: Any
) -> None:
    with tenant_context(tenant_ids=org.id, reason="test"):
        html = _rendered(factory(), field)

    assert org_owner.email.split("@")[0] in html or str(org_owner.pk) in html, (
        "the caller's own colleagues should be selectable"
    )
    assert str(rival_owner.pk) not in html, "another tenant's user id appears in the picker"
    assert "rival" not in html.lower(), "another tenant's user is named in the picker"


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (RequestForm, "assigned_to"),
        (NoticeForm, "assigned_to"),
        (WorkItemForm, "assigned_to"),
        (WorkItemForm, "reviewer"),
    ],
    ids=["request-assigned", "notice-assigned", "work-assigned", "work-reviewer"],
)
def test_a_forged_user_id_is_refused(
    factory: Any, field: str, org: Any, other_org: Any, rival_owner: Any
) -> None:
    """Absent from the rendered options is not enough — the POST must be rejected.

    The queryset is re-resolved at validation time precisely so that editing the
    HTML, or replaying somebody else's form, does not get past it.
    """
    with tenant_context(tenant_ids=org.id, reason="test"):
        form = factory(data={field: str(rival_owner.pk)})
        form.is_valid()  # other fields will fail too; only this one is under test
        errors = form.errors.get(field, [])

    assert errors, f"{factory.__name__}.{field} accepted a user from another tenant"


def test_the_pickers_still_offer_the_callers_own_people(org: Any, org_owner: Any) -> None:
    """The narrowing must not be so tight that nobody is selectable at all.

    A picker that lists nobody is the same bug as the empty entity dropdown, just
    caused from the other direction.
    """
    with tenant_context(tenant_ids=org.id, reason="test"):
        for factory, field in (
            (RequestForm, "assigned_to"),
            (NoticeForm, "assigned_to"),
            (WorkItemForm, "assigned_to"),
            (WorkItemForm, "reviewer"),
        ):
            html = _rendered(factory(), field)
            assert f'value="{org_owner.pk}"' in html, (
                f"{factory.__name__}.{field} does not offer the caller's own colleague"
            )
