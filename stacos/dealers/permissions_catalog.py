"""Permissions over the channel programme.

Note what is not here: anything touching compliance data. A dealer sells
subscriptions and manages accounts. Seeing a client's obligations, documents or
notices requires an explicit, time-boxed, client-approved engagement, exactly
like a professional firm's access.
"""

from stacos.core.permissions import Permission, TenantType, permission_registry

CATEGORY = "Channel partners"

DEALER_OR_PLATFORM = frozenset({TenantType.DEALER, TenantType.PLATFORM})

VIEW = "dealers.commission.view"

permission_registry.register_many(
    [
        Permission(
            code=VIEW,
            label="View the commission ledger",
            category=CATEGORY,
            tenant_types=DEALER_OR_PLATFORM,
            description="A dealer's own accruals, line by line. Never another dealer's.",
        ),
        Permission(
            code="dealers.plan.manage",
            label="Set commission terms",
            category=CATEGORY,
            tenant_types=frozenset({TenantType.PLATFORM}),
            is_sensitive=True,
            description="Platform only. A dealer setting their own rate is not a channel programme.",
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="dealers.payout.approve",
            label="Approve a payout",
            category=CATEGORY,
            tenant_types=frozenset({TenantType.PLATFORM}),
            is_sensitive=True,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="dealers.payout.pay",
            label="Record a payout as paid",
            category=CATEGORY,
            tenant_types=frozenset({TenantType.PLATFORM}),
            is_sensitive=True,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="dealers.account.manage",
            label="Create and manage client accounts",
            category=CATEGORY,
            tenant_types=DEALER_OR_PLATFORM,
            description=(
                "Onboarding and subscription management only. Grants no sight of "
                "the client's compliance data — that needs an engagement."
            ),
        ),
    ]
)
