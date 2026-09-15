"""
"What applies to you" — the grouping behind it, and the counts on it.

This screen is where somebody decides whether the product understood their
business before committing to it. Two things were stopping it doing that job, and
both are logic rather than appearance, so both are pinned here.

**The counts were not true.** Each column was truncated to sixty rows *before*
being stored, and the counts were read off the truncated tuples — so the button
somebody presses to create their calendar offered "60 obligations" to a business
with several hundred. Shortening the screen is fine; shortening the number is
the one thing on it that must never happen.

**The reason was printed against every row.** A column of two hundred rows each
carrying the same sentence is a column nobody reads, and the rows that are there
for a *different* reason — the ones somebody checking our working is looking for
— were the hardest to find in it.

Appearance is not tested. The grouping and the arithmetic are.
"""

from __future__ import annotations

from datetime import date

import pytest

from stacos.core.scope import platform_scope
from stacos.obligations.preview import preview_entity
from stacos.obligations.profile import build_profile_view
from stacos.tenancy.models import Entity, EntityProfile, EntityRegistration, Tenant

pytestmark = pytest.mark.django_db


@pytest.fixture
def partly_known_entity(org: Tenant) -> Entity:
    """A realistic entity that has added one registration and answered two
    questions — everything else, deliberately, is still unknown.

    Deliberately partial rather than the fully-profiled ``manufacturer``
    fixture: a business that complete leaves almost nothing undecided, and
    this file is specifically about the "might apply" column and its link to
    the questions beside it, which only exists while real facts are still
    missing. A registration is included rather than omitted, unlike the old
    session-draft fixture this replaces — ``build_profile_view`` treats zero
    registrations as a definite "none", not "not answered yet" (see
    ``stacos.obligations.profile._base_facts``), which is correct for a saved
    entity but leaves nothing to test here if there genuinely are none.
    """
    with platform_scope(reason="test-fixture"):
        entity = Entity.objects.create(
            tenant=org,
            name="Shreeji Textiles",
            entity_type="PVT_LTD",
            country="IN",
            registered_office_state="IN-GJ",
        )
        EntityProfile.objects.create(
            tenant=org,
            entity=entity,
            employee_count=200,
            aggregate_turnover=800000000,
            states_of_operation=["IN-GJ", "IN-MH"],
            facts={"gst_scheme": "REGULAR"},
        )
        EntityRegistration.objects.create(
            tenant=org,
            entity=entity,
            type="GST",
            value="24AABCU9603R1ZM",
            jurisdiction="IN-GJ",
        )
    return entity


@pytest.fixture
def preview(partly_known_entity: Entity) -> object:
    """The partition against a realistic-but-incomplete profile.

    Realistic rather than minimal, so the catalog actually produces all three
    columns — a one-fact profile would leave "applies" nearly empty and prove
    nothing about grouping, which only misbehaves at the sizes that made the
    screen unreadable in the first place.
    """
    with platform_scope(reason="test-fixture"):
        profile = build_profile_view(partly_known_entity, as_of=date(2026, 8, 12))
    # A larger queue than the card's default, so the link between a blocked
    # group and the question that clears it is actually exercised. With the
    # default eight, whether any group links at all depends on which facts happen
    # to rank highest for this profile.
    return preview_entity(profile, country=partly_known_entity.country, question_limit=40)


# ---------------------------------------------------------------------------
# The counts are the full set
# ---------------------------------------------------------------------------


def test_counts_match_the_rows_they_describe(preview: object) -> None:
    assert preview.applies_count == len(preview.applies)
    assert preview.might_count == len(preview.might_apply)
    assert preview.excluded_count == len(preview.does_not_apply)


def test_every_rule_considered_lands_in_exactly_one_column(preview: object) -> None:
    """Kleene evaluation has three outcomes and the screen has three columns.

    A rule that fell out of all of them would be an obligation quietly dropped,
    which is the failure mode the whole "might apply" column exists to prevent.
    """
    total = preview.applies_count + preview.might_count + preview.excluded_count

    assert total == preview.considered

    codes = [row.code for row in (*preview.applies, *preview.might_apply, *preview.does_not_apply)]
    assert len(codes) == len(set(codes)), "a definition appears in two columns"


