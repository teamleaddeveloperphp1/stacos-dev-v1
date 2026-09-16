"""
The lifecycle transition table, asserted rather than assumed.

Pure-Python tests: no database, no Django. The table is data, and these are the
properties that make it a *usable* state machine rather than a list of pairs —
every state reachable, every open state escapable, no move that skips a guard.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stacos.engine.lifecycle import (
    CLOSED_STATES,
    OPEN_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    DisplayStatus,
    State,
    allowed_transitions,
    days_late,
    days_to_due,
    derive_display_status,
    describe_states,
    filed_late,
    is_overdue,
    state_label,
    transition_for,
)

TODAY = date(2026, 8, 12)

ALL_PERMISSIONS = frozenset(move.permission for move in TRANSITIONS)


# ===========================================================================
# Shape of the table
# ===========================================================================


def test_every_state_is_reachable() -> None:
    """No orphan states.

    A state nothing can transition *into* is either dead code or a gap in the
    table, and both are worth failing the build over. ``NOT_STARTED`` is exempt:
    it is where an instance is created.
    """
    reachable = {move.target for move in TRANSITIONS} | {State.NOT_STARTED}
    orphans = sorted(set(State) - reachable)
    assert not orphans, f"unreachable states: {orphans}"


def test_every_non_terminal_state_can_be_left() -> None:
    """No dead ends short of ``CLOSED``.

    An obligation that can be moved into a state it cannot be moved out of is
    stuck, and the only remedy for the user is a support ticket.
    """
    stuck = [state for state in State if state != State.CLOSED and not allowed_transitions(state)]
    assert not stuck, f"states with no way out: {stuck}"


def test_terminal_states_are_reachable_and_reversible() -> None:
    """Filing and closing can both be undone, because both get entered by mistake.

    The alternative to an explicit reversal is somebody editing the row in the
    database, which leaves no trace at all.
    """
    for state in TERMINAL_STATES:
        assert allowed_transitions(state), f"{state} cannot be reversed"


def test_open_and_closed_states_partition_the_vocabulary() -> None:
    """Every state is either owed or resolved, and none is both."""
    assert not (OPEN_STATES & CLOSED_STATES)
    assert set(State) == OPEN_STATES | CLOSED_STATES


def test_no_duplicate_transitions() -> None:
    """One move per (source, target). Two would make the guard chosen arbitrary."""
    pairs = [(move.source, move.target) for move in TRANSITIONS]
    duplicates = {pair for pair in pairs if pairs.count(pair) > 1}
    assert not duplicates, f"duplicate transitions: {sorted(duplicates)}"


def test_no_self_transitions() -> None:
    """A move to the state you are already in would write a meaningless event."""
    assert not [move for move in TRANSITIONS if move.source == move.target]


def test_every_state_has_a_human_label() -> None:
    assert all(state_label(state) != str(state) for state in State)


# ===========================================================================
# Guards
# ===========================================================================


def test_recording_a_filing_requires_a_reference() -> None:
    """The acknowledgement number is what defends an assessment.

    A filing recorded without one produces a register that cannot be audited,
    which defeats the purpose of keeping it.
    """
    move = transition_for(State.READY_TO_FILE, State.FILED)
    assert move is not None
    assert move.requires_filing_reference


def test_closing_requires_mandatory_evidence() -> None:
    move = transition_for(State.FILED, State.CLOSED)
    assert move is not None
    assert move.requires_mandatory_evidence


@pytest.mark.parametrize(
    "target",
    [State.NOT_APPLICABLE, State.DEFERRED, State.DISPUTED],
)
def test_leaving_the_happy_path_always_requires_a_reason(target: State) -> None:
    """Every judgement call is recorded with the reason for it.

    These three transitions override the engine's own conclusion. Somebody will
    ask why in eighteen months, and "a user clicked a button" is not an answer.
    """
    moves = [m for m in TRANSITIONS if m.target == target]
    assert moves
    assert all(move.requires_note for move in moves), f"{target} allows a silent move"


def test_dismissing_warns_before_it_happens() -> None:
    """Marking something not applicable suppresses it permanently, so it is confirmed."""
    move = transition_for(State.NOT_STARTED, State.NOT_APPLICABLE)
    assert move is not None
    assert move.confirmation


# ===========================================================================
# Permission filtering
# ===========================================================================


def test_transitions_are_filtered_by_permission() -> None:
    """The action list a user sees is the list they could actually complete."""
    everything = allowed_transitions(State.NOT_STARTED, permissions=ALL_PERMISSIONS)
    nothing = allowed_transitions(State.NOT_STARTED, permissions=frozenset())
    only_prepare = allowed_transitions(
        State.NOT_STARTED, permissions=frozenset({"compliance.obligation.prepare"})
    )

    assert everything
    assert not nothing
    assert [move.target for move in only_prepare] == [State.IN_PREPARATION]


def test_an_illegal_transition_returns_none_rather_than_raising() -> None:
    """A stale page is an ordinary race, not a crash.

    Somebody else advanced the obligation between the page rendering and the
    button being pressed. That deserves "someone moved this on", not a 500.
    """
    assert transition_for(State.NOT_STARTED, State.CLOSED) is None


def test_describe_states_covers_the_whole_vocabulary() -> None:
    """The serialised table is what a mobile client renders its actions from."""
    described = describe_states()
    assert {d.code for d in described} == {str(s) for s in State}
    assert all(d.label for d in described)


# ===========================================================================
# Derived status
# ===========================================================================


def test_overdue_needs_a_date_a_state_and_a_comparison() -> None:
    yesterday = TODAY - timedelta(days=1)

    assert is_overdue(state=State.NOT_STARTED, due_date=yesterday, as_of=TODAY)
    # No date resolved yet: unknowable, therefore not overdue.
    assert not is_overdue(state=State.NOT_STARTED, due_date=None, as_of=TODAY)
    # Finished: cannot become overdue afterwards.
    assert not is_overdue(state=State.FILED, due_date=yesterday, as_of=TODAY)
    # Due today is not yet late.
    assert not is_overdue(state=State.NOT_STARTED, due_date=TODAY, as_of=TODAY)


def test_filed_late_compares_against_the_effective_date() -> None:
    """A filing inside a granted extension is not late.

    Which is the entire reason the original and effective dates are tracked
    apart: a client who relied on a notification did nothing wrong.
    """
    assert filed_late(filed_on=date(2026, 8, 21), due_date=date(2026, 8, 20))
    assert not filed_late(filed_on=date(2026, 8, 20), due_date=date(2026, 8, 20))
    assert not filed_late(filed_on=None, due_date=date(2026, 8, 20))
    assert not filed_late(filed_on=date(2026, 8, 21), due_date=None)


@pytest.mark.parametrize(
    ("state", "due", "expected"),
    [
        (State.FILED, TODAY - timedelta(days=30), DisplayStatus.COMPLETE),
        (State.CLOSED, TODAY - timedelta(days=30), DisplayStatus.COMPLETE),
        (State.NOT_APPLICABLE, TODAY, DisplayStatus.NOT_APPLICABLE),
        (State.DISPUTED, TODAY - timedelta(days=5), DisplayStatus.DISPUTED),
        (State.NOT_STARTED, TODAY - timedelta(days=1), DisplayStatus.OVERDUE),
        (State.IN_PREPARATION, TODAY - timedelta(days=1), DisplayStatus.OVERDUE),
        (State.INFO_REQUESTED, TODAY + timedelta(days=30), DisplayStatus.WAITING),
        (State.PENDING_CLIENT_APPROVAL, TODAY + timedelta(days=30), DisplayStatus.WAITING),
        (State.NOT_STARTED, TODAY + timedelta(days=3), DisplayStatus.DUE_SOON),
        (State.IN_PREPARATION, TODAY + timedelta(days=30), DisplayStatus.IN_PROGRESS),
        (State.NOT_STARTED, TODAY + timedelta(days=90), DisplayStatus.NOT_STARTED),
        (State.NOT_STARTED, None, DisplayStatus.NOT_STARTED),
    ],
)
def test_display_status_matrix(state: State, due: date | None, expected: DisplayStatus) -> None:
    """Overdue beats everything except being finished.

    A user scanning two hundred rows needs the worst true thing about each one,
    not the most recent thing that happened to it.
    """
    assert derive_display_status(state=state, due_date=due, as_of=TODAY) is expected


def test_not_started_with_a_recorded_reason_reads_as_pending() -> None:
    """"Not started, nothing said" and "not started, here is why" are different
    enough to read differently — see ``stacos.obligations.transitions.record_pending``.
    """
    assert (
        derive_display_status(
            state=State.NOT_STARTED,
            due_date=TODAY + timedelta(days=90),
            as_of=TODAY,
            pending_reason="Waiting on the client's bank statement.",
        )
        is DisplayStatus.PENDING
    )


def test_overdue_and_due_soon_still_beat_a_recorded_reason() -> None:
    """An explanation does not make a late filing calmer, or an imminent one
    less urgent — see :func:`derive_display_status`'s docstring."""
    assert (
        derive_display_status(
            state=State.NOT_STARTED,
            due_date=TODAY - timedelta(days=1),
            as_of=TODAY,
            pending_reason="Waiting on the client's bank statement.",
        )
        is DisplayStatus.OVERDUE
    )
    assert (
        derive_display_status(
            state=State.NOT_STARTED,
            due_date=TODAY + timedelta(days=3),
            as_of=TODAY,
            pending_reason="Waiting on the client's bank statement.",
        )
        is DisplayStatus.DUE_SOON
    )


