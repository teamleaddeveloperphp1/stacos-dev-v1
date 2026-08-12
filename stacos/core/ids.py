"""
UUIDv7 generation (RFC 9562).

Every primary key in STACOS is a UUIDv7. The reasons, in order:

  * **Time-ordered**, so B-tree index locality is close to a bigint sequence.
    Random UUIDv4 keys scatter inserts across the index and destroy write
    throughput on large tables — which is exactly what the obligation register
    will become.
  * **Not enumerable across tenants.** Sequential integers let a customer count
    your customers, and let a scoping bug become a walk of the whole table.
  * **Generable offline**, which the mobile client needs so evidence captured on
    a factory floor with no signal can carry its final ID before it syncs.

Neither Python 3.12 (`uuid.uuid7()` landed in 3.14) nor PostgreSQL 16
(`uuidv7()` landed in 18) can generate these, so it is done here.

.. warning::
   ``uuid7`` must stay at this module path forever. Django serialises the
   function *reference* into every migration that uses it as a field default;
   moving or renaming it breaks the historical migration graph.

Layout (128 bits, most significant first)::

    48 bits   unix timestamp in milliseconds
     4 bits   version (0b0111)
    12 bits   monotonic counter within the millisecond
     2 bits   variant (0b10)
    62 bits   random
"""

import secrets
import threading
import time
from datetime import UTC, datetime
from uuid import UUID

__all__ = ["uuid7", "uuid7_timestamp"]

_VERSION = 0x7
_VARIANT = 0b10
_COUNTER_BITS = 12
_COUNTER_MAX = (1 << _COUNTER_BITS) - 1
# Seed each new millisecond low in the counter space so there is room to
# increment for the rest of it without rolling over.
_COUNTER_SEED_BITS = 10

_lock = threading.Lock()
_last_ms = -1
_counter = 0


def uuid7() -> UUID:
    """Return a new time-ordered UUIDv7.

    Monotonic within a process even when several are generated in the same
    millisecond: the 12-bit counter increments, and on the (very unlikely)
    overflow we spin to the next millisecond rather than emit an out-of-order id.
    """
    global _last_ms, _counter

    with _lock:
        ms = time.time_ns() // 1_000_000

        if ms > _last_ms:
            _last_ms = ms
            _counter = secrets.randbits(_COUNTER_SEED_BITS)
        elif ms == _last_ms:
            _counter += 1
            if _counter > _COUNTER_MAX:
                # Exhausted this millisecond. Wait for the clock rather than
                # wrap, which would produce a non-monotonic id.
                while ms <= _last_ms:
                    ms = time.time_ns() // 1_000_000
                _last_ms = ms
                _counter = secrets.randbits(_COUNTER_SEED_BITS)
        else:
            # Clock moved backwards (NTP step). Keep issuing ids under the last
            # observed millisecond so ordering within this process holds.
            _counter += 1
            if _counter > _COUNTER_MAX:
                _last_ms += 1
                _counter = secrets.randbits(_COUNTER_SEED_BITS)
            ms = _last_ms

        timestamp, counter = ms, _counter

    value = (timestamp & 0xFFFF_FFFF_FFFF) << 80
    value |= _VERSION << 76
    value |= (counter & _COUNTER_MAX) << 64
    value |= _VARIANT << 62
    value |= secrets.randbits(62)
    return UUID(int=value)


def uuid7_timestamp(value: UUID) -> datetime:
    """Extract the creation time embedded in a UUIDv7.

    Useful for debugging and for support. Note this makes creation time visible
    to anyone holding the id — acceptable for a customer's own data, but a reason
    not to expose these ids to *other* parties without an opaque alias.
    """
    if value.version != 7:
        raise ValueError(f"Not a UUIDv7: {value} (version {value.version})")
    return datetime.fromtimestamp((value.int >> 80) / 1000, tz=UTC)
