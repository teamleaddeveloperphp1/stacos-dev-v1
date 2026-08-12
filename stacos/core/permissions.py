"""
Permission registry and enforcement.

Permissions are **strings owned by a registry**, never role names. `if
user.role == "manager"` is how authorisation logic ends up duplicated in fifteen
places and wrong in three of them; `if scope.has_permission("return.approve")`
is checkable, greppable, and lets a customer build their own role bundles.

Django's model-level ``auth.Permission`` is too coarse here — STACOS needs
permissions scoped by entity, department and compliance category simultaneously —
so this is a parallel, explicit system.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar, cast

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest

from stacos.core.exceptions import PermissionDenied, StepUpRequired

__all__ = [
    "Permission",
    "PermissionRegistry",
    "RequirePermissionMixin",
    "permission_registry",
    "public_view",
    "require_permission",
]

F = TypeVar("F", bound=Callable[..., Any])

#: Attribute stamped onto every view. The CI check walks the URL resolver
#: looking for it; a view without it fails the build.
PERMISSION_ATTR = "stacos_permissions"
PUBLIC_ATTR = "stacos_public"


class TenantType:
    """Tenant kinds a permission can apply to. Mirrors ``tenancy.Tenant.Type``."""

    ORGANISATION = "ORGANISATION"
    PRACTICE = "PRACTICE"
    DEALER = "DEALER"
    PLATFORM = "PLATFORM"
    ALL: ClassVar[frozenset[str]] = frozenset({ORGANISATION, PRACTICE, DEALER, PLATFORM})


@dataclass(frozen=True, slots=True)
class Permission:
    """One capability.

    :param code: dotted, stable, and never renamed once shipped — custom roles
        stored by customers reference these strings.
    :param is_sensitive: requires fresh re-authentication (step-up). Return
        approval, role changes, payment method changes, data export and
        engagement grants all qualify.
    :param implies: codes automatically granted alongside this one, so a role
        bundle does not have to enumerate every read permission behind a write.
    """

    code: str
    label: str
    category: str
    tenant_types: frozenset[str] = field(default_factory=lambda: TenantType.ALL)
    is_sensitive: bool = False
    description: str = ""
    implies: frozenset[str] = field(default_factory=frozenset)


class PermissionRegistry:
    """Process-wide catalogue of permissions, populated at app-ready.

    Registering twice with different definitions is an error rather than a
    silent overwrite — two apps disagreeing about what ``document.download``
    means is exactly the bug this catches.
    """

    def __init__(self) -> None:
        self._permissions: dict[str, Permission] = {}

    def register(self, permission: Permission) -> Permission:
        existing = self._permissions.get(permission.code)
        if existing is not None and existing != permission:
            raise ImproperlyConfigured(
                f"Permission {permission.code!r} is already registered with a "
                f"different definition. Codes must be unique across apps."
            )
        self._permissions[permission.code] = permission
        return permission

    def register_many(self, permissions: Iterable[Permission]) -> None:
        for permission in permissions:
            self.register(permission)

    def get(self, code: str) -> Permission:
        try:
            return self._permissions[code]
        except KeyError:
            raise ImproperlyConfigured(
                f"Unknown permission {code!r}. Register it in your app's "
                f"permissions.py before referencing it."
            ) from None

    def __contains__(self, code: object) -> bool:
        return code in self._permissions

    def __iter__(self) -> Iterator[Permission]:
        return iter(sorted(self._permissions.values(), key=lambda p: p.code))

    def __len__(self) -> int:
        return len(self._permissions)

    def codes(self) -> frozenset[str]:
        return frozenset(self._permissions)

    def for_tenant_type(self, tenant_type: str) -> list[Permission]:
        return [p for p in self if tenant_type in p.tenant_types]

    def sensitive_codes(self) -> frozenset[str]:
        return frozenset(p.code for p in self if p.is_sensitive)

    def expand(self, codes: Iterable[str]) -> frozenset[str]:
        """Close a set of codes over ``implies``."""
        resolved: set[str] = set()
        pending = list(codes)
        while pending:
            code = pending.pop()
            if code in resolved:
                continue
            resolved.add(code)
            permission = self._permissions.get(code)
            if permission is not None:
                pending.extend(permission.implies)
        return frozenset(resolved)

    def clear(self) -> None:  # pragma: no cover - test helper
        self._permissions.clear()


permission_registry = PermissionRegistry()


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


def require_permission(*codes: str, any_of: bool = False) -> Callable[[F], F]:
    """Declare and enforce the permissions a view requires.

    ``manage.py check_view_permissions`` walks the URL resolver and fails the
    build for any application view lacking this declaration.

    HTMX fragment endpoints are ordinary views with directly reachable URLs, so
    they carry the same decorator as the page that loads them. "It is only
    fetched from an authenticated page" is how HTMX applications leak data.

    :param any_of: require any one of the codes rather than all of them.
    """

    def decorator(view: F) -> F:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
            check_permissions(request, codes, any_of=any_of)
            return view(request, *args, **kwargs)

        # These attributes are what `manage.py check_view_permissions` reads
        # back off the resolved callback.
        setattr(wrapper, PERMISSION_ATTR, tuple(codes))
        setattr(wrapper, "stacos_permissions_any_of", any_of)  # noqa: B010
        return cast("F", wrapper)

    return decorator


def public_view[ViewT: Callable[..., Any]](view: ViewT) -> ViewT:
    """Mark a view as intentionally unauthenticated.

    Marketing pages, health checks, the sign-in screen. Explicit, so that "no
    permission declared" always means "someone forgot" and never means "this one
    is fine".
    """
    setattr(view, PUBLIC_ATTR, True)
    setattr(view, PERMISSION_ATTR, ())
    return view


def check_permissions(
    request: HttpRequest,
    codes: Iterable[str],
    *,
    any_of: bool = False,
) -> None:
    """Raise unless the request's scope satisfies ``codes``."""
    codes = tuple(codes)
    if not codes:
        return

    scope = getattr(request, "access_scope", None)
    if scope is None:
        raise PermissionDenied(codes[0], "No access scope is bound to this request.")

    if any_of:
        satisfied = any(scope.has_permission(c) for c in codes)
        missing = codes[0]
    else:
        missing_codes = [c for c in codes if not scope.has_permission(c)]
        satisfied = not missing_codes
        missing = missing_codes[0] if missing_codes else codes[0]

    if not satisfied:
        raise PermissionDenied(missing)

    _enforce_step_up(request, codes)


