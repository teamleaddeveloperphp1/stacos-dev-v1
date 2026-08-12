"""UUIDv7 generation. No database required."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stacos.core.ids import uuid7, uuid7_timestamp


def test_version_and_variant_bits_are_correct() -> None:
    value = uuid7()
    assert value.version == 7
    # RFC 9562 variant is 0b10 in the two most significant bits of octet 8.
    assert (value.int >> 62) & 0b11 == 0b10


def test_ids_are_monotonic_within_the_same_millisecond() -> None:
    """The reason a counter exists at all.

    Several ids generated inside one millisecond must still sort in creation
    order, otherwise index locality and any "order by id" listing break subtly.
    """
    batch = [uuid7() for _ in range(5000)]
    assert batch == sorted(batch), "UUIDv7 values must be monotonically increasing"


def test_ids_are_unique() -> None:
    batch = [uuid7() for _ in range(10_000)]
    assert len(set(batch)) == len(batch)


def test_timestamp_round_trips() -> None:
    before = datetime.now(UTC)
    value = uuid7()
    after = datetime.now(UTC)

    extracted = uuid7_timestamp(value)
    # Millisecond truncation means the extracted value can sit one millisecond
    # behind `before`.
    assert before.timestamp() - 0.002 <= extracted.timestamp() <= after.timestamp() + 0.002


def test_ids_generated_later_sort_later() -> None:
    first = uuid7()
    time.sleep(0.005)
    second = uuid7()
    assert first < second


def test_rejects_non_v7_input() -> None:
    with pytest.raises(ValueError, match="Not a UUIDv7"):
        uuid7_timestamp(UUID("00000000-0000-4000-8000-000000000000"))


@given(st.integers(min_value=1, max_value=200))
def test_any_batch_size_stays_ordered(size: int) -> None:
    batch = [uuid7() for _ in range(size)]
    assert batch == sorted(batch)
