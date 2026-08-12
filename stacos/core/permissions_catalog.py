"""Cross-cutting permissions owned by ``core``."""

from stacos.core.permissions import Permission, TenantType, permission_registry

CATEGORY = "Platform"

permission_registry.register_many(
    [
        Permission(
            code="core.audit.view",
            label="View audit trail",
            category=CATEGORY,
            description="Read the immutable activity log for the tenant.",
        ),
        Permission(
            code="core.audit.export",
            label="Export audit trail",
            category=CATEGORY,
            is_sensitive=True,
            description=(
                "Download the full audit log. Sensitive because a complete export "
                "is the single most valuable artefact an attacker could take."
            ),
            implies=frozenset({"core.audit.view"}),
        ),
        Permission(
            code="core.data.export",
            label="Export tenant data",
            category=CATEGORY,
            is_sensitive=True,
            description="Bulk export of tenant records. Requires step-up authentication.",
        ),
        Permission(
            code="core.search",
            label="Use search and the command palette",
            category=CATEGORY,
        ),
        Permission(
            code="finance.view",
            label="View financial figures",
            category=CATEGORY,
            description=(
                "Without this, obligation titles and due dates are visible but "
                "amounts, tax values and bank details are masked."
            ),
        ),
        Permission(
            code="platform.impersonate",
            label="Impersonate a user for support",
            category=CATEGORY,
            tenant_types=frozenset({TenantType.PLATFORM}),
            is_sensitive=True,
            description=(
                "Consent-gated, time-boxed, and visible to the user being "
                "supported. There is no silent impersonation."
            ),
        ),
        Permission(
            code="platform.tenant.view",
            label="View tenant health",
            category=CATEGORY,
            tenant_types=frozenset({TenantType.PLATFORM}),
        ),
    ]
)
