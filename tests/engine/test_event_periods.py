"""
Event-driven materialisation, at the engine level.

No database here on purpose. Every property that matters about triggered
obligations is a property of :func:`plan` against a profile, and proving them
without Postgres is what makes them cheap enough to keep.

The test that earns its place above all the others is
``test_occurrence_survives_a_sibling_being_deleted``. Occurrence numbers derived
from *position* pass every other test in this file and fail that one, and the
failure in production is silent: the row somebody had begun preparing is
superseded and an empty duplicate appears beside it.
"""

from __future__ import annotations

from datetime import date

import pytest

from stacos.engine.dates import collapse_occurrences
from stacos.engine.planner import EntityProfileView, ExistingInstance, plan
from stacos.engine.types import (
    DefinitionSnapshot,
    EventOccurrence,
    FiscalYearConvention,
    Identity,
    InstanceScope,
    Periodicity,
    ScopeRef,
    occurrence_number,
)

FY = FiscalYearConvention(4, 1, "FY{start_year}-{end_year_short}")
HORIZON_START = date(2026, 1, 1)
HORIZON_END = date(2027, 6, 30)
AS_OF = date(2026, 6, 1)


def triggered(
    code: str = "IN-MCA-DIR12-APPOINTMENT",
    *,
    event_key: str = "DIRECTOR_APPOINTED",
    days: int = 30,
    when: dict | None = None,
    scope: InstanceScope = InstanceScope.ENTITY,
    effective_from: date = date(2014, 4, 1),
) -> DefinitionSnapshot:
    trigger: dict = {"event_key": event_key}
    if when:
        trigger["when"] = when
    return DefinitionSnapshot(
        code=code,
        version=1,
        title=code,
        country="IN",
        periodicity=Periodicity.EVENT_BASED,
        due_rule={"anchor": "TRIGGER_DATE", "offset": {"days": days}, "shift_if_holiday": "NONE"},
        trigger=trigger,
        instance_scope=scope,
        scope_selector={"registration_type": "GST"} if scope is InstanceScope.REGISTRATION else {},
        effective_from=effective_from,
    )


def profile(
    *occurrences: EventOccurrence, registrations: tuple[ScopeRef, ...] = ()
) -> EntityProfileView:
    return EntityProfileView(
        entity_id="e1",
        country="IN",
        facts={"country": "IN", "entity_type": "PVT_LTD"},
        registrations=registrations,
        occurrences=occurrences,
        events=collapse_occurrences(occurrences),
        incorporation_date=date(2011, 1, 1),
    )


def run(catalog, view, *, existing=()) -> object:
    return plan(
        profile=view,
        catalog=list(catalog),
        horizon_start=HORIZON_START,
        horizon_end=HORIZON_END,
        fy=FY,
        existing=existing,
        as_of=AS_OF,
    )


APRIL = date(2026, 4, 17)
RAMESH = EventOccurrence("DIRECTOR_APPOINTED", APRIL, "ref-ramesh", label="Ramesh Mehta")
PRIYA = EventOccurrence(
    "DIRECTOR_APPOINTED", APRIL, "ref-priya", label="Priya Nair", attributes={"role": "MD"}
)


def test_no_events_means_no_obligation() -> None:
    """The whole point of a trigger: nothing until something happens."""
    assert run([triggered()], profile()).to_create == ()


def test_one_instance_per_occurrence() -> None:
    created = run([triggered()], profile(RAMESH)).to_create
    assert len(created) == 1
    assert created[0].due_date == date(2026, 5, 17)


def test_two_events_on_the_same_day_both_materialise() -> None:
    """The case the original unique constraint made impossible."""
    created = run([triggered()], profile(RAMESH, PRIYA)).to_create
    assert len(created) == 2
    assert len({instance.identity.occurrence for instance in created}) == 2
    # Same period key — they are two occurrences within one day, not two days.
    assert {instance.identity.period_key for instance in created} == {"EV-2026-04-17"}


def test_occurrence_survives_a_sibling_being_deleted() -> None:
    """Deleting one same-day event must not renumber the other.

    Under the natural key a renumbered occurrence is a *different* obligation, so
    an ordinal scheme would supersede the row somebody was preparing and create an
    empty duplicate beside it. This is the test that rules ordinals out.
    """
    both = {
        instance.period.label: instance.identity.occurrence
        for instance in run([triggered()], profile(RAMESH, PRIYA)).to_create
    }
    alone = {
        instance.period.label: instance.identity.occurrence
        for instance in run([triggered()], profile(PRIYA)).to_create
    }
    assert alone["Priya Nair"] == both["Priya Nair"]


