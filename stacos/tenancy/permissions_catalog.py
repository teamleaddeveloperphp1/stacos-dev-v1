"""Permissions for tenants, entities and their profiles."""

from stacos.core.permissions import Permission, TenantType, permission_registry

CATEGORY = "Entities and organisation"

permission_registry.register_many(
    [
        Permission(
            code="tenancy.onboarding.start",
            label="Set up an organisation",
            category=CATEGORY,
            tenant_types=frozenset({TenantType.ORGANISATION}),
            description=(
                "Held by an authenticated user who is not yet a member of anything, "
                "and by an owner adding a second organisation. Granted by the scope "
                "resolver in the no-membership case — the one place in the product "
                "where access is decided without a tenant, and deliberately the only "
                "permission it hands out."
            ),
        ),
        Permission(
            code="tenancy.tenant.view",
            label="View organisation settings",
            category=CATEGORY,
        ),
        Permission(
            code="tenancy.tenant.manage",
            label="Manage organisation settings",
            category=CATEGORY,
            is_sensitive=True,
            implies=frozenset({"tenancy.tenant.view"}),
        ),
        Permission(
            code="tenancy.entity.view",
            label="View entities",
            category=CATEGORY,
        ),
        Permission(
            code="tenancy.entity.create",
            label="Add an entity",
            category=CATEGORY,
            tenant_types=frozenset({TenantType.ORGANISATION}),
            implies=frozenset({"tenancy.entity.view"}),
        ),
        Permission(
            code="tenancy.entity.edit",
            label="Edit an entity",
            category=CATEGORY,
            implies=frozenset({"tenancy.entity.view"}),
        ),
        Permission(
            code="tenancy.entity.archive",
            label="Archive an entity",
            category=CATEGORY,
            is_sensitive=True,
            implies=frozenset({"tenancy.entity.view"}),
        ),
        Permission(
            code="tenancy.profile.view",
            label="View the compliance profile",
            category=CATEGORY,
        ),
        Permission(
            code="tenancy.profile.edit",
            label="Edit the compliance profile",
            category=CATEGORY,
            description=(
                "Changing the profile changes which obligations apply, so an edit "
                "produces a reviewable plan rather than taking effect silently."
            ),
            implies=frozenset({"tenancy.profile.view"}),
        ),
        Permission(
            code="tenancy.registration.view",
            label="View registrations and tax identifiers",
            category=CATEGORY,
        ),
        Permission(
            code="tenancy.registration.manage",
            label="Manage registrations and tax identifiers",
            category=CATEGORY,
            is_sensitive=True,
            implies=frozenset({"tenancy.registration.view"}),
        ),
        Permission(
            code="tenancy.premises.manage",
            label="Manage premises",
            category=CATEGORY,
        ),
    ]
)
