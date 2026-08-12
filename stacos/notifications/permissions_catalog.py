"""Permissions over notifications.

Short, because a notification is addressed to one person: there is no "view
somebody else's notifications" permission, and adding one later would be a
decision worth arguing about rather than a gap to fill.

`notifications.preferences.manage` exists separately from viewing so that a
tenant can, if it wants, hold reminder settings centrally — a practice that has
been burned by a client turning their own reminders off has a real reason to.
"""

from stacos.core.permissions import Permission, permission_registry

CATEGORY = "Notifications"

VIEW = "notifications.view"

permission_registry.register_many(
    [
        Permission(
            code=VIEW,
            label="See your own notifications",
            category=CATEGORY,
            description="Everyone who can sign in has this. It grants nothing about other people.",
        ),
        Permission(
            code="notifications.preferences.manage",
            label="Change your notification preferences",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
    ]
)
