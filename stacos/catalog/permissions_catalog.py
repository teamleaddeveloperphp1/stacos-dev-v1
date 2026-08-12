"""Permissions over the global compliance catalog.

The catalog is platform-owned reference data. Tenants read it — the detail page
of an obligation shows what the law says and where it says it — but only the
platform edits it, and publishing is the single most dangerous action in the
product: a bad rule reaching ten thousand tenants at once is the platform's worst
case. So publication is sensitive, and it is separate from authoring.
"""

from stacos.core.permissions import Permission, TenantType, permission_registry

CATEGORY = "Compliance catalog"

permission_registry.register_many(
    [
        Permission(
            code="catalog.view",
            label="View the compliance catalog",
            category=CATEGORY,
            description=(
                "Read the statutory reference, plain-language summary and due-date "
                "rule behind an obligation. Granted broadly: a client asking 'why do "
                "I have to file this' deserves an answer."
            ),
        ),
        Permission(
            code="catalog.edit",
            label="Author catalog definitions",
            category=CATEGORY,
            tenant_types=frozenset({TenantType.PLATFORM}),
            implies=frozenset({"catalog.view"}),
        ),
        Permission(
            code="catalog.publish",
            label="Publish a catalog version",
            category=CATEGORY,
            tenant_types=frozenset({TenantType.PLATFORM}),
            is_sensitive=True,
            description=(
                "Publication changes what every tenant's calendar will contain on the "
                "next rebuild. Step-up authentication, and a staged rollout for "
                "anything that would supersede existing instances."
            ),
            implies=frozenset({"catalog.view", "catalog.edit"}),
        ),
        Permission(
            code="catalog.extension.publish",
            label="Publish a government extension",
            category=CATEGORY,
            tenant_types=frozenset({TenantType.PLATFORM}),
            is_sensitive=True,
            description=(
                "Moves a due date for every affected tenant at once. Getting the kind "
                "wrong — a waiver entered as an extension — tells clients they have "
                "longer than they do."
            ),
            implies=frozenset({"catalog.view"}),
        ),
    ]
)
