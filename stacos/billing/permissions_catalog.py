"""Permissions over billing.

Everything that moves money is sensitive. A stolen session should not be able to
void an invoice or record a payment that never arrived.
"""

from stacos.core.permissions import Permission, permission_registry

CATEGORY = "Billing"

VIEW = "billing.view"

permission_registry.register_many(
    [
        Permission(
            code=VIEW,
            label="View subscription and invoices",
            category=CATEGORY,
            description="A customer seeing their own billing. Granted to owners, not to staff.",
        ),
        Permission(
            code="billing.subscription.manage",
            label="Change the plan",
            category=CATEGORY,
            is_sensitive=True,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="billing.invoice.issue",
            label="Issue an invoice",
            category=CATEGORY,
            is_sensitive=True,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="billing.invoice.void",
            label="Void an invoice",
            category=CATEGORY,
            is_sensitive=True,
            description="A voided invoice number has to stay explainable, so a reason is required.",
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="billing.payment.record",
            label="Record a payment",
            category=CATEGORY,
            is_sensitive=True,
            description=(
                "Offline collection — cheque and transfer — is reconciled by hand, "
                "and that is somebody asserting money arrived."
            ),
            implies=frozenset({VIEW}),
        ),
    ]
)
