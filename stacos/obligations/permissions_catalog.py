"""
Permissions over the obligation register.

Split along the workflow rather than by read/write, because that is how firms
actually delegate: an article clerk prepares, a manager reviews, a partner
approves for filing, and the client signs off. A single "edit obligations"
permission cannot express any of that, and every customer asks for it in the
first month.

``approve`` and ``file`` are sensitive: they are the two points where somebody
takes responsibility for a statutory filing, and a stolen session should not be
able to reach them without fresh authentication.
"""

from stacos.core.permissions import Permission, permission_registry

CATEGORY = "Compliance"

VIEW = "compliance.obligation.view"

permission_registry.register_many(
    [
        Permission(
            code=VIEW,
            label="View the compliance calendar",
            category=CATEGORY,
        ),
        Permission(
            code="compliance.obligation.prepare",
            label="Prepare a filing",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.obligation.request_info",
            label="Request information from the client",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.obligation.review",
            label="Review a prepared filing",
            category=CATEGORY,
            description="Send back for changes, or approve for client sign-off.",
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.obligation.approve",
            label="Approve a filing as the client",
            category=CATEGORY,
            is_sensitive=True,
            description=(
                "Client-side sign-off on a return before it is filed. The point at "
                "which the business takes responsibility for what is submitted."
            ),
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.obligation.file",
            label="Record a filing",
            category=CATEGORY,
            is_sensitive=True,
            description=(
                "Records that a statutory return was submitted, with its "
                "acknowledgement number. This is the row an assessment is defended "
                "with."
            ),
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.obligation.close",
            label="Close an obligation",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.obligation.reopen",
            label="Reopen a closed obligation",
            category=CATEGORY,
            is_sensitive=True,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.obligation.defer",
            label="Defer an obligation",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.obligation.dismiss",
            label="Mark an obligation not applicable",
            category=CATEGORY,
            description=(
                "Overrides the engine. Recorded as a suppression so the nightly "
                "rebuild does not bring it back — which is why it needs a reason."
            ),
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.obligation.dispute",
            label="Mark an obligation disputed",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.obligation.assign",
            label="Assign an obligation",
            category=CATEGORY,
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.calendar.rebuild",
            label="Rebuild the compliance calendar",
            category=CATEGORY,
            description=(
                "Re-runs materialisation for an entity. Safe by construction — the "
                "planner never destroys an obligation with history — but it can add "
                "a lot of rows at once, so it is not granted to everyone."
            ),
            implies=frozenset({VIEW}),
        ),
        Permission(
            code="compliance.event.withdraw",
            label="Withdraw a recorded event",
            category=CATEGORY,
            description=(
                "Takes filings back off a client's calendar. Recording an event is "
                "routine data entry a junior does; withdrawing one removes "
                "obligations somebody may already have started work on, which is a "
                "different decision."
            ),
            implies=frozenset({"compliance.event.record"}),
        ),
        Permission(
            code="compliance.event.record",
            label="Record an entity event",
            category=CATEGORY,
            description=(
                "AGM dates, board meeting dates, licence issue dates. These unblock "
                "due dates that cannot otherwise be computed."
            ),
            implies=frozenset({VIEW}),
        ),
    ]
)
