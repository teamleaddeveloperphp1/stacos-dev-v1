"""
Refutability, and the one property the whole index rests on.

``possible_for`` exists so that "pick a state and a legal form, see what applies"
is one indexed query instead of a rule evaluation per definition. That is only
safe if it never rules something out that a fuller profile would have allowed —
if it ever does, the catalog browser silently hides an obligation and a client
misses a filing they were never shown.

So the headline test is not an example. It is a property, checked against the
real 248-definition catalog and the whole persona library:

    possible_for(rule, known) is FALSE  ⟹  evaluate(rule, facts) is FALSE
    for every facts that agrees with known.

The two regression tests beneath it pin the specific traps that make the obvious
implementation — ``evaluate`` against a small fact dict — wrong.
"""

from __future__ import annotations

import pytest

from stacos.catalog.loader import iter_documents, parse_document
from stacos.catalog.personas import PERSONAS
from stacos.engine.probe import possible_for
from stacos.engine.rules import V, evaluate
from stacos.jurisdictions.facts import ENTITY_TYPES


@pytest.fixture(scope="module")
def documents() -> list:
    return [parse_document(path, raw) for path, raw in iter_documents()]


def test_refuting_implies_evaluating_false(documents: list) -> None:
    """The soundness property. If this fails, the index is hiding an obligation."""
    profiles = [(persona, persona.to_profile().facts) for persona in PERSONAS]

    for document in documents:
        rule = document.applicability_rule
        if not rule:
            continue
        for persona, facts in profiles:
            known = {"country": "IN", "entity_type": persona.entity_type}
            if possible_for(rule, known) is not V.FALSE:
                continue
            # The probe says no completion of (country, entity_type) can make this
            # true. The full evaluation, which knows everything, must agree.
            assert evaluate(rule, facts).result is V.FALSE, (
                f"{document.code} was refuted for {persona.entity_type} but is not "
                f"FALSE for persona {persona.key}. The probe is unsound and the "
                f"catalog browser would hide this definition."
            )


def test_no_definition_is_invisible(documents: list) -> None:
    """Every definition must be possible for at least one legal form.

    A definition possible for none would never appear in the browser or in the
    onboarding preview, whatever the user selected — the exact failure mode the
    dead-rule check catches for applicability, applied to the index.
    """
    invisible = [
        document.code
        for document in documents
        if not any(
            possible_for(document.applicability_rule, {"country": "IN", "entity_type": entity_type})
            is not V.FALSE
            for entity_type in ENTITY_TYPES
        )
    ]
    assert invisible == []


def test_exists_is_not_decisive_while_the_profile_is_incomplete() -> None:
    """The trap that ``evaluate`` would fall into.

    ``_compare`` documents ``exists`` as "the only operator that is never
    UNKNOWN" — absence *is* the answer. Correct against a finished profile;
    catastrophic against a two-key probe. ``gstr8.yaml`` reads ``sector exists``
    inside an ``all``, so an ``evaluate``-based probe marks GSTR-8 FALSE for every
    entity type and it disappears from the catalog permanently.
    """
    rule = {"all": [{"fact": "sector", "op": "exists", "value": True}]}
    known = {"country": "IN", "entity_type": "PVT_LTD"}

    assert evaluate(rule, known).result is V.FALSE
    assert possible_for(rule, known) is V.UNKNOWN


def test_an_absent_collection_is_not_definite_absence() -> None:
    """The second trap, and the one most likely to be reintroduced.

    Most of the catalog reads ``registrations includes GST``. Building the probe
    from an ``EntityProfileView`` — which always sets ``registrations``, even to
    the empty list — turns the majority of the catalog FALSE at once and produces
    an index that looks plausible and is nearly empty.
    """
    rule = {"all": [{"fact": "registrations", "op": "includes", "value": "GST"}]}

    assert evaluate(rule, {"registrations": []}).result is V.FALSE
    assert possible_for(rule, {"country": "IN", "entity_type": "PVT_LTD"}) is V.UNKNOWN


def test_entity_type_still_refutes(documents: list) -> None:
    """The probe has to be useful as well as safe."""
    aoc4 = next(d for d in documents if d.code == "IN-MCA-AOC4")
    llp8 = next(d for d in documents if d.code == "IN-MCA-LLP-FORM8")

    assert aoc4.possible_entity_types == ["PVT_LTD", "PUBLIC_LTD", "OPC", "SECTION_8"]
    assert llp8.possible_entity_types == ["LLP"]


def test_an_empty_rule_is_possible_for_everyone() -> None:
    assert possible_for({}, {}) is V.TRUE


def test_the_index_is_advisory_only() -> None:
    """The name must not appear anywhere in the materialisation path.

    ``possible_entity_types`` is a navigation filter. If the planner ever reads
    it, a stale index becomes a wrong calendar, and the whole reason it is safe to
    compute cheaply disappears.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "stacos"
    offenders = [
        path.relative_to(root).as_posix()
        for path in [*(root / "engine").rglob("*.py"), root / "obligations" / "services.py"]
        if "possible_entity_types" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{offenders} reads the advisory probe index. It is a filter, not an "
        f"authority — applicability is decided by evaluate() against the real "
        f"profile."
    )
