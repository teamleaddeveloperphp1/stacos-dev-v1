"""Permissions governing cross-tenant access."""

from stacos.core.permissions import Permission, permission_registry

CATEGORY = "Engagements"

permission_registry.register_many(
    [
        Permission(
            code="engagements.view",
            label="View engagements",
            category=CATEGORY,
        ),
        Permission(
            code="engagements.invite",
            label="Invite a professional firm or a client",
            category=CATEGORY,
            implies=frozenset({"engagements.view"}),
        ),
        Permission(
            code="engagements.grant",
            label="Approve an engagement and grant access",
            category=CATEGORY,
            is_sensitive=True,
            description=(
                "Granting access hands another organisation sight of this "
                "entity's records, so it requires step-up authentication."
            ),
            implies=frozenset({"engagements.view"}),
        ),
        Permission(
            code="engagements.revoke",
            label="Suspend or end an engagement",
            category=CATEGORY,
            is_sensitive=True,
            implies=frozenset({"engagements.view"}),
        ),
        Permission(
            code="engagements.scope.edit",
            label="Change what an engagement covers",
            category=CATEGORY,
            is_sensitive=True,
            implies=frozenset({"engagements.view"}),
        ),
    ]
)
