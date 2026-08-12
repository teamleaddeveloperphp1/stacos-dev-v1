"""
The portal adapter interface. **No adapters are implemented, deliberately.**

Automated retrieval of notices from the income tax, GST and MCA portals is the
feature every prospect asks about. It is also the feature that would require
STACOS to hold client portal credentials and drive an authenticated session
against terms of service that do not contemplate a third party doing so. Both are
legal exposures rather than technical ones, and neither is worth taking before
there is a product to protect.

So this file is an interface with nothing behind it, and that is the point. It
costs almost nothing now and it means the decision is reversible: a written legal
position — or, far better, an official API — becomes an adapter registered here
rather than a rewrite of the notices module. Everything downstream already reads
``Notice.source`` and handles ``PORTAL`` without knowing whether anything ever
produces it.

What *is* implemented is the honest half: manual entry, and email ingestion in
:mod:`stacos.notices.ingest`.

Note what this interface deliberately does not have: any way to pass a password.
An adapter that is ever written should authenticate by a token the client granted
through the authority's own consent flow. Designing the credential parameter in
now would make storing credentials the path of least resistance later.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

__all__ = [
    "PortalAdapter",
    "PortalFetchResult",
    "RetrievedNotice",
    "available_adapters",
    "register_adapter",
]


@dataclass(frozen=True, slots=True)
class RetrievedNotice:
    """A notice as an adapter would hand it over.

    Deliberately the same shape as manual entry produces, so the ingestion path
    is identical whether a person typed it or a machine fetched it — and so the
    code that creates a notice has no idea which happened.
    """

    authority_code: str
    reference_number: str
    subject: str
    notice_type: str = "OTHER"
    statutory_reference: str = ""
    summary: str = ""
    issued_on: date | None = None
    received_on: date | None = None
    respond_by: date | None = None
    period_key: str = ""
    demand_amount: str = ""
    #: Bytes of the notice itself, to land in the vault. Empty where the adapter
    #: can see the metadata but not the document.
    attachments: tuple[tuple[str, bytes], ...] = ()


@dataclass(frozen=True, slots=True)
class PortalFetchResult:
    notices: tuple[RetrievedNotice, ...] = ()
    errors: tuple[str, ...] = ()
    #: True when the adapter could not authenticate at all, which is a different
    #: operational problem from "authenticated and found nothing".
    unauthenticated: bool = False
    diagnostics: dict[str, str] = field(default_factory=dict)


class PortalAdapter(Protocol):
    """What an adapter must provide, if one is ever written.

    ``authority_code`` matches ``jurisdictions.Authority.code``, so the registry
    can answer "can we fetch for this authority" without a lookup table.
    """

    authority_code: str
    #: Shown to the user next to the manual-entry option. An adapter that cannot
    #: explain what it is about to do on the client's behalf should not run.
    description: str

    def fetch(self, *, entity_id: str, since: date | None = None) -> PortalFetchResult:
        """Retrieve notices for one entity. Must not raise for an ordinary failure."""
        ...


_REGISTRY: dict[str, PortalAdapter] = {}


def register_adapter(adapter: PortalAdapter) -> PortalAdapter:
    """Register an adapter. Nothing calls this today.

    Kept so that the first adapter is a registration rather than a refactor, and
    so a test can register a fake one and exercise the ingestion path end to end
    without any real portal existing.
    """
    _REGISTRY[adapter.authority_code] = adapter
    return adapter


def available_adapters(authority_codes: Iterable[str] = ()) -> Sequence[PortalAdapter]:
    """Adapters that could run, filtered to the authorities asked about.

    Returns empty today. The UI reads this to decide whether to offer automated
    retrieval at all, which is why it must be a query rather than a constant —
    "no adapters" and "adapters exist but not for this authority" render
    differently, and neither should be hardcoded in a template.
    """
    if not authority_codes:
        return tuple(_REGISTRY.values())
    wanted = set(authority_codes)
    return tuple(a for code, a in _REGISTRY.items() if code in wanted)


def clear_registry() -> None:  # pragma: no cover - test helper
    _REGISTRY.clear()