def _enforce_step_up(request: HttpRequest, codes: Iterable[str]) -> None:
    """Demand fresh re-authentication for sensitive permissions."""
    sensitive = [
        c for c in codes if c in permission_registry and permission_registry.get(c).is_sensitive
    ]
    if not sensitive:
        return

    from stacos.accounts.stepup import step_up_is_fresh

    if not step_up_is_fresh(request):
        raise StepUpRequired(permission=sensitive[0], next_url=request.get_full_path())


class RequirePermissionMixin:
    """Class-based-view equivalent of :func:`require_permission`.

    Subclasses set ``required_permissions``. ``as_view()`` stamps the same
    attribute the function decorator does, so the CI check sees both the same
    way — which is why that check walks the resolver instead of grepping source.
    """

    required_permissions: ClassVar[tuple[str, ...]] = ()
    require_any_permission: ClassVar[bool] = False

    @classmethod
    def as_view(cls, **initkwargs: Any) -> Callable[..., Any]:
        view = super().as_view(**initkwargs)  # type: ignore[misc]
        # Stamped identically to the function decorator, so the CI check sees
        # both the same way — which is why that check walks the URL resolver
        # rather than grepping source.
        setattr(view, PERMISSION_ATTR, tuple(cls.required_permissions))
        setattr(view, "stacos_permissions_any_of", cls.require_any_permission)  # noqa: B010
        return cast("Callable[..., Any]", view)

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        check_permissions(
            request,
            self.required_permissions,
            any_of=self.require_any_permission,
        )
        return super().dispatch(request, *args, **kwargs)  # type: ignore[misc]
