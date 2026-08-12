"""
The applicability rule language.

Rules are JSON predicates evaluated against a dictionary of facts about an
entity. Nothing about a jurisdiction, an industry or a threshold appears in this
file — it is only the interpreter. That is what lets a platform administrator add
a state or a sector without a deploy.

**Three-valued logic is the point.** A missing fact yields ``UNKNOWN``, never
``FALSE``. Treating "we haven't asked yet" as "doesn't apply" is the failure mode
that quietly drops an obligation and gets a client penalised — so a rule that
cannot be decided produces an obligation flagged *unconfirmed*, not silence.

**Explainability is a requirement.** The evaluator returns not just an answer but
the minimal reason for it, so the interface can say "applicable because you are
GST-registered with turnover above ₹5 Cr" instead of asserting it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any

from stacos.engine.types import to_decimal

__all__ = [
    "OPERATORS",
    "RuleError",
    "V",
    "Verdict",
    "evaluate",
    "explain",
    "facts_used",
    "validate_rule",
]


class V(StrEnum):
    """Kleene three-valued logic."""

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


def k_all(values: Sequence[V]) -> V:
    """Conjunction. FALSE dominates, then UNKNOWN.

    FALSE winning over UNKNOWN is deliberate and load-bearing: if an entity is
    definitively not GST-registered, no amount of unknown turnover can make
    GSTR-3B apply. Most rules therefore resolve despite missing facts, which is
    what keeps onboarding from becoming an interrogation.
    """
    if V.FALSE in values:
        return V.FALSE
    if V.UNKNOWN in values:
        return V.UNKNOWN
    return V.TRUE


def k_any(values: Sequence[V]) -> V:
    """Disjunction. TRUE dominates, then UNKNOWN."""
    if V.TRUE in values:
        return V.TRUE
    if V.UNKNOWN in values:
        return V.UNKNOWN
    return V.FALSE


def k_not(value: V) -> V:
    return {V.TRUE: V.FALSE, V.FALSE: V.TRUE, V.UNKNOWN: V.UNKNOWN}[value]


# ---------------------------------------------------------------------------
# Trace and verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TraceNode:
    """One node of the evaluation, retained so the answer can be explained."""

    result: V
    kind: str  # "leaf" | "all" | "any" | "none" | "not" | "const"
    fact: str = ""
    op: str = ""
    expected: Any = None
    actual: Any = None
    explain: str = ""
    children: tuple[TraceNode, ...] = ()


@dataclass(frozen=True, slots=True)
class Verdict:
    result: V
    trace: TraceNode
    #: Facts that were absent *and* would have changed the answer. These become
    #: the onboarding question queue, ranked by how many definitions each unlocks.
    missing_facts: frozenset[str] = field(default_factory=frozenset)

    @property
    def applicable(self) -> bool:
        return self.result is V.TRUE

    @property
    def confirmed(self) -> bool:
        return self.result is not V.UNKNOWN

    def reasons(self) -> list[str]:
        return explain(self.trace)


class RuleError(ValueError):
    """A rule that is malformed, at authoring time rather than evaluation time."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        super().__init__(f"{path}: {message}")


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


def _as_set(value: Any) -> set[Any] | None:
    if isinstance(value, list | tuple | set | frozenset):
        return set(value)
    return None


def _order(op: str, actual: Any, expected: Any) -> V:
    """Ordering comparison over numbers, or over ISO date strings.

    ISO-8601 dates sort correctly as strings, which is why a date fact can be
    compared without the engine needing a date type at the rule layer.
    """
    left = to_decimal(actual)
    right = to_decimal(expected)

    if left is None or right is None:
        if not (isinstance(actual, str) and isinstance(expected, str)):
            return V.UNKNOWN
        matched = {
            "gt": actual > expected,
            "gte": actual >= expected,
            "lt": actual < expected,
            "lte": actual <= expected,
        }[op]
        return V.TRUE if matched else V.FALSE

    matched = {
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
    }[op]
    return V.TRUE if matched else V.FALSE


def _compare(op: str, actual: Any, expected: Any) -> V:
    """Apply one operator, returning UNKNOWN rather than guessing."""
    if op == "exists":
        # The only operator that is never UNKNOWN: absence is itself the answer.
        present = actual is not None and actual != ""
        return V.TRUE if present == bool(expected) else V.FALSE

    if actual is None:
        return V.UNKNOWN

    match op:
        case "eq":
            return V.TRUE if actual == expected else V.FALSE
        case "ne":
            return V.TRUE if actual != expected else V.FALSE
        case "gt" | "gte" | "lt" | "lte":
            return _order(op, actual, expected)
        case "in":
            options = _as_set(expected) or set()
            return V.TRUE if actual in options else V.FALSE
        case "not_in":
            options = _as_set(expected) or set()
            return V.TRUE if actual not in options else V.FALSE
        case "includes":
            held = _as_set(actual)
            if held is None:
                return V.UNKNOWN
            wanted = _as_set(expected) or {expected}
            return V.TRUE if wanted <= held else V.FALSE
        case "excludes":
            held = _as_set(actual)
            if held is None:
                return V.UNKNOWN
            wanted = _as_set(expected) or {expected}
            return V.TRUE if not (wanted & held) else V.FALSE
        case "between":
            if not isinstance(expected, list | tuple) or len(expected) != 2:
                return V.UNKNOWN
            value = to_decimal(actual)
            low, high = to_decimal(expected[0]), to_decimal(expected[1])
            if value is None or low is None or high is None:
                return V.UNKNOWN
            return V.TRUE if low <= value <= high else V.FALSE
        case "count_gte" | "count_lte":
            members = _as_set(actual)
            threshold = to_decimal(expected)
            if members is None or threshold is None:
                return V.UNKNOWN
            size = Decimal(len(members))
            matched = size >= threshold if op == "count_gte" else size <= threshold
            return V.TRUE if matched else V.FALSE
        case "matches":
            if not isinstance(actual, str) or not isinstance(expected, str):
                return V.UNKNOWN
            return V.TRUE if re.search(expected, actual) else V.FALSE

    raise RuleError("", f"unknown operator {op!r}")