def test_occurrence_does_not_depend_on_the_hash_seed() -> None:
    """Pinned to a literal.

    ``hash()`` is salted per process, so numbering with it would give the web
    process and the Celery worker different answers — and the nightly job would
    duplicate every event-driven obligation the interactive path created, every
    night. If this assertion is ever "fixed" by updating the number, read
    ``occurrence_number`` first.
    """
    assert occurrence_number("ref-ramesh") == 18041
    assert occurrence_number("ref-priya") == 10942
    # And inside the column that stores it: PositiveSmallIntegerField is a
    # PostgreSQL smallint, so anything above 32767 is a DataError on insert.
    assert 0 <= occurrence_number("ref-ramesh") < 32768


def test_a_trigger_condition_filters_which_definitions_fire() -> None:
    """MR-1 follows an executive appointment only; DIR-12 follows all of them."""
    mr1 = triggered(
        "IN-MCA-MR1",
        days=60,
        when={"any": [{"fact": "role", "op": "in", "value": ["MD", "WTD", "MANAGER"]}]},
    )
    created = run([triggered(), mr1], profile(RAMESH, PRIYA)).to_create
    by_code: dict[str, list] = {}
    for instance in created:
        by_code.setdefault(instance.definition_code, []).append(instance)

    assert len(by_code["IN-MCA-DIR12-APPOINTMENT"]) == 2
    # Ramesh has no role recorded at all, so the condition is UNKNOWN for him and
    # the obligation materialises unconfirmed rather than being dropped.
    assert len(by_code["IN-MCA-MR1"]) == 2
    assert {instance.confirmed for instance in by_code["IN-MCA-MR1"]} == {True}


def test_a_trigger_condition_that_is_false_suppresses_the_instance() -> None:
    clerk = EventOccurrence(
        "DIRECTOR_APPOINTED", APRIL, "ref-clerk", attributes={"role": "DIRECTOR"}
    )
    mr1 = triggered(
        "IN-MCA-MR1",
        when={"any": [{"fact": "role", "op": "in", "value": ["MD", "WTD", "MANAGER"]}]},
    )
    assert run([mr1], profile(clerk)).to_create == ()


def test_an_event_before_the_rule_existed_produces_nothing() -> None:
    old = EventOccurrence("DIRECTOR_APPOINTED", date(2010, 5, 1), "ref-old")
    assert run([triggered(effective_from=date(2014, 4, 1))], profile(old)).to_create == ()


def test_an_event_before_incorporation_produces_nothing() -> None:
    view = EntityProfileView(
        entity_id="e1",
        country="IN",
        facts={"country": "IN", "entity_type": "PVT_LTD"},
        occurrences=(EventOccurrence("DIRECTOR_APPOINTED", date(2009, 5, 1), "r"),),
        incorporation_date=date(2011, 1, 1),
    )
    assert run([triggered()], view).to_create == ()


def test_a_triggered_instance_outside_the_horizon_still_materialises() -> None:
    """The deliberate asymmetry with periodic obligations.

    An unfiled DIR-12 from eight months ago carries a daily penalty with no cap.
    Horizon-filtering it would make the planner stop desiring it, and `_diff`
    would quietly archive the most expensive row in the register on a night when
    nothing happened.
    """
    long_ago = EventOccurrence("DIRECTOR_APPOINTED", date(2024, 2, 1), "ref-stale")
    created = run([triggered()], profile(long_ago)).to_create
    assert len(created) == 1
    assert created[0].due_date == date(2024, 3, 2)
    assert created[0].due_date < HORIZON_START


def test_a_scoped_event_only_produces_its_own_scope() -> None:
    gst_mh = ScopeRef(
        kind=InstanceScope.REGISTRATION, ref="reg-mh", label="GST", jurisdiction="IN-MH"
    )
    gst_ka = ScopeRef(
        kind=InstanceScope.REGISTRATION, ref="reg-ka", label="GST", jurisdiction="IN-KA"
    )
    cancelled = EventOccurrence(
        "GST_CANCELLATION_EFFECTIVE", APRIL, "ref-cancel", scope_ref="reg-ka"
    )
    view = profile(cancelled, registrations=(gst_mh, gst_ka))
    definition = triggered(
        "IN-GST-GSTR10", event_key="GST_CANCELLATION_EFFECTIVE", scope=InstanceScope.REGISTRATION
    )

    created = run([definition], view).to_create
    assert len(created) == 1
    assert created[0].identity.scope_ref == "reg-ka"