def test_days_to_due_is_signed_and_null_safe() -> None:
    assert days_to_due(due_date=TODAY + timedelta(days=5), as_of=TODAY) == 5
    assert days_to_due(due_date=TODAY - timedelta(days=5), as_of=TODAY) == -5
    assert days_to_due(due_date=TODAY, as_of=TODAY) == 0
    assert days_to_due(due_date=None, as_of=TODAY) is None


def test_days_late_floors_at_zero_and_needs_no_clock() -> None:
    assert days_late(due_date=None, filed_on=None, as_of=TODAY) == 0
    # Not yet due — floored, not negative.
    assert days_late(due_date=TODAY + timedelta(days=5), filed_on=None, as_of=TODAY) == 0
    # Overdue and unfiled: accrues against `as_of`.
    assert days_late(due_date=TODAY - timedelta(days=5), filed_on=None, as_of=TODAY) == 5
    # Filed on time: nothing accrued, regardless of how long ago `as_of` is.
    assert days_late(due_date=TODAY, filed_on=TODAY, as_of=TODAY + timedelta(days=30)) == 0
    # Filed late: frozen at the filing date, not still growing against `as_of`.
    assert (
        days_late(
            due_date=TODAY - timedelta(days=10),
            filed_on=TODAY - timedelta(days=3),
            as_of=TODAY + timedelta(days=30),
        )
        == 7
    )