OPERATORS: frozenset[str] = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "in",
        "not_in",
        "includes",
        "excludes",
        "between",
        "exists",
        "matches",
        "count_gte",
        "count_lte",
    }
)

COMBINATORS: frozenset[str] = frozenset({"all", "any", "none", "not"})

#: Guards against a runaway rule from the admin editor, and against rules nobody
#: can read.
MAX_DEPTH = 8
MAX_NODES = 64


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate(rule: Mapping[str, Any], facts: Mapping[str, Any]) -> Verdict:
    """Evaluate a rule, returning the answer, a trace and the decisive gaps.

    An empty rule is applicable to everyone — that is how an obligation with no
    conditions (every company files an annual return) is expressed.
    """
    if not rule:
        return Verdict(V.TRUE, TraceNode(V.TRUE, "const", explain="applies to everyone"))

    missing: set[str] = set()
    trace = _evaluate_node(rule, facts, missing, depth=0)
    return Verdict(trace.result, trace, frozenset(missing))


def _evaluate_node(
    node: Mapping[str, Any],
    facts: Mapping[str, Any],
    missing: set[str],
    depth: int,
) -> TraceNode:
    if depth > MAX_DEPTH:
        raise RuleError("", f"rule nested deeper than {MAX_DEPTH} levels")

    if "const" in node:
        literal = node["const"]
        result = V.UNKNOWN if literal == "unknown" else (V.TRUE if literal else V.FALSE)
        return TraceNode(result, "const", explain=str(node.get("explain", "")))

    for combinator in ("all", "any", "none"):
        if combinator in node:
            children = tuple(
                _evaluate_node(child, facts, missing, depth + 1) for child in node[combinator]
            )
            values = [child.result for child in children]
            if combinator == "all":
                result = k_all(values)
            elif combinator == "any":
                result = k_any(values)
            else:
                result = k_not(k_any(values))
            return TraceNode(
                result, combinator, explain=str(node.get("explain", "")), children=children
            )

    if "not" in node:
        child = _evaluate_node(node["not"], facts, missing, depth + 1)
        return TraceNode(
            k_not(child.result), "not", explain=str(node.get("explain", "")), children=(child,)
        )

    # Leaf.
    fact_name = node.get("fact")
    if not fact_name:
        raise RuleError("", f"node has neither a combinator nor a fact: {node!r}")

    op = node.get("op", "eq")
    expected = node.get("value")
    actual = facts.get(fact_name)

    result = _compare(op, actual, expected)
    if result is V.UNKNOWN:
        missing.add(str(fact_name))

    return TraceNode(
        result,
        "leaf",
        fact=str(fact_name),
        op=str(op),
        expected=expected,
        actual=actual,
        explain=str(node.get("explain", "")),
    )


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------


def prune(node: TraceNode) -> TraceNode:
    """Reduce a trace to the *minimal sufficient reason* for its result.

    A user asking "why does this apply to me?" wants one sentence, not the whole
    decision tree. For an ``all`` that failed, the first failing clause is the
    reason; for one that passed, every clause is.
    """
    if node.kind in {"leaf", "const"}:
        return node

    children = list(node.children)
    if node.kind == "all":
        keep = (
            children if node.result is V.TRUE else [c for c in children if c.result is V.FALSE][:1]
        )
    elif node.kind == "any":
        keep = (
            [c for c in children if c.result is V.TRUE][:1] if node.result is V.TRUE else children
        )
    elif node.kind == "none":
        keep = (
            [c for c in children if c.result is V.TRUE][:1] if node.result is V.FALSE else children
        )
    else:
        keep = children

    return replace(node, children=tuple(prune(c) for c in keep if c is not None))


def explain(trace: TraceNode) -> list[str]:
    """Human-readable reasons for the result, most specific first."""
    return _collect(prune(trace))


def _collect(node: TraceNode) -> list[str]:
    if node.kind in {"leaf", "const"}:
        sentence = node.explain or _describe(node)
        return [sentence] if sentence else []

    reasons: list[str] = []
    if node.explain:
        reasons.append(node.explain)
    for child in node.children:
        reasons.extend(_collect(child))
    return reasons


