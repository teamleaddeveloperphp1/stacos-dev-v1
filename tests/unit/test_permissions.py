"""The permission registry and its enforcement helpers."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from stacos.core.permissions import (
    PERMISSION_ATTR,
    Permission,
    PermissionRegistry,
    permission_registry,
    public_view,
    require_permission,
)
from stacos.core.scope import AccessScope


def test_registering_the_same_code_twice_differently_is_an_error() -> None:
    """Two apps disagreeing about what a permission means is exactly the bug
    this catches — silently overwriting would leave which definition wins up to
    import order."""
    registry = PermissionRegistry()
    registry.register(Permission(code="x.y", label="One", category="Test"))
    registry.register(Permission(code="x.y", label="One", category="Test"))  # identical: fine

    with pytest.raises(ImproperlyConfigured, match="already registered"):
        registry.register(Permission(code="x.y", label="Different", category="Test"))


def test_unknown_permission_lookup_explains_itself() -> None:
    registry = PermissionRegistry()
    with pytest.raises(ImproperlyConfigured, match="Unknown permission"):
        registry.get("nope.nope")


def test_implies_is_closed_transitively() -> None:
    """A role bundle should not have to enumerate every read permission behind a
    write one."""
    registry = PermissionRegistry()
    registry.register(Permission(code="a.view", label="A", category="T"))
    registry.register(
        Permission(code="a.edit", label="B", category="T", implies=frozenset({"a.view"}))
    )
    registry.register(
        Permission(code="a.admin", label="C", category="T", implies=frozenset({"a.edit"}))
    )

    assert registry.expand(["a.admin"]) == {"a.admin", "a.edit", "a.view"}


def test_expand_tolerates_unknown_codes() -> None:
    """A role stored by a customer may reference a permission a later release
    removed. Dropping the whole role would be worse than ignoring the code."""
    registry = PermissionRegistry()
    registry.register(Permission(code="a.view", label="A", category="T"))
    assert registry.expand(["a.view", "gone.away"]) == {"a.view", "gone.away"}


def test_sensitive_permissions_are_discoverable() -> None:
    sensitive = permission_registry.sensitive_codes()
    assert "engagements.grant" in sensitive
    assert "core.data.export" in sensitive
    assert "tenancy.entity.view" not in sensitive


def test_require_permission_stamps_the_view() -> None:
    """The CI check reads this attribute through the URL resolver, so it has to
    survive decoration."""

    @require_permission("tenancy.entity.view")
    def view(request):
        return "ok"

    assert getattr(view, PERMISSION_ATTR) == ("tenancy.entity.view",)


def test_public_view_marks_intent_explicitly() -> None:
    """ "No permission declared" must always mean "someone forgot" — never "this
    one is fine"."""

    @public_view
    def view(request):
        return "ok"

    assert view.stacos_public is True


def test_scope_permission_checks() -> None:
    scope = AccessScope(permissions=frozenset({"a.view"}), reason="test")
    assert scope.has_permission("a.view")
    assert not scope.has_permission("a.edit")


def test_platform_scope_satisfies_every_permission() -> None:
    scope = AccessScope(reason="platform:test", bypass=True)
    assert scope.has_permission("anything.at.all")


def test_every_registered_permission_has_a_category_and_label() -> None:
    """These strings appear in the role editor a customer uses, so a blank one is
    a user-facing defect rather than a cosmetic one."""
    for permission in permission_registry:
        assert permission.label, f"{permission.code} has no label"
        assert permission.category, f"{permission.code} has no category"
