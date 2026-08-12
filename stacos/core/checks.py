"""
System checks that keep the architectural conventions honest.

These run as part of ``manage.py check``, so they fail the build in CI rather
than being enforced by code review, which does not scale past the third month.

Two checks, both of which exist because the failure they catch is invisible:

* ``stacos.E001`` — an application view with no permission declaration. A view
  that forgets ``@require_permission`` does not misbehave; it silently serves
  data to anyone who can reach the URL.
* ``stacos.E002`` — a model that neither declares tenancy nor is listed as
  deliberately global. Adding a model without *deciding* its tenancy is the
  origin of most cross-tenant leaks.

The view check walks Django's **URL resolver**, not the source tree. Grepping for
decorators misses permissions applied at ``as_view()`` time and produces false
positives for helper functions that merely look like views; the resolver sees
exactly what is routable.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from django.apps import apps
from django.core.checks import CheckMessage, Error, register
from django.urls import URLPattern, URLResolver, get_resolver

from stacos.core.permissions import PERMISSION_ATTR, PUBLIC_ATTR, permission_registry

__all__ = ["check_model_tenancy", "check_view_permissions", "iter_app_views"]

#: Views under these module prefixes are ours and must declare a permission.
OWNED_MODULE_PREFIX = "stacos."

#: Our own views that are legitimately public. Marketing pages use
#: ``@public_view`` instead; this list is for views where the decorator cannot be
#: applied, and every entry needs a reason.
PUBLIC_VIEW_ALLOWLIST: frozenset[str] = frozenset(
    {
        "stacos.core.views.healthz",
    }
)

#: Models that are deliberately not tenant-scoped, with the reason each is safe.
GLOBAL_MODEL_ALLOWLIST: dict[str, str] = {
    # Identity is global by design: one user, one login, many tenants.
    "accounts.User": "Global identity, deliberately not owned by a tenant.",
    "accounts.PendingVerification": "Pre-authentication; no tenant exists yet.",
    "accounts.TrustedDevice": "Belongs to a user across all their tenants.",
    "accounts.UserSession": "Belongs to a user across all their tenants.",
    "accounts.MessageSpendLedger": "Platform-level cost control.",
    # The tenant table cannot be scoped by itself.
    "tenancy.Tenant": "Is the tenant.",
    "tenancy.Role": "System roles are global; tenant roles carry a nullable tenant FK.",
    # Audit rows exist for pre-auth and platform events with no tenant.
    "core.AuditLog": "Nullable tenant; covers pre-authentication and platform events.",
    "core.TaskRun": "Infrastructure bookkeeping, not customer data.",
    # The catalog is platform-owned reference data, readable by every tenant.
    "jurisdictions.JurisdictionPack": "Platform-owned reference data.",
    "jurisdictions.HolidayCalendar": "Platform-owned reference data.",
    "jurisdictions.Holiday": "Platform-owned reference data.",
    "jurisdictions.Authority": "Platform-owned reference data.",
    "jurisdictions.FactDefinition": "Platform-owned reference data.",
    "jurisdictions.WeekendRule": "Platform-owned reference data.",
    # An invitation frequently predates the receiving tenant existing at all, so
    # it cannot carry that tenant's id. Access is controlled by a single-use
    # hashed token and an expiry instead.
    "engagements.EngagementInvitation": (
        "Cross-tenant by nature; the receiving tenant often does not exist yet."
    ),
}


def iter_app_views() -> Iterator[tuple[str, Any]]:
    """Yield ``(route, callback)`` for every routable view in the project."""
    yield from _walk(get_resolver(), "")


def _walk(resolver: URLResolver, prefix: str) -> Iterator[tuple[str, Any]]:
    for pattern in resolver.url_patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from _walk(pattern, route)
        elif isinstance(pattern, URLPattern) and pattern.callback is not None:
            yield route, pattern.callback


@register()
def check_view_permissions(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Every routable STACOS view must declare its required permissions."""
    errors: list[CheckMessage] = []
    seen: set[str] = set()

    for route, callback in iter_app_views():
        module = getattr(callback, "__module__", "") or ""
        if not module.startswith(OWNED_MODULE_PREFIX):
            continue

        # Django REST Framework attaches the class as `.cls`; its `as_view()`
        # wrapper has an unhelpful qualname of `View.as_view.<locals>.view`.
        drf_class = getattr(callback, "cls", None)
        if drf_class is not None:
            dotted = f"{drf_class.__module__}.{drf_class.__name__}"
        else:
            dotted = f"{module}.{getattr(callback, '__qualname__', callback.__class__.__name__)}"

        if dotted in seen:
            continue
        seen.add(dotted)

        if dotted in PUBLIC_VIEW_ALLOWLIST or getattr(callback, PUBLIC_ATTR, False):
            continue

        if drf_class is not None:
            # DRF has its own authorisation mechanism. Requiring the class to set
            # `permission_classes` in its own body — not merely inherit the
            # project default — keeps the same property: someone decided.
            if "permission_classes" not in drf_class.__dict__:
                errors.append(
                    Error(
                        f"DRF view {dotted} (route {route!r}) does not set permission_classes.",
                        hint=(
                            "Declare permission_classes explicitly on the class. "
                            "Inheriting the project default is not a decision."
                        ),
                        id="stacos.E001",
                        obj=dotted,
                    )
                )
            continue

        declared = getattr(callback, PERMISSION_ATTR, None)
        if declared is None:
            errors.append(
                Error(
                    f"View {dotted} (route {route!r}) declares no permission.",
                    hint=(
                        "Add @require_permission('some.permission.code'), or "
                        "@public_view if it is intentionally unauthenticated. "
                        "HTMX fragment endpoints are directly reachable URLs and "
                        "need the same decorator as the page that loads them."
                    ),
                    id="stacos.E001",
                    obj=dotted,
                )
            )
            continue

        unknown = [code for code in declared if code not in permission_registry]
        if unknown:
            errors.append(
                Error(
                    f"View {dotted} requires unregistered permission(s): {', '.join(unknown)}.",
                    hint="Register the permission in your app's permissions.py.",
                    id="stacos.E003",
                    obj=dotted,
                )
            )

    return errors


@register()
def check_model_tenancy(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Every STACOS model must be tenant-scoped or explicitly declared global."""
    from stacos.core.models import TenantScopedModel

    errors: list[CheckMessage] = []

    for model in apps.get_models():
        module = model.__module__ or ""
        if not module.startswith(OWNED_MODULE_PREFIX):
            continue
        label = model._meta.label
        if issubclass(model, TenantScopedModel) or label in GLOBAL_MODEL_ALLOWLIST:
            continue
        errors.append(
            Error(
                f"Model {label} declares no tenancy.",
                hint=(
                    "Inherit stacos.core.models.TenantScopedModel, or add the model "
                    "to GLOBAL_MODEL_ALLOWLIST in stacos/core/checks.py with a "
                    "reason it is safe to share across tenants."
                ),
                id="stacos.E002",
                obj=label,
            )
        )

    return errors
