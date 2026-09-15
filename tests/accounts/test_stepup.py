"""Step-up re-authentication, switched off.

``STEP_UP_ENABLED`` is ``False`` in the running application. The suite runs with
it forced on (see ``config/settings/test.py``), so without this file the
disabled path — the one actually in production use — would be the only branch
nothing covers.

Both directions are asserted deliberately. A kill switch that silently stops
working is worse than no kill switch, and the failure mode of the "on" case is
invisible: sensitive actions simply stop being challenged and nothing logs it.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from stacos.accounts.models import User
from stacos.tenancy.models import Entity
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db

HTMX = {"HX-Request": "true"}


@override_settings(STEP_UP_ENABLED=False)
def test_a_sensitive_permission_is_not_challenged_when_step_up_is_off(
    client: Client, org_owner: User, entity_a: Entity
) -> None:
    """The point of the switch: no password prompt on a sensitive action.

    ``tenancy.registration.manage`` is sensitive, and this is the exact request
    that ``test_htmx_navigation`` shows bouncing to the step-up screen when the
    feature is on. Signed in without fresh re-authentication, it now just works.
    """
    signed_in = sign_in(client, org_owner, step_up=False)
    fragment = reverse("app:registration_create", args=[entity_a.pk])

    response = signed_in.get(fragment, headers=HTMX)

    assert response.status_code == 200
    assert "HX-Redirect" not in response, "the user was sent to the password prompt"


@override_settings(STEP_UP_ENABLED=True)
def test_the_switch_still_turns_the_challenge_back_on(
    client: Client, org_owner: User, entity_a: Entity
) -> None:
    """Re-enabling must be one line, so prove the line still does something.

    This is the assertion that makes the setting's promise checkable: the
    machinery behind the switch has not rotted while it was off.
    """
    signed_in = sign_in(client, org_owner, step_up=False)
    page = reverse("app:entity_detail", args=[entity_a.pk])
    fragment = reverse("app:registration_create", args=[entity_a.pk])

    response = signed_in.get(
        fragment, headers={**HTMX, "HX-Current-URL": f"http://testserver{page}"}
    )

    assert response.status_code == 204
    assert response["HX-Redirect"].startswith(reverse("accounts:step_up"))
    assert quote(page) in response["HX-Redirect"]


@override_settings(STEP_UP_ENABLED=False)
def test_step_up_is_fresh_short_circuits_without_a_session_stamp(rf: object) -> None:
    """The funnel itself, with no session at all.

    ``require_step_up`` and ``permissions._enforce_step_up`` both route through
    this one function, so asserting it here covers the decorator path that no
    view currently exercises.
    """
    from django.contrib.sessions.backends.db import SessionStore

    from stacos.accounts.stepup import step_up_is_fresh

    request = rf.get("/")  # type: ignore[attr-defined]
    request.session = SessionStore()

    assert step_up_is_fresh(request) is True
