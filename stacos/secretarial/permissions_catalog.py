"""Permissions over the secretarial records.

The cap table is separated from everything else. Shareholding is the most
commercially sensitive information a private company holds, and a compliance
manager who legitimately maintains the minute book has no business seeing who
owns what.
"""

from stacos.core.permissions import Permission, permission_registry

CATEGORY = "Corporate secretarial"

VIEW = "secretarial.view"

permission_registry.register_many(
    [
        Permission(code=VIEW, label="View secretarial records", category=CATEGORY),
        Permission(
            code="secretarial.meeting.manage",
            label="Schedule and record meetings",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="secretarial.minutes.sign",
            label="Sign off minutes",
            category=CATEGORY,
            is_sensitive=True,
            description=(
                "Entering minutes in the minute book is what turns a diary entry "
                "into evidence a regulator will read."
            ),
            implies=frozenset({VIEW, "secretarial.meeting.manage"}),
        ),
        Permission(
            code="secretarial.resolution.manage",
            label="Record resolutions",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="secretarial.register.manage",
            label="Maintain statutory registers",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="secretarial.captable.view",
            label="View the cap table",
            category=CATEGORY,
            description=(
                "Separated from the rest deliberately. Who owns what is the most "
                "commercially sensitive thing a private company holds."
            ),
        ),
        Permission(
            code="secretarial.captable.manage",
            label="Record share transactions",
            category=CATEGORY,
            is_sensitive=True,
            implies=frozenset({VIEW, "secretarial.captable.view"}),
        ),
    ]
)
