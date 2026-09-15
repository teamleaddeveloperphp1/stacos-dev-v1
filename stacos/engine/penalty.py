"""
Computing exposure from a catalog definition's ``penalty_rules``.

Real statutory penalties do not all take the same shape, and pretending they
do produces a confident-looking number that is wrong. Two shapes are handled
here, deliberately not more:

* ``PER_DAY_CAPPED`` — a rate that accrues per day late, capped at either a
  fixed rupee amount or nothing STACOS tracks (e.g. "capped at the TDS
  amount"). The second case is still real data — the rate and the statutory
  reference are both worth showing — it just cannot be summed to a total, so
  :attr:`ComputedPenalty.computed_minor` is ``None`` rather than a guess.
* ``FIXED_RANGE`` — a flat band ("₹10,000 to ₹1,00,000") that depends on facts
  this module is never given (assessing officer's discretion, total income,
  ...). Never a day-accrual, so it never produces a total either — only the
  range, for the caller to render as text.

Money is integer paise throughout (``*_minor``), matching every other amount
in the product. This module takes ``days_late`` as an explicit argument rather
than computing it — see ``stacos.engine.lifecycle.days_late`` — because it
must not read a clock itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["ComputedPenalty", "compute_penalties"]


@dataclass(frozen=True, slots=True)
class ComputedPenalty:
    """One rule's exposure, computed as far as the data allows.

    ``computed_minor`` is ``None`` when the rule is real but not fully
    computable — an uncapped per-day rate, or a fixed range — never a
    fabricated number standing in for "we don't know".
    """

    kind: str
    statutory_reference: str
    computed_minor: int | None
    capped: bool
    rate_minor: int | None = None
    cap_minor: int | None = None
    min_minor: int | None = None
    max_minor: int | None = None


def compute_penalties(
    rules: Sequence[Mapping[str, Any]], *, days_late: int
) -> list[ComputedPenalty]:
    """Exposure for every rule a definition carries, independently.

    Penalties stack rather than combine — a filing can owe a per-day fee
    *and* a separate fixed penalty under a different section at once, which is
    why this returns one result per rule rather than a single total.
    """
    return [_compute_one(rule, days_late=days_late) for rule in rules]


def _compute_one(rule: Mapping[str, Any], *, days_late: int) -> ComputedPenalty:
    kind = str(rule.get("kind", ""))
    reference = str(rule.get("statutory_reference", ""))

    if kind == "PER_DAY_CAPPED":
        rate_minor = int(rule["rate_minor"])
        cap_minor = rule.get("cap_minor")
        cap_minor = int(cap_minor) if cap_minor is not None else None

        if days_late <= 0:
            return ComputedPenalty(
                kind=kind,
                statutory_reference=reference,
                computed_minor=0,
                capped=False,
                rate_minor=rate_minor,
                cap_minor=cap_minor,
            )

        accrued = rate_minor * days_late
        if cap_minor is None:
            # A real cap exists in the statute; we just don't have the base
            # amount it applies to (e.g. "the TDS amount"). Showing the rate
            # and reference without a total is honest; inventing a base is not.
            return ComputedPenalty(
                kind=kind,
                statutory_reference=reference,
                computed_minor=None,
                capped=False,
                rate_minor=rate_minor,
                cap_minor=None,
            )

        return ComputedPenalty(
            kind=kind,
            statutory_reference=reference,
            computed_minor=min(accrued, cap_minor),
            capped=accrued >= cap_minor,
            rate_minor=rate_minor,
            cap_minor=cap_minor,
        )

    if kind == "FIXED_RANGE":
        return ComputedPenalty(
            kind=kind,
            statutory_reference=reference,
            computed_minor=None,
            capped=False,
            min_minor=int(rule["min_minor"]),
            max_minor=int(rule["max_minor"]),
        )

    raise ValueError(f"unknown penalty rule kind {kind!r}")
