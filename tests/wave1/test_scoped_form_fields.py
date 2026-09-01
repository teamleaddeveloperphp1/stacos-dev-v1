"""
Scoped pickers must actually render their options.

Every create form in the product hangs off a tenant-scoped entity picker, and all
four rendered a ``<select>`` with no ``<option>`` elements in it — not even the
``---------`` empty label. The forms were therefore unusable in a browser while
remaining perfectly valid on POST, which is why the existing tests, which only
ever posted, stayed green.

The cause was that ``ScopedModelChoiceField`` overrode ``queryset`` as a plain
property and so dropped the side effect Django's own setter performs
(``self.widget.choices = self.choices``). Rendering reads the *widget's* choices;
validation reads the field's. Hence one working and the other not.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import tenant_context
from stacos.notices.forms import NoticeForm
from stacos.requests.forms import RequestForm
from stacos.secretarial.forms import MeetingForm
from stacos.tenancy.models import Entity, Tenant
from stacos.vault.forms import UploadForm
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

FORMS = [
    (RequestForm, "information request"),
    (NoticeForm, "notice"),
    (MeetingForm, "meeting"),
    (UploadForm, "document upload"),
]


@pytest.mark.parametrize(("factory", "label"), FORMS, ids=[label for _, label in FORMS])
def test_the_entity_picker_renders_its_options(
    factory: type, label: str, org: Tenant, entity_a: Entity
) -> None:
    with tenant_context(tenant_ids=org.id, reason="test"):
        html = str(factory()["entity"])

    assert f'value="{entity_a.pk}"' in html, f"the {label} form's entity picker rendered no options"
    assert entity_a.name in html


@pytest.mark.parametrize(("factory", "label"), FORMS, ids=[label for _, label in FORMS])
def test_the_picker_shows_nothing_from_another_tenant(
    factory: type, label: str, org: Tenant, entity_a: Entity, rival_entity: Entity
) -> None:
    with tenant_context(tenant_ids=org.id, reason="test"):
        html = str(factory()["entity"])

    assert str(rival_entity.pk) not in html, f"the {label} form offers another tenant's entity"


def test_the_forms_module_imports_with_no_scope_bound() -> None:
    """The whole reason these fields exist.

    Django builds a ``ModelChoiceField``'s queryset when the class body runs, at
    import time, when nothing is bound. A regression here does not fail a form —
    it fails the URL conf, and the entire site returns 500.
    """
    import importlib

    for module in (
        "stacos.core.forms",
        "stacos.requests.forms",
        "stacos.notices.forms",
        "stacos.secretarial.forms",
        "stacos.vault.forms",
        "stacos.practice.forms",
    ):
        importlib.reload(importlib.import_module(module))


def test_a_forged_entity_id_is_refused(org: Tenant, rival_entity: Entity) -> None:
    """Resolution happens again at validation, not only at render."""
    with tenant_context(tenant_ids=org.id, reason="test"):
        form = RequestForm(data={"entity": str(rival_entity.pk)})
        form.is_valid()
        assert form.errors.get("entity"), "another tenant's entity was accepted"


@pytest.fixture
def signed_in(client: Client, org_owner: User) -> Client:
    return sign_in(client, org_owner, step_up=True)


@pytest.mark.parametrize(
    ("route", "label"),
    [
        ("rfi:create", "information request"),
        ("notices:create", "notice"),
    ],
    ids=["information request", "notice"],
)
def test_the_create_screen_offers_the_entity(
    route: str, label: str, signed_in: Client, entity_a: Entity
) -> None:
    """End to end, through the real view and template, as a signed-in user."""
    response = signed_in.get(reverse(route), headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert f'value="{entity_a.pk}"'.encode() in response.content, (
        f"the {label} modal rendered an empty entity picker"
    )
