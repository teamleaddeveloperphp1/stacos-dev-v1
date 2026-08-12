"""
The compliance engine. **Pure Python — no Django, no ORM, no clock.**

This package answers the question the whole product is built around: given what
we know about an entity, which obligations apply to it, and when is each one due?

Three constraints, enforced by tests rather than convention:

* **No framework imports.** Django models, Celery and psycopg are all absent, so
  the engine can be reasoned about and tested without a database.
* **No ambient time.** Every entry point takes ``as_of`` explicitly. A function
  that reads the clock cannot be covered by a golden-file test, and golden files
  are the only practical way to assert "this entity gets exactly these 47
  obligations".
* **No jurisdiction literals.** No ``if industry == "pharma"``, no April
  financial year. Applicability and due dates are data; this package is only the
  interpreter.
"""

from stacos.engine.types import (
    CalendarSnapshot,
    DefinitionSnapshot,
    DueDateResolution,
    EvidenceRequirement,
    ExtensionRecord,
    ExtensionSet,
    FiscalYearConvention,
    Period,
)

__all__ = [
    "CalendarSnapshot",
    "DefinitionSnapshot",
    "DueDateResolution",
    "EvidenceRequirement",
    "ExtensionRecord",
    "ExtensionSet",
    "FiscalYearConvention",
    "Period",
]
