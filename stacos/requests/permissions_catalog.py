"""Permissions over information requests.

Split between raising a request and answering one, because those are two
different people: the practice asks, the client answers. A single "manage
requests" permission would let a client close their own outstanding items.
"""

from stacos.core.permissions import Permission, permission_registry

CATEGORY = "Information requests"

VIEW = "rfi.request.view"

permission_registry.register_many(
    [
        Permission(code=VIEW, label="View information requests", category=CATEGORY),
        Permission(
            code="rfi.request.create",
            label="Raise an information request",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="rfi.request.send",
            label="Send a request to the client",
            category=CATEGORY,
            description="Sends a message on the client's channel, so it is separated from drafting.",
            implies=frozenset({VIEW, "rfi.request.create"}),
        ),
        Permission(
            code="rfi.request.respond",
            label="Answer an information request",
            category=CATEGORY,
            description="The client side. Held by organisation users, not by the practice.",
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="rfi.request.review",
            label="Accept or reject a response",
            category=CATEGORY,
            description=(
                "Rejecting sends the client back with a reason. Separated from "
                "answering so nobody can approve their own submission."
            ),
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="rfi.request.close",
            label="Close an information request",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
    ]
)
