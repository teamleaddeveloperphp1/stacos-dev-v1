"""Permissions over the practice's own work board and time.

All practice-side. An organisation user never holds these — the work board is
the firm's internal view of the work, and a client seeing its own file's
estimate, staff assignment and margin would be a commercial problem.
"""

from stacos.core.permissions import Permission, TenantType, permission_registry

CATEGORY = "Practice management"

PRACTICE_ONLY = frozenset({TenantType.PRACTICE, TenantType.PLATFORM})

VIEW = "practice.work.view"

permission_registry.register_many(
    [
        Permission(
            code=VIEW,
            label="View the work board",
            category=CATEGORY,
            tenant_types=PRACTICE_ONLY,
        ),
        Permission(
            code="practice.work.manage",
            label="Create and assign work",
            category=CATEGORY,
            tenant_types=PRACTICE_ONLY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="practice.time.log",
            label="Log time",
            category=CATEGORY,
            tenant_types=PRACTICE_ONLY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="practice.time.view_all",
            label="See everybody's time",
            category=CATEGORY,
            tenant_types=PRACTICE_ONLY,
            description=(
                "Without this a user sees only their own entries. Utilisation is a "
                "management view, not a peer-comparison tool."
            ),
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="practice.rates.manage",
            label="Set charge-out rates",
            category=CATEGORY,
            tenant_types=PRACTICE_ONLY,
            is_sensitive=True,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="practice.wip.view",
            label="See work in progress and profitability",
            category=CATEGORY,
            tenant_types=PRACTICE_ONLY,
            description="What the work was worth, and what it cost. Partner-level.",
            implies=frozenset({VIEW}),
        ),
    ]
)
