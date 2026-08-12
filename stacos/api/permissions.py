"""
STACOS permissions, expressed as DRF permission classes.

The web views use `@require_permission`, which wraps a function taking
``(request, ...)``. A DRF method takes ``(self, request, ...)``, so the same
decorator cannot be reused on one — and `manage.py check_view_permissions`
requires a DRF view to set ``permission_classes`` in its own class body anyway,
precisely so that "someone decided" is provable for both styles.

The permission codes and the scope they are checked against are identical. What
differs is only the plumbing.

**Step-up does not exist over the API.** On the web, a sensitive permission
demands fresh re-authentication before the action proceeds. There is no
equivalent flow in the mobile client yet, so a sensitive permission is refused
here with an explanation rather than quietly waved through — which is the one
outcome that would make the web-side step-up decorative.
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from stacos.core.permissions import permission_registry

__all__ = ["HasStacosPermission", "is_sensitive", "requires"]


class HasStacosPermission(BasePermission):
    """Base class. Subclasses set ``required``."""

    required: tuple[str, ...] = ()
    any_of: bool = False

    message = "You do not have permission to do that."

    def has_permission(self, request: Request, view: Any) -> bool:  # noqa: ARG002 - DRF signature
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False

        scope = getattr(request, "access_scope", None)
        if scope is None:
            self.message = "No tenant is selected for this request."
            return False

        satisfied = [code for code in self.required if scope.has_permission(code)]
        if not (satisfied if self.any_of else len(satisfied) == len(self.required)):
            return False

        # Only the codes actually being relied on are tested for sensitivity.
        # With `any_of`, a user who qualifies through an ordinary permission must
        # not be turned away because some *other* code in the list happens to be
        # sensitive — that would refuse a preparer for holding nothing.
        usable = [code for code in satisfied if not is_sensitive(code)]
        if not usable:
            self.message = (
                f"'{satisfied[0]}' needs re-authentication, which the mobile client "
                f"cannot yet perform. Use the web application for this action."
            )
            return False

        return True


def is_sensitive(code: str) -> bool:
    return code in permission_registry and permission_registry.get(code).is_sensitive


def requires(*codes: str, any_of: bool = False) -> type[HasStacosPermission]:
    """Build a permission class for these codes.

    >>> permission_classes = [requires("compliance.obligation.view")]

    A generated class rather than a parameterised instance because DRF stores
    *classes* on the view and instantiates them per request.
    """
    return type(
        "Requires_" + "_".join(code.replace(".", "_") for code in codes),
        (HasStacosPermission,),
        {"required": tuple(codes), "any_of": any_of},
    )