def test_folding_the_display_does_not_change_the_count(preview: object) -> None:
    """The heart of it.

    Every row is reachable through the grouping, whether or not the template
    chooses to draw it — so no arrangement of the screen can make the number on
    the button disagree with the calendar it creates.
    """
    grouped = sum(group.count for family in preview.applies_by_family() for group in family.groups)

    assert grouped == preview.applies_count

    shown = sum(
        len(group.shown) for family in preview.applies_by_family() for group in family.groups
    )
    hidden = sum(group.hidden for family in preview.applies_by_family() for group in family.groups)
    assert shown + hidden == preview.applies_count


def test_a_family_count_is_the_sum_of_its_groups(preview: object) -> None:
    for family in preview.applies_by_family():
        assert family.count == sum(group.count for group in family.groups)


def test_the_number_on_the_button_is_the_number_of_obligations(preview: object) -> None:
    """`applies_count` is what the commit button renders. It used to cap at 60."""
    assert preview.applies_count == len(preview.applies)
    assert preview.applies_count > 60 or len(preview.applies) < 60, (
        "the fixture no longer produces enough rows to catch a 60-row truncation"
    )


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_each_row_is_grouped_exactly_once(preview: object) -> None:
    grouped = [
        row.code
        for family in preview.applies_by_family()
        for group in family.groups
        for row in group.rows
    ]

    assert sorted(grouped) == sorted(row.code for row in preview.applies)
    assert len(grouped) == len(set(grouped)), "a row was grouped twice"


def test_rows_in_a_group_really_do_share_their_reason(preview: object) -> None:
    """Otherwise printing the reason once per group would be a lie about the
    rows underneath it, which is worse than repeating it."""
    for family in preview.applies_by_family():
        for group in family.groups:
            assert {row.reason for row in group.rows} == {group.reason}


def test_families_are_ordered_largest_first(preview: object) -> None:
    counts = [family.count for family in preview.applies_by_family()]

    assert counts == sorted(counts, reverse=True)


def test_groups_within_a_family_are_ordered_largest_first(preview: object) -> None:
    """So the shared explanation covering the most ground comes first, and the
    odd ones out fall to the bottom where they are visible."""
    for family in preview.applies_by_family():
        counts = [group.count for group in family.groups]
        assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# The undecided column is connected to the questions beside it
# ---------------------------------------------------------------------------


def test_undecided_groups_carry_the_question_that_settles_them(preview: object) -> None:
    """The whole point of the column.

    A group that names a fact but not a question leaves the user to work out
    which control clears it, which is the reading this screen was failing.
    """
    asked = {question.key for question in preview.questions}
    linked = [
        group
        for family in preview.might_by_family()
        for group in family.groups
        if group.blocked_on in asked
    ]

    assert linked, (
        "nothing in the undecided column is linked to the queue beside it — "
        "the connection between the two halves of this screen is the whole point"
    )

    for group in linked:
        assert group.question is not None
        assert group.question.key == group.blocked_on
        assert group.is_answerable_here


def test_a_group_nothing_here_can_settle_is_not_offered_as_work(preview: object) -> None:
    """Facts that come from a registration or a premises are not questions.

    Presenting them as if they were is what makes the column read as a list
    nobody can finish.
    """
    asked = {question.key for question in preview.questions}

    for family in preview.might_by_family():
        for group in family.groups:
            if group.blocked_on not in asked:
                assert not group.is_answerable_here
                assert group.question is None


def test_the_three_blocked_states_are_kept_apart(preview: object) -> None:
    """Asked now, asked once the queue moves on, and never asked here.

    The queue is capped at the most consequential handful, so a genuinely
    answerable fact can sit outside it. Collapsing that into "comes from your
    registrations" would tell the user something false about their own data.
    """
    states = set()
    for family in preview.might_by_family():
        for group in family.groups:
            # Exactly one of the three, never two.
            assert not (group.is_answerable_here and group.is_askable_later)
            if group.is_answerable_here:
                states.add("now")
            elif group.is_askable_later:
                states.add("later")
                assert group.blocked_label, (
                    f"{group.blocked_on} is answerable but has no label to show"
                )
            else:
                states.add("elsewhere")
                assert not group.blocked_on

    assert states, "this profile leaves nothing undecided at all"


def test_the_answerable_count_only_counts_what_is_answerable(preview: object) -> None:
    answerable = sum(1 for row in preview.might_apply if row.blocked_on)

    assert preview.answerable_might_count == answerable
    assert preview.answerable_might_count <= preview.might_count


def test_undecided_rows_are_grouped_by_what_blocks_them(preview: object) -> None:
    for family in preview.might_by_family():
        for group in family.groups:
            assert {row.blocked_on for row in group.rows} == {group.blocked_on}
