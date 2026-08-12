"""Permissions over the notice tracker.

Notices carry the sharpest deadlines and the largest numbers in the product, so
recording a response is sensitive: it is the point at which somebody asserts to
the platform that the authority has been answered.
"""

from stacos.core.permissions import Permission, permission_registry

CATEGORY = "Notices"

VIEW = "notices.notice.view"

permission_registry.register_many(
    [
        Permission(code=VIEW, label="View notices", category=CATEGORY),
        Permission(
            code="notices.notice.create",
            label="Record a notice",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="notices.notice.edit",
            label="Edit a notice",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="notices.notice.assign",
            label="Assign a notice",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="notices.notice.respond",
            label="Record a response to a notice",
            category=CATEGORY,
            is_sensitive=True,
            description=(
                "Asserts that the authority has been answered, with a reference. "
                "The record an appeal is later built on."
            ),
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="notices.notice.close",
            label="Close a notice",
            category=CATEGORY,
            is_sensitive=True,
            implies=frozenset({VIEW}),
        ),
    ]
)
