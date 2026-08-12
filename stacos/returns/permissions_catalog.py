"""Permissions over return preparation.

Four distinct people, four permissions. Maker, checker, client approver and
filer. Collapsing any two of them defeats the control the module exists for.
"""

from stacos.core.permissions import Permission, permission_registry

CATEGORY = "Return preparation"

VIEW = "returns.preparation.view"

permission_registry.register_many(
    [
        Permission(code=VIEW, label="View return working papers", category=CATEGORY),
        Permission(
            code="returns.preparation.prepare",
            label="Prepare a return",
            category=CATEGORY,
            description="The maker. Enters the figures and resolves reconciliation differences.",
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="returns.preparation.review",
            label="Review a prepared return",
            category=CATEGORY,
            description=(
                "The checker. Cannot be the same person who prepared it — enforced "
                "by a database constraint as well as by this permission."
            ),
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="returns.preparation.approve",
            label="Approve a return as the client",
            category=CATEGORY,
            is_sensitive=True,
            description="Client sign-off: where the business takes responsibility for the figures.",
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="returns.preparation.file",
            label="Record a filing",
            category=CATEGORY,
            is_sensitive=True,
            description="Records the acknowledgement number and moves the obligation with it.",
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="returns.reconciliation.run",
            label="Run and resolve reconciliations",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
    ]
)