_OP_PHRASES = {
    "eq": "is",
    "ne": "is not",
    "gt": "is above",
    "gte": "is at least",
    "lt": "is below",
    "lte": "is at most",
    "in": "is one of",
    "not_in": "is not one of",
    "includes": "includes",
    "excludes": "does not include",
    "between": "is between",
    "exists": "is recorded",
    "count_gte": "has at least",
    "count_lte": "has at most",
    "matches": "matches",
}


def _describe(node: TraceNode) -> str:
    """Fallback phrasing when an author supplied no ``explain``.

    Readable but plainly generated — which is itself useful, because it makes the
    definitions still needing a hand-written explanation easy to spot.
    """
    label = node.fact.replace("_", " ")
    phrase = _OP_PHRASES.get(node.op, node.op)
    if node.op == "exists":
        return f"{label} {phrase}"
    return f"{label} {phrase} {_format(node.expected)}"


def _format(value: Any) -> str:
    if isinstance(value, list | tuple | set | frozenset):
        return ", ".join(str(v) for v in value)
    return str(value)


# ---------------------------------------------------------------------------
# Authoring-time validation
# ---------------------------------------------------------------------------


def validate_rule(
    rule: Mapping[str, Any],
    *,
    known_facts: Iterable[str] | None = None,
    path: str = "",
    depth: int = 0,
    counter: list[int] | None = None,
) -> list[RuleError]:
    """Check a rule before it is stored.

    Returns every problem rather than the first, because an author fixing a rule
    wants the whole list. Validation is separate from evaluation on purpose: an
    unknown fact is a *bug in the rule* at authoring time, but merely an unknown
    value at evaluation time.
    """
    errors: list[RuleError] = []
    counter = counter if counter is not None else [0]
    counter[0] += 1

    if depth > MAX_DEPTH:
        return [RuleError(path or "/", f"nested deeper than {MAX_DEPTH} levels")]
    if counter[0] > MAX_NODES:
        return [RuleError(path or "/", f"more than {MAX_NODES} nodes")]
    if not isinstance(rule, Mapping):
        return [RuleError(path or "/", "each node must be an object")]
    if not rule:
        return errors

    if "const" in rule:
        if rule["const"] not in (True, False, "unknown"):
            errors.append(RuleError(path, "const must be true, false or 'unknown'"))
        return errors

    for combinator in ("all", "any", "none"):
        if combinator in rule:
            children = rule[combinator]
            if not isinstance(children, list) or not children:
                errors.append(RuleError(f"{path}/{combinator}", "must be a non-empty list"))
                return errors
            for index, child in enumerate(children):
                errors.extend(
                    validate_rule(
                        child,
                        known_facts=known_facts,
                        path=f"{path}/{combinator}/{index}",
                        depth=depth + 1,
                        counter=counter,
                    )
                )
            return errors

    if "not" in rule:
        return validate_rule(
            rule["not"],
            known_facts=known_facts,
            path=f"{path}/not",
            depth=depth + 1,
            counter=counter,
        )

    fact_name = rule.get("fact")
    if not fact_name:
        errors.append(RuleError(path or "/", "node has neither a combinator nor a fact"))
        return errors

    if known_facts is not None and fact_name not in set(known_facts):
        errors.append(
            RuleError(f"{path}/fact", f"unknown fact {fact_name!r} — is it in the fact registry?")
        )

    op = rule.get("op", "eq")
    if op not in OPERATORS:
        errors.append(
            RuleError(f"{path}/op", f"unknown operator {op!r}; expected one of {sorted(OPERATORS)}")
        )
        return errors

    if op != "exists" and "value" not in rule:
        errors.append(RuleError(f"{path}/value", f"operator {op!r} requires a value"))

    if op == "between":
        value = rule.get("value")
        if not isinstance(value, list | tuple) or len(value) != 2:
            errors.append(RuleError(f"{path}/value", "between requires a two-element [low, high]"))
        else:
            low, high = to_decimal(value[0]), to_decimal(value[1])
            if low is not None and high is not None and low > high:
                errors.append(RuleError(f"{path}/value", "between requires low <= high"))

    if op == "matches":
        try:
            re.compile(str(rule.get("value", "")))
        except re.error as exc:
            errors.append(RuleError(f"{path}/value", f"invalid regular expression: {exc}"))

    return errors


def facts_used(rule: Mapping[str, Any]) -> frozenset[str]:
    """Every fact a rule reads.

    Stored alongside the definition so a profile edit can re-evaluate only the
    definitions it could possibly have affected, instead of the whole catalog.
    """
    found: set[str] = set()
    _walk_facts(rule, found)
    return frozenset(found)


def _walk_facts(node: Any, found: set[str]) -> None:
    if not isinstance(node, Mapping):
        return
    if "fact" in node:
        found.add(str(node["fact"]))
    for combinator in ("all", "any", "none"):
        for child in node.get(combinator, []) or []:
            _walk_facts(child, found)
    if "not" in node:
        _walk_facts(node["not"], found)
