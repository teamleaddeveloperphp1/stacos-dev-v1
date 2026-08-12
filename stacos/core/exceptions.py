"""Exceptions that carry architectural meaning."""

__all__ = [
    "CrossTenantWriteError",
    "PermissionDenied",
    "StacosError",
    "StepUpRequired",
    "UnscopedQueryError",
]


class StacosError(Exception):
    """Base class for STACOS domain errors."""


class UnscopedQueryError(StacosError):
    """A tenant-scoped model was queried with no :class:`AccessScope` bound.

    This is raised rather than silently returning every row, because a forgotten
    scope must fail loudly in development instead of leaking quietly in
    production. If you see this in a management command or a Celery task, wrap
    the work in ``tenant_context(...)``.
    """


class CrossTenantWriteError(StacosError):
    """An attempt to write a row belonging to a tenant outside the write scope.

    Read access via an engagement is frequently broader than write access — a
    practice may be able to see an entity's obligations while being scoped out of
    editing them — so reads and writes are checked separately.
    """


class PermissionDenied(StacosError):
    """The actor lacks a permission required by the view or service."""

    def __init__(self, permission: str, message: str = "") -> None:
        self.permission = permission
        super().__init__(message or f"Missing required permission: {permission}")


class StepUpRequired(StacosError):
    """A sensitive action needs fresh re-authentication before it can proceed."""

    def __init__(self, permission: str = "", next_url: str = "") -> None:
        self.permission = permission
        self.next_url = next_url
        super().__init__("Step-up authentication required")
