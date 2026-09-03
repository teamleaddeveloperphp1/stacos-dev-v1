"""
Refutability: can this rule still fire, given only part of the picture?

The catalog browser and the onboarding preview both need to answer "select a
legal form and a state — what could apply to me?" before anything else is known
about the entity. Evaluating the whole catalog against a real profile is the
authoritative answer, but there is no profile yet, and there are thirteen entity
types times thirty-six states of them to precompute.

``possible_for`` is the cheap, *sound* half of that: it returns ``FALSE`` only
when no completion of ``known`` could ever make the rule true. Never wrongly
excludes; happily includes plenty a fuller profile will later rule out. That is
the correct bias for a filter — a definition wrongly shown is a moment's
confusion, a definition wrongly hidden is a missed filing.

**Why this is not just ``evaluate`` against a small fact dict.** Two of the
evaluator's deliberate design decisions are exactly wrong for this question:

* ``exists`` is documented as "the only operator that is never UNKNOWN" — absence
  *is* the answer. Correct when the profile is complete; catastrophic here.
  ``catalog/definitions/gst/gstr8.yaml`` reads ``{fact: sector, op: exists}``
  inside an ``all``, so a probe using ``evaluate`` would find GSTR-8 FALSE for
  every entity type and hide it from the catalog permanently.
* An empty collection is definite absence. ``registrations`` is read by most of
  the catalog as ``includes GST``; a probe supplying ``registrations: []`` — which
  is what reusing ``EntityProfileView`` would do, since it always sets the key —
  turns the majority of the catalog FALSE at once and produces an index that
  looks plausible and is nearly empty.

So the rule here is simpler and stricter: a leaf resting on a fact outside
``known`` is UNKNOWN, always, whatever the operator. Everything else is the
evaluator's own comparison, unchanged, so the two cannot disagree about a fact
that is actually present.

The soundness property — ``possible_for(rule, known) is FALSE`` implies
``evaluate(rule, facts) is FALSE`` for every ``facts`` agreeing with ``known`` —
is asserted in the tests over the persona library. If it ever fails, the index is
hiding a real obligation and the build must go red.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from stacos.engine.rules import V, _compare, k_all, k_any, k_not

__all__ = ["possible_for"]

#: Depth guard mirroring the evaluator's. A rule deeper than this cannot have
#: been loaded, so reaching it means the caller built a node by hand.
_MAX_DEPTH = 8


def possible_for(rule: Mapping[str, Any], known: Mapping[str, Any]) -> V:
    """Whether ``rule`` can still be TRUE for some completion of ``known``.

    ``FALSE`` means *refutable*: the facts already given rule it out, and no
    further information can bring it back. ``TRUE`` means every clause was
    decided affirmatively by what is already known. ``UNKNOWN`` — the common
    answer — means it depends on something not yet asked.
    """
    if not rule:
        # An unconditional definition applies to everyone, and always could.
        return V.TRUE
    return _probe_node(rule, known, depth=0)


def _probe_node(node: Any, known: Mapping[str, Any], *, depth: int) -> V:
    if depth > _MAX_DEPTH or not isinstance(node, Mapping):
        return V.UNKNOWN

    if "const" in node:
        raw = node["const"]
        if raw is True:
            return V.TRUE
        if raw is False:
            return V.FALSE
        return V.UNKNOWN

    if "all" in node:
        return k_all(_probe_children(node["all"], known, depth=depth))
    if "any" in node:
        return k_any(_probe_children(node["any"], known, depth=depth))
    if "none" in node:
        return k_not(k_any(_probe_children(node["none"], known, depth=depth)))
    if "not" in node:
        return k_not(_probe_node(node["not"], known, depth=depth + 1))

    fact = node.get("fact")
    if not isinstance(fact, str):
        return V.UNKNOWN

    # The load-bearing line. A fact nobody has supplied leaves the clause open,
    # whatever the operator — including `exists`, which the evaluator treats as
    # decisive and which must not be decisive while the profile is still being
    # filled in.
    if fact not in known:
        return V.UNKNOWN

    return _compare(str(node.get("op", "eq")), known[fact], node.get("value"))


def _probe_children(children: Any, known: Mapping[str, Any], *, depth: int) -> Sequence[V]:
    if not isinstance(children, list | tuple):
        return (V.UNKNOWN,)
    return [_probe_node(child, known, depth=depth + 1) for child in children]
