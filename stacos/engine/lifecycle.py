"""
The obligation lifecycle: states, the transitions between them, and the two
statuses that are derived rather than stored.

An explicit transition table rather than a state-machine library. Two reasons,
and both are structural rather than aesthetic. ``django-fsm`` is ORM-coupled, so
it could not live in a pure-Python engine at all; and the table has to be
serialisable to the front end, because the UI asks "what can I do to this
obligation" and expects an answer already filtered by the user's permissions.
Seventy lines of data beats a dependency that can do neither.

**Two states are deliberately absent from the stored vocabulary.**

``OVERDUE`` is derived. It cannot be a PostgreSQL generated column — those
require an ``IMMUTABLE`` expression and ``CURRENT_DATE`` is only ``STABLE`` — and
a nightly-stamped boolean is wrong for up to twenty-four hours, which generates
every "why does this still say on track" support ticket. So it is computed, here
in Python and identically in SQL, with a parity test across a fixture matrix
holding the two implementations together.

``FILED_LATE`` is a *fact* (``filed_on > due_date``), not a workflow position. As
a stored state it collides with ``CLOSED`` and doubles the size of this table.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

__all__ = [
    "CLOSED_STATES",
    "DisplayStatus",
    "OPEN_STATES",
    "State",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "Transition",
    "allowed_transitions",
    "derive_display_status",
    "filed_late",
    "is_overdue",
    "transition_for",
]


class State(StrEnum):
    """Where an obligation sits in its workflow.

    The order is the happy path, and it is worth reading as a sentence: nothing
    started, we asked the client for information, we prepared it, someone
    reviewed it, the client approved it, it is ready, it is filed, it is closed.
    """

    NOT_STARTED = "NOT_STARTED"
    INFO_REQUESTED = "INFO_REQUESTED"
    IN_PREPARATION = "IN_PREPARATION"
    PENDING_REVIEW = "PENDING_REVIEW"
    PENDING_CLIENT_APPROVAL = "PENDING_CLIENT_APPROVAL"
    READY_TO_FILE = "READY_TO_FILE"
    FILED = "FILED"
    CLOSED = "CLOSED"

    # -- Off the happy path -------------------------------------------------
    #: Determined not to apply — either by the engine, or by a person who knows
    #: something the profile does not say.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: Consciously postponed. Distinct from NOT_STARTED because somebody decided.
    DEFERRED = "DEFERRED"
    #: Under dispute with the authority. The date stops meaning what it meant.
    DISPUTED = "DISPUTED"


#: Finished. Dates are frozen here: a retroactive extension must never turn a
#: filing that was late into one that was on time, or the record stops being
#: evidence of anything.
TERMINAL_STATES: frozenset[str] = frozenset({State.FILED, State.CLOSED})

#: Resolved, but not by being done. Excluded from "what do I owe" counts.
CLOSED_STATES: frozenset[str] = TERMINAL_STATES | {State.NOT_APPLICABLE}

#: Still owed. The partial index behind the overdue query covers exactly these.
OPEN_STATES: frozenset[str] = frozenset(
    {
        State.NOT_STARTED,
        State.INFO_REQUESTED,
        State.IN_PREPARATION,
        State.PENDING_REVIEW,
        State.PENDING_CLIENT_APPROVAL,
        State.READY_TO_FILE,
        State.DEFERRED,
        State.DISPUTED,
    }
)


class DisplayStatus(StrEnum):
    """The universal status vocabulary.

    Identical word, colour and icon everywhere in the product — the values here
    are exactly the ones ``components/status_chip.html`` knows how to render, and
    that is not a coincidence worth breaking. Status is never encoded by colour
    alone.
    """

    OVERDUE = "overdue"
    DUE_SOON = "due-soon"
    ON_TRACK = "on-track"
    IN_PROGRESS = "in-progress"
    WAITING = "waiting"
    COMPLETE = "complete"
    NOT_APPLICABLE = "na"
    DISPUTED = "disputed"


#: How many days ahead counts as "due soon". A week is the span over which a CA
#: can still act — shorter and the warning is useless, longer and everything is
#: amber all the time and the colour stops meaning anything.
DUE_SOON_DAYS = 7


@dataclass(frozen=True, slots=True)
class Transition:
    """One legal move, and what it costs to make it.

    :param permission: the code the actor must hold. Serialised to the client so
        the UI can render only the buttons that would actually work — but never
        *trusted* from the client; the server re-checks on the POST.
    :param requires_note: the move is a judgement call and the reason has to be
        recorded. Deferring, disputing and overriding applicability all qualify.
    :param requires_filing_reference: an acknowledgement number is the evidence
        that the filing happened. Filing without one produces a register that
        cannot be audited.
    :param requires_mandatory_evidence: every evidence requirement the definition
        marked ``mandatory_for_close`` must be satisfied first.
    """

    source: State
    target: State
    label: str
    permission: str
    requires_note: bool = False
    requires_filing_reference: bool = False
    requires_mandatory_evidence: bool = False
    #: Shown to the user before they commit, when the move is hard to walk back.
    confirmation: str = ""


_WORKFLOW_STATES: tuple[State, ...] = (
    State.NOT_STARTED,
    State.INFO_REQUESTED,
    State.IN_PREPARATION,
    State.PENDING_REVIEW,
    State.PENDING_CLIENT_APPROVAL,
    State.READY_TO_FILE,
)


def _abandonable() -> tuple[Transition, ...]:
    """Deferring, dismissing and disputing are reachable from any open state.

    Written as a comprehension rather than enumerated, because the alternative is
    eighteen near-identical rows in which one inevitably ends up subtly different
    from its neighbours.
    """
    moves: list[Transition] = []
    for source in _WORKFLOW_STATES:
        moves.extend(
            [
                Transition(
                    source,
                    State.DEFERRED,
                    "Defer",
                    "compliance.obligation.defer",
                    requires_note=True,
                ),
                Transition(
                    source,
                    State.NOT_APPLICABLE,
                    "Mark not applicable",
                    "compliance.obligation.dismiss",
                    requires_note=True,
                    confirmation=(
                        "This obligation will stop appearing in the calendar, and the "
                        "nightly rebuild will not bring it back."
                    ),
                ),
                Transition(
                    source,
                    State.DISPUTED,
                    "Mark disputed",
                    "compliance.obligation.dispute",
                    requires_note=True,
                ),
            ]
        )
    return tuple(moves)


TRANSITIONS: tuple[Transition, ...] = (
    # -- The happy path -----------------------------------------------------
    Transition(
        State.NOT_STARTED,
        State.INFO_REQUESTED,
        "Request information",
        "compliance.obligation.request_info",
    ),
    Transition(
        State.NOT_STARTED, State.IN_PREPARATION, "Start work", "compliance.obligation.prepare"
    ),
    Transition(
        State.INFO_REQUESTED,
        State.IN_PREPARATION,
        "Information received",
        "compliance.obligation.prepare",
    ),
    Transition(
        State.IN_PREPARATION,
        State.INFO_REQUESTED,
        "Need more information",
        "compliance.obligation.request_info",
    ),
    Transition(
        State.IN_PREPARATION,
        State.PENDING_REVIEW,
        "Submit for review",
        "compliance.obligation.prepare",
    ),
    Transition(
        State.PENDING_REVIEW,
        State.IN_PREPARATION,
        "Send back for changes",
        "compliance.obligation.review",
        requires_note=True,
    ),
    Transition(
        State.PENDING_REVIEW,
        State.PENDING_CLIENT_APPROVAL,
        "Approve and send to client",
        "compliance.obligation.review",
    ),
    # A review that needs no client sign-off goes straight to ready. Common for
    # routine monthly filings where the client has standing authorisation.
    Transition(
        State.PENDING_REVIEW, State.READY_TO_FILE, "Approve for filing", "compliance.obligation.review"
    ),
    Transition(
        State.PENDING_CLIENT_APPROVAL,
        State.READY_TO_FILE,
        "Client approved",
        "compliance.obligation.approve",
    ),
    Transition(
        State.PENDING_CLIENT_APPROVAL,
        State.IN_PREPARATION,
        "Client requested changes",
        "compliance.obligation.approve",
        requires_note=True,
    ),
    Transition(
        State.READY_TO_FILE,
        State.FILED,
        "Record filing",
        "compliance.obligation.file",
        requires_filing_reference=True,
    ),
    Transition(
        State.FILED,
        State.CLOSED,
        "Close",
        "compliance.obligation.close",
        requires_mandatory_evidence=True,
    ),
    # -- Corrections --------------------------------------------------------
    # Filing details get entered wrong, and the alternative to an explicit
    # reversal is somebody editing the row in the database.
    Transition(
        State.FILED,
        State.READY_TO_FILE,
        "Reverse filing record",
        "compliance.obligation.file",
        requires_note=True,
        confirmation="The acknowledgement number and filing date will be cleared.",
    ),
    Transition(
        State.CLOSED,
        State.FILED,
        "Reopen",
        "compliance.obligation.reopen",
        requires_note=True,
    ),
    # -- Returning from the sidelines ---------------------------------------
    Transition(State.DEFERRED, State.NOT_STARTED, "Resume", "compliance.obligation.defer"),
    Transition(
        State.DISPUTED,
        State.IN_PREPARATION,
        "Dispute resolved — resume",
        "compliance.obligation.dispute",
        requires_note=True,
    ),
    Transition(
        State.DISPUTED,
        State.NOT_APPLICABLE,
        "Dispute resolved — not applicable",
        "compliance.obligation.dismiss",
        requires_note=True,
    ),
    Transition(
        State.NOT_APPLICABLE,
        State.NOT_STARTED,
        "Reinstate",
        "compliance.obligation.dismiss",
        requires_note=True,
    ),
    *_abandonable(),
)


_BY_SOURCE: dict[str, tuple[Transition, ...]] = {}
for _transition in TRANSITIONS:
    _BY_SOURCE[_transition.source] = (*_BY_SOURCE.get(_transition.source, ()), _transition)


def allowed_transitions(
    state: str,
    *,
    permissions: Iterable[str] | None = None,
) -> tuple[Transition, ...]:
    """Legal moves from ``state``, optionally filtered by what the actor holds.

    ``permissions=None`` means "do not filter" — used by the engine's own tests
    and by the serialiser that documents the whole table. A view always passes
    the real set.
    """
    moves = _BY_SOURCE.get(state, ())
    if permissions is None:
        return moves
    held = frozenset(permissions)
    return tuple(move for move in moves if move.permission in held)


def transition_for(state: str, target: str) -> Transition | None:
    """The transition from ``state`` to ``target``, or ``None`` if illegal.

    Returning ``None`` rather than raising: an illegal transition arrives from a
    stale page that a colleague already advanced, which is an ordinary race and
    deserves "someone else moved this on", not a 500.
    """
    for move in _BY_SOURCE.get(state, ()):
        if move.target == target:
            return move
    return None


# ---------------------------------------------------------------------------
# Derived status
# ---------------------------------------------------------------------------


def is_overdue(*, state: str, due_date: date | None, as_of: date) -> bool:
    """Whether an obligation is past its date and still owed.

    Kept trivially simple on purpose: the SQL annotation in
    ``stacos.obligations.queries`` must express exactly this, and a parity test
    asserts the two agree across a fixture matrix. Every clause added here has to
    be added there too.
    """
    if due_date is None:
        return False
    if state not in OPEN_STATES:
        return False
    return due_date < as_of


def filed_late(*, filed_on: date | None, due_date: date | None) -> bool:
    """Whether the filing missed its date.

    A fact about two dates, not a workflow position. Compared against the
    *effective* due date, so a government extension the client actually relied on
    means they were not late — which is the entire point of tracking the original
    and effective dates separately.
    """
    if filed_on is None or due_date is None:
        return False
    return filed_on > due_date


def derive_display_status(
    *,
    state: str,
    due_date: date | None,
    as_of: date,
    filed_on: date | None = None,
) -> DisplayStatus:
    """Collapse a state and a date into the one word shown to a user.

    The ordering matters: overdue beats everything except being finished. A user
    scanning a list of two hundred rows needs the worst true thing about each one,
    not the most recent.
    """
    if state in {State.FILED, State.CLOSED}:
        return DisplayStatus.COMPLETE
    if state == State.NOT_APPLICABLE:
        return DisplayStatus.NOT_APPLICABLE
    if state == State.DISPUTED:
        return DisplayStatus.DISPUTED

    if is_overdue(state=state, due_date=due_date, as_of=as_of):
        return DisplayStatus.OVERDUE

    if state in {State.INFO_REQUESTED, State.PENDING_CLIENT_APPROVAL, State.DEFERRED}:
        # Waiting on somebody else. Distinguished from in-progress because the
        # next action is not the current user's to take.
        return DisplayStatus.WAITING

    if due_date is not None and 0 <= (due_date - as_of).days <= DUE_SOON_DAYS:
        return DisplayStatus.DUE_SOON

    if state in {State.IN_PREPARATION, State.PENDING_REVIEW, State.READY_TO_FILE}:
        return DisplayStatus.IN_PROGRESS

    return DisplayStatus.ON_TRACK


def days_to_due(*, due_date: date | None, as_of: date) -> int | None:
    """Signed day count: negative is late. ``None`` when there is no date yet.

    Computed server-side against the entity's jurisdiction and handed to the
    template, never computed in the browser — a client whose clock or timezone
    differs must not see a different answer from the one the platform will act on.
    """
    if due_date is None:
        return None
    return (due_date - as_of).days


# ---------------------------------------------------------------------------
# Serialisation, for the client
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StateDescriptor:
    code: str
    label: str
    is_open: bool
    is_terminal: bool
    moves: tuple[Transition, ...] = field(default_factory=tuple)


_LABELS: Mapping[str, str] = {
    State.NOT_STARTED: "Not started",
    State.INFO_REQUESTED: "Information requested",
    State.IN_PREPARATION: "In preparation",
    State.PENDING_REVIEW: "Pending review",
    State.PENDING_CLIENT_APPROVAL: "Pending client approval",
    State.READY_TO_FILE: "Ready to file",
    State.FILED: "Filed",
    State.CLOSED: "Closed",
    State.NOT_APPLICABLE: "Not applicable",
    State.DEFERRED: "Deferred",
    State.DISPUTED: "Disputed",
}


def state_label(state: str) -> str:
    return _LABELS.get(state, state.replace("_", " ").capitalize())


def describe_states() -> tuple[StateDescriptor, ...]:
    """The whole transition table, ready to serialise.

    Used by the API so a mobile client can render the same action list as the web
    app without reimplementing the rules, and by a test that asserts every state
    is reachable and every non-terminal state can be left.
    """
    return tuple(
        StateDescriptor(
            code=str(state),
            label=state_label(state),
            is_open=state in OPEN_STATES,
            is_terminal=state in TERMINAL_STATES,
            moves=_BY_SOURCE.get(state, ()),
        )
        for state in State
    )