def test_an_entity_wide_event_produces_one_per_scope() -> None:
    gst_mh = ScopeRef(kind=InstanceScope.REGISTRATION, ref="reg-mh", label="GST")
    gst_ka = ScopeRef(kind=InstanceScope.REGISTRATION, ref="reg-ka", label="GST")
    changed = EventOccurrence("GST_PARTICULARS_CHANGED", APRIL, "ref-change")
    definition = triggered(
        "IN-GST-REG14", event_key="GST_PARTICULARS_CHANGED", scope=InstanceScope.REGISTRATION
    )

    created = run([definition], profile(changed, registrations=(gst_mh, gst_ka))).to_create
    assert {instance.identity.scope_ref for instance in created} == {"reg-mh", "reg-ka"}


def test_planning_twice_is_idempotent() -> None:
    """The invariant the nightly job depends on."""
    view = profile(RAMESH, PRIYA)
    first = run([triggered()], view)

    applied = [
        ExistingInstance(
            identity=instance.identity,
            due_date=instance.due_date,
            definition_version=instance.definition_version,
        )
        for instance in first.to_create
    ]
    assert run([triggered()], view, existing=applied).is_empty


def test_period_keys_sort_chronologically() -> None:
    early = EventOccurrence("DIRECTOR_APPOINTED", date(2026, 2, 3), "a")
    late = EventOccurrence("DIRECTOR_APPOINTED", date(2026, 11, 30), "b")
    keys = sorted(
        instance.identity.period_key
        for instance in run([triggered()], profile(early, late)).to_create
    )
    assert keys == ["EV-2026-02-03", "EV-2026-11-30"]


@pytest.mark.parametrize(
    "generated",
    ["2026-07", "FY2026-27", "FY2026-27-Q2", "FY2026-27-H1", "2026-CQ1", "ONCE"],
)
def test_the_event_period_key_cannot_collide_with_a_generated_one(generated: str) -> None:
    assert not generated.startswith("EV-")


def test_collapse_takes_the_latest_per_key() -> None:
    """What an EVENT_DATE anchor reads: the last board meeting, not the first."""
    occurrences = [
        EventOccurrence("BOARD_MEETING", date(2026, 5, 1), "a"),
        EventOccurrence("BOARD_MEETING", date(2026, 9, 1), "b"),
        EventOccurrence("AGM_DATE", date(2026, 9, 25), "c"),
    ]
    assert collapse_occurrences(occurrences) == {
        "BOARD_MEETING": date(2026, 9, 1),
        "AGM_DATE": date(2026, 9, 25),
    }


def test_an_opt_in_materialises_a_definition_the_rule_refuses() -> None:
    """The symmetric counterpart of a suppression."""
    llp_only = DefinitionSnapshot(
        code="IN-MCA-LLP-FORM8",
        version=1,
        title="LLP Form 8",
        country="IN",
        periodicity=Periodicity.ANNUAL,
        due_rule={
            "anchor": "PERIOD_END",
            "offset": {"months": 6, "day_of_month": 30},
            "shift_if_holiday": "NONE",
        },
        applicability_rule={
            "all": [
                {"fact": "entity_type", "op": "eq", "value": "LLP", "explain": "you are an LLP"}
            ]
        },
        effective_from=date(2009, 4, 1),
    )
    view = profile()

    assert run([llp_only], view).to_create == ()

    forced = plan(
        profile=view,
        catalog=[llp_only],
        horizon_start=HORIZON_START,
        horizon_end=HORIZON_END,
        fy=FY,
        opted_in=frozenset({"IN-MCA-LLP-FORM8"}),
        as_of=AS_OF,
    )
    assert len(forced.to_create) == 1
    instance = forced.to_create[0]
    # Never "confirmed": the rule did not decide this, a person did, and the row
    # has to say which.
    assert instance.confirmed is False
    assert instance.reasons[0] == "you added this to your calendar"


def test_suppression_matches_a_specific_occurrence() -> None:
    """Dismissing one of two same-day filings must not dismiss the other."""
    view = profile(RAMESH, PRIYA)
    created = run([triggered()], view).to_create
    victim = created[0].identity

    remaining = plan(
        profile=view,
        catalog=[triggered()],
        horizon_start=HORIZON_START,
        horizon_end=HORIZON_END,
        fy=FY,
        suppressed=frozenset({victim}),
        as_of=AS_OF,
    ).to_create

    assert len(remaining) == 1
    assert remaining[0].identity != victim
    assert remaining[0].identity.occurrence != victim.occurrence


def test_identity_carries_the_occurrence() -> None:
    """Guards the field the whole scheme rests on."""
    assert Identity("c", "", "EV-2026-04-17", 7) != Identity("c", "", "EV-2026-04-17", 8)
