"""Permissions for identity and user administration."""

from stacos.core.permissions import Permission, permission_registry

CATEGORY = "Users and access"

permission_registry.register_many(
    [
        Permission(
            code="accounts.user.view",
            label="View users",
            category=CATEGORY,
        ),
        Permission(
            code="accounts.user.invite",
            label="Invite users",
            category=CATEGORY,
            implies=frozenset({"accounts.user.view"}),
        ),
        Permission(
            code="accounts.user.role.change",
            label="Change a user's role",
            category=CATEGORY,
            is_sensitive=True,
            description=(
                "Requires step-up authentication, and signs the affected user out "
                "of every device immediately."
            ),
            implies=frozenset({"accounts.user.view"}),
        ),
        Permission(
            code="accounts.user.deactivate",
            label="Deactivate a user",
            category=CATEGORY,
            is_sensitive=True,
            implies=frozenset({"accounts.user.view"}),
        ),
        Permission(
            code="accounts.role.manage",
            label="Create and edit custom roles",
            category=CATEGORY,
            is_sensitive=True,
        ),
        Permission(
            code="accounts.security.manage",
            label="Manage your own devices and sessions",
            category=CATEGORY,
            description=(
                "Held by every signed-in user. Governs the security settings "
                "screen, which lists trusted devices and active sessions and "
                "can end them."
            ),
        ),
    ]
)
