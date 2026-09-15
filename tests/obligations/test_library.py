"""
The compliance library: bounded queries, representative-occurrence selection,
and the three actions that move a definition between states.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.engine.lifecycle import State
from stacos.engine.types import DefinitionSnapshot, InstanceScope, Periodicity
from stacos.obligations.library import (
    LibraryError,
    _blocked_reason,
    _representative,
    build_library,
    force_add_definition,
    remove_definition,
    restore_definition,
)
from stacos.obligations.models import (
    ObligationInclusion,
    ObligationInstance,
    ObligationSuppression,
)
from stacos.obligations.services import materialise
from stacos.tenancy.models import Entity, Tenant

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 8, 12)


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    from stacos.accounts.middleware import SESSION_VERIFIED_KEY

    client.force_login(org_owner)
    session = client.session
    session[SESSION_VERIFIED_KEY] = True
    session.save()
    return client


def _instance(**kwargs: object) -> ObligationInstance:
    """An unsaved instance, for testing the pure selection rule in isolation."""
    defaults: dict[str, object] = {
        "state": State.NOT_STARTED,
        "due_date": None,
        "closed_at": None,
        "filed_on": None,
    }
    defaults.update(kwargs)
    return ObligationInstance(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Representative occurrence selection
# ---------------------------------------------------------------------------


def test_open_beats_closed() -> None:
    open_row = _instance(state=State.IN_PREPARATION, due_date=date(2026, 9, 1))
    closed_row = _instance(state=State.CLOSED, closed_at=timezone.now())
    assert _representative([closed_row, open_row]) is open_row


def test_soonest_due_date_wins_among_open() -> None:
    soon = _instance(state=State.NOT_STARTED, due_date=date(2026, 9, 1))
    later = _instance(state=State.NOT_STARTED, due_date=date(2026, 10, 1))
    assert _representative([later, soon]) is soon


def test_an_open_row_with_no_due_date_loses_to_one_that_has_one() -> None:
    dated = _instance(state=State.NOT_STARTED, due_date=date(2026, 9, 1))
    undated = _instance(state=State.NOT_STARTED, due_date=None)
    assert _representative([undated, dated]) is dated


def test_most_recently_completed_wins_among_closed() -> None:
    old = _instance(state=State.CLOSED, closed_at=timezone.make_aware(datetime(2025, 1, 1)))
    new = _instance(state=State.CLOSED, closed_at=timezone.make_aware(datetime(2026, 1, 1)))
    assert _representative([old, new]) is new


def test_closed_without_a_closed_at_falls_back_to_filed_on() -> None:
    """Never a stale default — the fallback is still a real recorded date."""
    filed_recently = _instance(state=State.FILED, filed_on=date(2026, 6, 1))
    filed_long_ago = _instance(state=State.FILED, filed_on=date(2020, 1, 1))
    assert _representative([filed_long_ago, filed_recently]) is filed_recently


def test_not_applicable_falls_back_to_state_changed_at() -> None:
    recent = _instance(
        state=State.NOT_APPLICABLE,
        state_changed_at=timezone.make_aware(datetime(2026, 6, 1)),
    )
    stale = _instance(
        state=State.NOT_APPLICABLE,
        state_changed_at=timezone.make_aware(datetime(2020, 1, 1)),
    )
    assert _representative([stale, recent]) is recent


# ---------------------------------------------------------------------------
# Blocked-on-force-add hints
# ---------------------------------------------------------------------------


def _snapshot(
    *, instance_scope: InstanceScope, scope_selector: dict[str, str]
) -> DefinitionSnapshot:
    return DefinitionSnapshot(
        code="TEST-DEF",
        version=1,
        title="Test definition",
        country="IN",
        periodicity=Periodicity.MONTHLY,
        due_rule={},
        instance_scope=instance_scope,
        scope_selector=scope_selector,
    )


def test_blocked_when_the_required_registration_is_missing() -> None:
    snapshot = _snapshot(
        instance_scope=InstanceScope.REGISTRATION, scope_selector={"registration_type": "GST"}
    )
    assert _blocked_reason(snapshot, registration_types=set(), premises_types=set())


def test_not_blocked_when_the_required_registration_is_on_file() -> None:
    snapshot = _snapshot(
        instance_scope=InstanceScope.REGISTRATION, scope_selector={"registration_type": "GST"}
    )
    assert not _blocked_reason(snapshot, registration_types={"GST"}, premises_types=set())


def test_blocked_when_the_required_premises_type_is_missing() -> None:
    snapshot = _snapshot(
        instance_scope=InstanceScope.PREMISES, scope_selector={"premises_type": "FACTORY"}
    )
    assert _blocked_reason(snapshot, registration_types=set(), premises_types=set())


# ---------------------------------------------------------------------------
# build_library — bounded queries, and a consistent read of the register
# ---------------------------------------------------------------------------


def test_build_library_is_bounded_regardless_of_catalog_size(
    materialised: Entity, django_assert_max_num_queries: object
) -> None:
    with platform_scope(reason="test"), django_assert_max_num_queries(15):  # type: ignore[operator]
        build_library(materialised, as_of=AS_OF)


def test_every_row_with_a_live_instance_is_added(materialised: Entity) -> None:
    with platform_scope(reason="test"):
        rows = build_library(materialised, as_of=AS_OF)
        added_codes = set(
            ObligationInstance.objects.filter(
                entity=materialised, superseded_at__isnull=True, archived_at__isnull=True
            ).values_list("definition_code", flat=True)
        )

    for row in rows:
        if row.code in added_codes:
            assert row.state == "added"
            assert row.occurrence is not None
        else:
            assert row.state in {"not_added", "removed"}


# ---------------------------------------------------------------------------
# remove_definition
# ---------------------------------------------------------------------------


def _an_added_code(entity: Entity) -> str:
    with platform_scope(reason="test"):
        row = next(row for row in build_library(entity, as_of=AS_OF) if row.state == "added")
        return row.code


def _a_not_added_code(entity: Entity) -> str:
    with platform_scope(reason="test"):
        row = next(row for row in build_library(entity, as_of=AS_OF) if row.state == "not_added")
        return row.code


def test_removing_a_definition_blocks_a_rebuild_from_bringing_it_back(
    materialised: Entity, org_owner: User
) -> None:
    code = _an_added_code(materialised)

    with platform_scope(reason="test"):
        remove_definition(materialised, code, actor=org_owner, reason="Client says N/A.")
        materialise(materialised, as_of=AS_OF, trigger="MANUAL", actor=org_owner)

        assert not ObligationInstance.objects.filter(
            entity=materialised, definition_code=code, superseded_at__isnull=True
        ).exists()
        rows = build_library(materialised, as_of=AS_OF)
        assert next(row for row in rows if row.code == code).state == "removed"


def test_removing_a_definition_dismisses_its_open_instances(
    materialised: Entity, org_owner: User
) -> None:
    code = _an_added_code(materialised)

    with platform_scope(reason="test"):
        open_before = list(
            ObligationInstance.objects.filter(
                entity=materialised, definition_code=code, superseded_at__isnull=True
            )
        )
        assert open_before

        remove_definition(materialised, code, actor=org_owner, reason="Not applicable.")

        for instance in open_before:
            instance.refresh_from_db()
            assert instance.state == State.NOT_APPLICABLE
            assert instance.superseded_at is not None


def test_removing_a_definition_cancels_an_earlier_force_add(
    materialised: Entity, org_owner: User
) -> None:
    code = _a_not_added_code(materialised)

    with platform_scope(reason="test"):
        try:
            force_add_definition(
                materialised, code, actor=org_owner, reason="We do file this.", as_of=AS_OF
            )
        except LibraryError:
            pytest.skip("no force-addable definition was blocked-free in this catalog")

        remove_definition(materialised, code, actor=org_owner, reason="Changed our mind.")

        assert not ObligationInclusion.objects.filter(
            entity=materialised, definition_code=code, revoked_at__isnull=True
        ).exists()


def test_removing_an_already_removed_definition_refuses(
    materialised: Entity, org_owner: User
) -> None:
    code = _an_added_code(materialised)
    with platform_scope(reason="test"):
        remove_definition(materialised, code, actor=org_owner, reason="Not applicable.")
        with pytest.raises(LibraryError):
            remove_definition(materialised, code, actor=org_owner, reason="Again.")


# ---------------------------------------------------------------------------
# restore_definition
# ---------------------------------------------------------------------------


def test_restoring_clears_the_block_and_lets_a_rebuild_re_add_it(
    materialised: Entity, org_owner: User
) -> None:
    code = _an_added_code(materialised)

    with platform_scope(reason="test"):
        remove_definition(materialised, code, actor=org_owner, reason="Not applicable.")
        restore_definition(materialised, code, actor=org_owner, as_of=AS_OF)

        assert not ObligationSuppression.objects.filter(
            entity=materialised, definition_code=code, revoked_at__isnull=True
        ).exists()
        # The engine's own verdict decides from here — restoring only undoes the
        # block, it does not itself force the definition back on.
        rows = build_library(materialised, as_of=AS_OF)
        assert next(row for row in rows if row.code == code).state in {"added", "not_added"}


def test_restoring_something_not_removed_refuses(materialised: Entity, org_owner: User) -> None:
    code = _an_added_code(materialised)
    with platform_scope(reason="test"), pytest.raises(LibraryError):
        restore_definition(materialised, code, actor=org_owner, as_of=AS_OF)


# ---------------------------------------------------------------------------
# force_add_definition
# ---------------------------------------------------------------------------


def test_force_adding_an_already_added_definition_refuses(
    materialised: Entity, org_owner: User
) -> None:
    code = _an_added_code(materialised)
    with platform_scope(reason="test"), pytest.raises(LibraryError):
        force_add_definition(materialised, code, actor=org_owner, reason="x", as_of=AS_OF)


def test_force_adding_a_definition_missing_its_required_registration_refuses(
    org: Tenant,
) -> None:
    """An entity with no GST registration at all cannot force-add a
    REGISTRATION-scoped, GST-selected definition — the planner has nothing to
    fan out to, so nothing is left behind."""
    with platform_scope(reason="test"):
        entity = Entity.objects.create(
            tenant=org,
            name="No Registrations Pvt Ltd",
            entity_type="PVT_LTD",
            country="IN",
        )
        rows = build_library(entity, as_of=AS_OF)
        candidate = next(
            (row for row in rows if row.state == "not_added" and row.blocked_reason), None
        )
        if candidate is None:
            pytest.skip("no registration-scoped definition in this catalog to exercise")

        with pytest.raises(LibraryError):
            force_add_definition(
                entity, candidate.code, actor=None, reason="We file this anyway.", as_of=AS_OF
            )

        assert not ObligationInclusion.objects.filter(
            entity=entity, definition_code=candidate.code
        ).exists()


# ---------------------------------------------------------------------------
# HTTP: permissions and cross-tenant isolation
# ---------------------------------------------------------------------------


def test_library_picker_and_detail_render_both_ways(
    signed_in: Client, materialised: Entity
) -> None:
    picker = signed_in.get(reverse("compliance:library_picker"))
    detail = signed_in.get(reverse("compliance:library", args=[materialised.pk]))
    fragment = signed_in.get(
        reverse("compliance:library", args=[materialised.pk]), headers={"HX-Request": "true"}
    )

    assert picker.status_code == 200
    assert detail.status_code == 200
    assert fragment.status_code == 200
    assert b"<!doctype html>" not in fragment.content.lower()


def test_view_only_user_sees_rows_but_no_action_controls(
    client: Client, materialised: Entity, org: Tenant
) -> None:
    from stacos.accounts.middleware import SESSION_VERIFIED_KEY
    from stacos.tenancy.models import Membership, Role

    with platform_scope(reason="test"):
        role = Role.objects.create(
            tenant=org,
            code="library-viewer",
            name="Library viewer",
            tenant_type=Tenant.Type.ORGANISATION,
            permissions=["compliance.library.view"],
        )
        user = User.objects.create_user(
            email="viewer@example.com",
            password="test-password-12345",
            full_name="Viewer",
            phone_e164="+919800099001",
            email_verified=True,
            phone_verified=True,
        )
        Membership.objects.create(tenant=org, user=user, role=role, status=Membership.Status.ACTIVE)

    client.force_login(user)
    session = client.session
    session[SESSION_VERIFIED_KEY] = True
    session.save()

    response = client.get(reverse("compliance:library", args=[materialised.pk]))
    assert response.status_code == 200
    assert b"library_remove" not in response.content
    assert b"Remove</button>" not in response.content


def test_a_forged_entity_id_from_another_tenant_is_a_404(
    signed_in: Client, rival_entity: Entity
) -> None:
    response = signed_in.get(reverse("compliance:library", args=[rival_entity.pk]))
    assert response.status_code == 404
