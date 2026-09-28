"""
Learn: the catalog read by legal form, with no entity in the picture.

Pinned here, in the order they would hurt if they broke:

* **It reads no customer data, and nothing reads it.** Asserted structurally —
  the tables its queries touch, the apps it imports, the modules importing it —
  rather than by example, because what it guards against is a future change
  that looks harmless.
* **"Next due" is the real calendar's date.** Checked against the register the
  planner actually builds for a real entity, not only against dates worked out
  by hand, so the two paths cannot drift apart while both stay green.
* **A count is the probe index, and a stale index is not trusted** — including
  the loader fix that lets ``loadcatalog`` repair one.
* **The pages say what they are.** The caveats are load-bearing copy, asserted
  word for word.
"""

from __future__ import annotations

import ast
import html
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from stacos.accounts.models import User
from stacos.catalog import learn, loader
from stacos.catalog.loader import PROBE_SIGNATURE, iter_documents, load_catalog
from stacos.catalog.models import DefinitionVersion, GovernmentExtension, PublicationStatus
from stacos.core.scope import platform_scope
from stacos.core.templatetags.stacos import periodicity_adjective, periodicity_label
from stacos.engine.types import (
    CalendarSnapshot,
    DefinitionSnapshot,
    ExtensionSet,
    FiscalYearConvention,
    Periodicity,
)
from stacos.jurisdictions.facts import ENTITY_TYPES
from stacos.jurisdictions.models import JurisdictionPack
from stacos.obligations import learn_views
from stacos.obligations.queries import live
from stacos.tenancy.forms import ENTITY_TYPE_LABELS
from stacos.tenancy.models import Entity, Tenant
from tests.conftest import _make_member, sign_in

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 8, 12)
HTMX = {"HX-Request": "true"}

NOTICE = (
    "Every rule in the compliance catalog is written once, then checked against a real "
    "business by the rules engine — see a client's own compliance library for "
    "that. This page is the other direction: pick a legal form and see what the catalog "
    "currently carries for it, before any entity exists to check it against.",
    "A count here says a rule cannot be ruled out for this legal form knowing only its "
    "type — it is navigation, not a filing calendar. What actually applies to one "
    "business depends on its registrations, turnover, sector and other facts.",
)
CAVEAT = (
    "This shows what could apply to this legal form under Indian law today — it is "
    "navigation, not a filing calendar. 'Next due' is the standard statutory date for "
    "that filing, not a specific business's deadline. Which of these actually apply to a "
    "given business, and its real due dates, depend on its registrations, turnover, "
    "sector and other facts. Add the entity to see the calendar built for it."
)
STALE = (
    "This summary has not been reviewed against the statute within the last twelve "
    "months. Treat it as a guide and check the reference."
)
PROVISIONAL = (
    "This summary is provisional and has not yet been verified against the statute. "
    "Treat it as a guide and check the reference."
)


@pytest.fixture
def at_as_of(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the views' today, so a rendered date is one a test can name."""
    monkeypatch.setattr(learn_views, "_today", lambda: AS_OF)


def _rows(
    entity_type: str, pack: JurisdictionPack, as_of: date = AS_OF
) -> dict[str, learn.LearnRow]:
    return {row.code: row for row in learn.obligations_for(entity_type, pack=pack, as_of=as_of)}


def _counts(pack: JurisdictionPack) -> dict[str, int]:
    return {
        card.entity_type.code: card.obligation_count
        for card in learn.entity_type_cards(pack, as_of=AS_OF)
    }


def _text(response: object) -> str:
    """The body as a reader sees it — entities decoded, so an apostrophe in copy
    compares as an apostrophe."""
    return html.unescape(response.content.decode())  # type: ignore[attr-defined]


# ===========================================================================
# Legal forms and their counts
# ===========================================================================


def test_every_legal_form_in_the_pack_gets_a_card(india: JurisdictionPack) -> None:
    cards = learn.entity_type_cards(india, as_of=AS_OF)

    assert [card.entity_type.code for card in cards] == [
        item["code"] for item in india.entity_types
    ]
    assert all(card.entity_type.name and card.entity_type.description for card in cards)


def test_the_pack_describes_exactly_the_vocabulary_the_probe_indexes(
    india: JurisdictionPack,
) -> None:
    """A legal form the probe never considers would show "0 obligations" for
    ever; one the pack omits would have no card. And a form is spelled the way
    the Add Entity screen spells it, or the product names one thing two ways."""
    codes = [item["code"] for item in india.entity_types]

    assert sorted(codes) == sorted(ENTITY_TYPES)
    assert len(codes) == len(set(codes))
    for item in india.entity_types:
        assert item["name"] == ENTITY_TYPE_LABELS[item["code"]]
        assert item["description"].strip()


def test_a_count_is_the_probe_index_over_definitions_in_force(india: JurisdictionPack) -> None:
    in_force = DefinitionVersion.objects.filter(
        status=PublicationStatus.PUBLISHED,
        definition__country="IN",
        definition__is_active=True,
        effective_from__lte=AS_OF,
    ).exclude(effective_to__lt=AS_OF)

    expected = {
        entity_type: sum(entity_type in version.possible_entity_types for version in in_force)
        for entity_type in ENTITY_TYPES
    }

    assert _counts(india) == expected
    # The legal form added after the index was first built. It is covered by the
    # nationwide GST rules like everything else, so zero here is the bug.
    assert expected["GOVERNMENT"] > 0


def test_the_list_is_exactly_what_the_count_counted(india: JurisdictionPack) -> None:
    for code, count in _counts(india).items():
        assert len(learn.obligations_for(code, pack=india, as_of=AS_OF)) == count, code


def test_a_legal_form_lists_what_its_type_cannot_rule_out(india: JurisdictionPack) -> None:
    company = _rows("PVT_LTD", india)
    firm = _rows("LLP", india)

    assert "IN-IT-ITR-COMPANY" in company
    assert "IN-IT-ITR-COMPANY" not in firm
    assert "IN-IT-ITR5" in firm
    assert "IN-IT-ITR5" not in company
    # Nationwide and conditional on a registration nobody has asked about yet:
    # it cannot be ruled out for either.
    assert "IN-GST-GSTR3B-MONTHLY" in company
    assert "IN-GST-GSTR3B-MONTHLY" in firm


def test_a_current_index_row_is_read_rather_than_recomputed(india: JurisdictionPack) -> None:
    before = _counts(india)
    DefinitionVersion.objects.filter(definition__code="IN-IT-ITR-COMPANY").update(
        possible_entity_types=["LLP"]
    )

    after = _counts(india)

    assert after["LLP"] == before["LLP"] + 1
    assert after["PVT_LTD"] == before["PVT_LTD"] - 1


def test_a_row_indexed_against_an_older_vocabulary_is_re_probed(india: JurisdictionPack) -> None:
    """The state every database loaded before GOVERNMENT existed was left in:
    each row's list missing the newest type, under an old signature. Trusted,
    it would show GOVERNMENT with nothing at all — and here it would also
    inflate every other type, because the stale list below names them all."""
    fresh = _counts(india)
    DefinitionVersion.objects.update(
        probe_signature="old-vocabulary",
        possible_entity_types=[code for code in ENTITY_TYPES if code != "GOVERNMENT"],
    )

    assert _counts(india) == fresh
    assert "IN-GST-GSTR3B-MONTHLY" in _rows("GOVERNMENT", india)


# ===========================================================================
# The loader fix behind the stale index
# ===========================================================================


def test_a_vocabulary_change_changes_every_checksum(monkeypatch: pytest.MonkeyPatch) -> None:
    _, raw = next(iter(iter_documents()))
    before = loader._checksum(raw)

    monkeypatch.setattr(loader, "PROBE_SIGNATURE", "0" * 16)

    assert loader._checksum(raw) != before


def test_loadcatalog_re_derives_an_index_built_against_an_older_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly what adding GOVERNMENT left behind — the YAML unchanged, so the
    checksum it hashed unchanged too, and every ``loadcatalog`` since reporting
    "unchanged" over an index missing the new type."""
    code = "IN-GST-GSTR3B-MONTHLY"
    _, raw = next((path, raw) for path, raw in iter_documents() if raw.get("code") == code)
    with monkeypatch.context() as earlier:
        earlier.setattr(loader, "PROBE_SIGNATURE", "old-vocabulary-0")
        checksum_then = loader._checksum(raw)

    version = DefinitionVersion.objects.get(
        definition__code=code, status=PublicationStatus.PUBLISHED
    )
    DefinitionVersion.objects.filter(pk=version.pk).update(
        probe_signature="old-vocabulary-0",
        possible_entity_types=[t for t in version.possible_entity_types if t != "GOVERNMENT"],
        source_checksum=checksum_then,
    )

    report = load_catalog()

    assert report.ok, report.errors
    assert code in report.updated
    version.refresh_from_db()
    assert version.probe_signature == PROBE_SIGNATURE
    assert "GOVERNMENT" in version.possible_entity_types


# ===========================================================================
# Next due
# ===========================================================================


@pytest.mark.parametrize(
    ("entity_type", "code", "expected"),
    [
        # July's return, due the 20th of the following month.
        ("PVT_LTD", "IN-GST-GSTR3B-MONTHLY", date(2026, 8, 20)),
        # FY2025-26, due 31 October: a period that closed in March, due after today.
        ("PVT_LTD", "IN-IT-ITR-COMPANY", date(2026, 10, 31)),
        # Q1 was due 31 July and has passed, so the next is Q2's.
        ("PVT_LTD", "IN-TDS-24Q", date(2026, 10, 31)),
        # 31 July 2026 has passed, so it is next year's.
        ("LLP", "IN-IT-ITR-NON-AUDIT", date(2027, 7, 31)),
        # Twenty-three months after the year end: FY2024-25's, not FY2025-26's.
        ("PVT_LTD", "IN-IT-ITRU", date(2027, 2, 28)),
    ],
)
def test_next_due_is_the_soonest_date_not_yet_passed(
    india: JurisdictionPack, entity_type: str, code: str, expected: date
) -> None:
    row = _rows(entity_type, india)[code]

    assert row.next_due is not None
    assert row.next_due.effective_date == expected


def test_next_due_is_the_date_the_real_calendar_computes(
    india: JurisdictionPack, materialised: Entity
) -> None:
    """The column's whole claim, checked against the register itself: for every
    rule a real private company's calendar carries, the soonest open date the
    planner wrote down is the date Learn shows."""
    with platform_scope(reason="test"):
        upcoming: dict[str, date] = {}
        for instance in live().filter(entity=materialised, due_date__gte=AS_OF):
            known = upcoming.get(instance.definition_code)
            if known is None or instance.due_date < known:
                upcoming[instance.definition_code] = instance.due_date

    rows = _rows("PVT_LTD", india)
    compared = sorted(code for code in upcoming if code in rows)

    assert len(compared) >= 4, compared
    for code in compared:
        next_due = rows[code].next_due
        assert next_due is not None, code
        assert next_due.effective_date == upcoming[code], code


def test_the_next_period_is_dated_by_its_own_version_not_today_s(india: JurisdictionPack) -> None:
    """The planner is handed every version, and so is this. Here the version in
    force today moves the due day to the 25th from August — but July's return is
    still the old rule's, due 20 August, and that has not passed yet. Given only
    today's version, July has no rule at all and the page would skip to 25
    September."""
    code = "IN-GST-GSTR3B-MONTHLY"
    first = DefinitionVersion.objects.get(definition__code=code, version=1)
    DefinitionVersion.objects.filter(pk=first.pk).update(effective_to=date(2026, 7, 31))
    DefinitionVersion.objects.create(
        definition=first.definition,
        version=2,
        status=PublicationStatus.PUBLISHED,
        title=first.title,
        periodicity=first.periodicity,
        applicability_rule=first.applicability_rule,
        possible_entity_types=first.possible_entity_types,
        probe_signature=PROBE_SIGNATURE,
        instance_scope=first.instance_scope,
        scope_selector=first.scope_selector,
        due_rule={**first.due_rule, "offset": {"months": 1, "day_of_month": 25}},
        effective_from=date(2026, 8, 1),
    )

    row = _rows("PVT_LTD", india, as_of=date(2026, 8, 15))[code]

    assert row.version.version == 2
    assert row.next_due is not None
    assert row.next_due.effective_date == date(2026, 8, 20)


def _extension(**overrides: object) -> GovernmentExtension:
    fields: dict[str, object] = {
        "definition_code": "IN-GST-GSTR3B-MONTHLY",
        "kind": GovernmentExtension.Kind.EXTENSION,
        "notification_reference": "Notification 99/2026 – Central Tax",
        "period_key": "2026-07",
        "new_due_date": date(2026, 8, 27),
        "published_at": date(2026, 8, 1),
    }
    fields.update(overrides)
    return GovernmentExtension.objects.create(**fields)


def test_a_published_extension_moves_next_due(india: JurisdictionPack) -> None:
    _extension()

    row = _rows("PVT_LTD", india)["IN-GST-GSTR3B-MONTHLY"]

    assert row.next_due is not None
    assert row.next_due.effective_date == date(2026, 8, 27)
    assert row.next_due.original_date == date(2026, 8, 20)
    assert row.notification == "Notification 99/2026 – Central Tax"


def test_an_extension_keeps_a_passed_statutory_date_current(india: JurisdictionPack) -> None:
    """The 20th has gone by, but the notification moved it to the 27th: the next
    date is the moved one, not September's."""
    _extension()

    row = _rows("PVT_LTD", india, as_of=date(2026, 8, 22))["IN-GST-GSTR3B-MONTHLY"]

    assert row.next_due is not None
    assert row.next_due.effective_date == date(2026, 8, 27)


def test_relief_granted_to_one_state_is_not_the_standard_date(india: JurisdictionPack) -> None:
    _extension(jurisdictions=["IN-MH"])

    row = _rows("PVT_LTD", india)["IN-GST-GSTR3B-MONTHLY"]

    assert row.next_due is not None
    assert row.next_due.effective_date == date(2026, 8, 20)
    assert row.notification == ""


def test_the_holiday_calendar_is_the_pack_s_own(india: JurisdictionPack) -> None:
    """A rule that shifts off a non-working day is shifted by the pack's own
    holidays, loaded once for the list — September's date here is 2 October,
    Gandhi Jayanti in IN.yaml, and Saturday is a working day."""
    shifting = DefinitionSnapshot(
        code="TEST-SHIFTING",
        version=1,
        title="A physically-attended filing",
        country="IN",
        periodicity=Periodicity.MONTHLY,
        due_rule={
            "anchor": "PERIOD_END",
            "offset": {"months": 1, "day_of_month": 2},
            "shift_if_holiday": "NEXT_WORKING_DAY",
            "calendars": ["IN-NATIONAL"],
        },
    )

    due = learn.next_due_dates({"TEST-SHIFTING": [shifting]}, pack=india, as_of=date(2026, 9, 5))

    assert due["TEST-SHIFTING"].effective_date == date(2026, 10, 3)


FY = FiscalYearConvention(4, 1, "FY{start_year}-{end_year_short}")
MONTHLY_20TH = {
    "anchor": "PERIOD_END",
    "offset": {"months": 1, "day_of_month": 20},
    "shift_if_holiday": "NONE",
}


def _snapshot(**overrides: object) -> DefinitionSnapshot:
    fields: dict[str, object] = {
        "code": "TEST",
        "version": 1,
        "title": "Test",
        "country": "IN",
        "periodicity": Periodicity.MONTHLY,
        "due_rule": MONTHLY_20TH,
    }
    fields.update(overrides)
    return DefinitionSnapshot(**fields)  # type: ignore[arg-type]


def _next_due(*versions: DefinitionSnapshot, as_of: date) -> date | None:
    resolution = learn.next_due(
        versions,
        as_of=as_of,
        window_start=as_of - timedelta(days=800),
        window_end=as_of + timedelta(days=800),
        fy=FY,
        calendars=CalendarSnapshot(),
        extensions=ExtensionSet(),
    )
    return resolution.effective_date if resolution else None


def test_a_date_falling_today_is_still_next() -> None:
    assert _next_due(_snapshot(), as_of=date(2026, 8, 20)) == date(2026, 8, 20)
    assert _next_due(_snapshot(), as_of=date(2026, 8, 21)) == date(2026, 9, 20)


@pytest.mark.parametrize(
    "rule",
    [
        _snapshot(
            periodicity=Periodicity.ANNUAL,
            due_rule={
                "anchor": "EVENT_DATE",
                "event_key": "AGM_HELD",
                "offset": {"days": 30},
                "shift_if_holiday": "NONE",
            },
        ),
        _snapshot(
            periodicity=Periodicity.ANNUAL,
            due_rule={
                "anchor": "LICENCE_EXPIRY",
                "offset": {"days": -30},
                "shift_if_holiday": "NONE",
            },
        ),
        _snapshot(
            periodicity=Periodicity.EVENT_BASED,
            due_rule={"anchor": "TRIGGER_DATE", "offset": {"days": 30}, "shift_if_holiday": "NONE"},
            trigger={"event_key": "DIRECTOR_APPOINTED"},
        ),
        # Its one "period" would be the generation window, so any date resolved
        # from it would be an artefact of this module rather than the statute.
        _snapshot(periodicity=Periodicity.ONE_TIME),
    ],
    ids=["event-anchored", "licence-renewal", "event-triggered", "one-time"],
)
def test_a_rule_that_waits_on_something_happening_has_no_standard_date(
    rule: DefinitionSnapshot,
) -> None:
    assert _next_due(rule, as_of=AS_OF) is None


def test_each_period_is_dated_by_the_version_governing_it() -> None:
    """September is the old rule's and its date has passed; October is the new
    rule's. Applied to every period regardless, the new rule would date
    September too, and the page would say 25 October."""
    old = _snapshot(version=1, effective_to=date(2026, 9, 30))
    new = _snapshot(
        version=2,
        effective_from=date(2026, 10, 1),
        due_rule={**MONTHLY_20TH, "offset": {"months": 1, "day_of_month": 25}},
    )

    assert _next_due(old, new, as_of=date(2026, 10, 21)) == date(2026, 11, 25)


# ===========================================================================
# Structural guarantees
# ===========================================================================

_TABLE = re.compile(r'\b(?:FROM|JOIN|UPDATE|INTO)\s+"(\w+)"', re.IGNORECASE)


def test_learn_reads_only_platform_reference_data(
    india: JurisdictionPack, materialised: Entity
) -> None:
    """With a real client's calendar sitting in the database, none of it is read
    — and nothing is written. No tenant scope is bound here either, so a query
    against a tenant-owned model would raise rather than pass."""
    with CaptureQueriesContext(connection) as queries:
        pack = learn.pack_for("IN")
        assert pack is not None
        for card in learn.entity_type_cards(pack, as_of=AS_OF):
            learn.obligations_for(card.entity_type.code, pack=pack, as_of=AS_OF)

    statements = [query["sql"] for query in queries.captured_queries]
    tables = {table for sql in statements for table in _TABLE.findall(sql)}

    assert statements
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert tables
    assert all(table.startswith(("catalog_", "jurisdictions_")) for table in tables), tables


STACOS = Path(learn.__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_learn_imports_nothing_that_owns_customer_data() -> None:
    reached = {
        name for name in _imports(STACOS / "catalog" / "learn.py") if name.startswith("stacos.")
    }
    offenders = sorted(
        name
        for name in reached
        if not name.startswith(("stacos.catalog", "stacos.engine", "stacos.jurisdictions"))
    )

    assert offenders == [], (
        f"stacos.catalog.learn imports {offenders}. It reads platform reference data "
        f"only; a tenant-owned app in reach is a customer's data in reach."
    )


def test_only_the_learn_pages_read_learn() -> None:
    """One-way. The planner, the nightly rebuild, the library and every other
    per-customer path compute their own dates; none may lean on this."""
    importers = sorted(
        path.relative_to(STACOS).as_posix()
        for path in STACOS.rglob("*.py")
        if "migrations" not in path.parts
        and path != STACOS / "catalog" / "learn.py"
        and "stacos.catalog.learn" in _imports(path)
    )

    assert importers == ["obligations/learn_views.py"]


def test_the_list_costs_the_same_however_long_it_is(india: JurisdictionPack) -> None:
    measured: dict[str, tuple[int, int]] = {}
    for entity_type in ("PVT_LTD", "LLP"):
        with CaptureQueriesContext(connection) as queries:
            rows = learn.obligations_for(entity_type, pack=india, as_of=AS_OF)
        measured[entity_type] = (len(rows), len(queries))

    assert measured["PVT_LTD"][0] != measured["LLP"][0]
    assert measured["PVT_LTD"][1] == measured["LLP"][1] <= 6

    with CaptureQueriesContext(connection) as queries:
        learn.entity_type_cards(india, as_of=AS_OF)
    assert len(queries) == 1


# ===========================================================================
# The pages
# ===========================================================================


def test_both_pages_render_both_ways(signed_in: Client, at_as_of: None) -> None:
    for url in (reverse("compliance:learn"), reverse("compliance:learn_type", args=["PVT_LTD"])):
        page = signed_in.get(url)
        fragment = signed_in.get(url, headers=HTMX)

        assert page.status_code == 200, url
        assert fragment.status_code == 200, url
        assert b"<!doctype html>" in page.content.lower()
        assert b"<!doctype html>" not in fragment.content.lower()
        assert b'class="app-shell"' not in fragment.content


def test_the_index_says_what_its_counts_are(
    signed_in: Client, india: JurisdictionPack, at_as_of: None
) -> None:
    body = _text(signed_in.get(reverse("compliance:learn")))

    for paragraph in NOTICE:
        assert paragraph in body
    assert "Private Limited Company" in body
    assert india.entity_types[0]["description"] in body
    assert f"{_counts(india)['PVT_LTD']} obligations in the catalog" in body
    assert f'href="{reverse("compliance:learn_type", args=["GOVERNMENT"])}"' in body


def test_a_legal_form_s_page_carries_its_caveat_groups_and_dates(
    signed_in: Client, india: JurisdictionPack, at_as_of: None
) -> None:
    body = _text(signed_in.get(reverse("compliance:learn_type", args=["PVT_LTD"])))

    assert CAVEAT in body
    assert india.entity_types[0]["description"] in body
    assert f"{len(_rows('PVT_LTD', india))} obligations in the catalog" in body

    headings = re.findall(r'class="learn-group__title"[^>]*>([^<]+)<', body)
    assert headings == ["Direct tax", "Indirect tax"]

    # Sorted by title within a group.
    direct = body[body.index(">Direct tax<") : body.index(">Indirect tax<")]
    titles = [
        "Form 24Q",
        "Form 26Q",
        "Income tax return — company (ITR-6)",
        "Updated return of income",
    ]
    assert [direct.index(title) for title in titles] == sorted(
        direct.index(title) for title in titles
    )

    # The adjective, standing alone — never "Month" or "Year".
    cells = re.findall(r"<td>\s*(\w[\w-]*)\s*</td>", body)
    assert {"Monthly", "Quarterly", "Annual"} <= set(cells)
    assert not {"Month", "Quarter", "Year"} & set(cells)

    # GSTR-3B's next standard date, and the definition page one click away.
    assert "20 Aug 2026" in body
    definition = reverse("compliance:definition", args=["IN-GST-GSTR3B-MONTHLY"])
    assert f'href="{definition}?learn=PVT_LTD"' in body


def test_a_rule_with_no_standard_date_shows_a_blank_not_a_guess(
    signed_in: Client, at_as_of: None
) -> None:
    DefinitionVersion.objects.filter(definition__code="IN-IT-TAX-PAYMENT").update(
        due_rule={
            "anchor": "EVENT_DATE",
            "event_key": "AGM_HELD",
            "offset": {"days": 30},
            "shift_if_holiday": "NONE",
        }
    )

    body = _text(signed_in.get(reverse("compliance:learn_type", args=["PVT_LTD"])))
    row = body[body.index("Income-tax payment") :]
    row = row[: row.index("</tr>")]

    assert "No standard date" in row
    assert "due-badge" not in row


def test_a_row_carries_the_definition_page_s_own_caveat(signed_in: Client, at_as_of: None) -> None:
    DefinitionVersion.objects.filter(definition__code="IN-IT-ITR-COMPANY").update(reviewed_at=None)
    DefinitionVersion.objects.filter(definition__code="IN-TDS-24Q").update(confidence="LOW")

    learn_page = _text(signed_in.get(reverse("compliance:learn_type", args=["PVT_LTD"])))
    stale_page = _text(signed_in.get(reverse("compliance:definition", args=["IN-IT-ITR-COMPANY"])))
    provisional_page = _text(signed_in.get(reverse("compliance:definition", args=["IN-TDS-24Q"])))

    assert learn_page.count(STALE) == 1
    assert learn_page.count(PROVISIONAL) == 1
    assert STALE in stale_page
    assert PROVISIONAL in provisional_page


def test_a_legal_form_the_catalog_does_not_cover_gets_a_way_back(
    signed_in: Client, at_as_of: None
) -> None:
    DefinitionVersion.objects.update(possible_entity_types=[], probe_signature=PROBE_SIGNATURE)

    body = _text(signed_in.get(reverse("compliance:learn_type", args=["HUF"])))

    assert "Nothing in the catalog for this legal form yet" in body
    assert "0 obligations in the catalog" in body
    assert body.count(f'href="{reverse("compliance:learn")}"') >= 2
    # It still says what the page is, even with nothing on it.
    assert CAVEAT in body


def test_an_unknown_legal_form_is_a_404(signed_in: Client) -> None:
    response = signed_in.get(reverse("compliance:learn_type", args=["NOT_A_LEGAL_FORM"]))
    assert response.status_code == 404


def test_the_pages_cost_a_fixed_number_of_queries(
    signed_in: Client, at_as_of: None, django_assert_max_num_queries: object
) -> None:
    """Nine statements are the request itself — session, user, membership, the
    RLS settings around it, the notification badge. The index adds the pack
    and one catalog scan; a legal form's page adds the pack, the definitions in
    force, their version history, and the calendar and extension inputs, once."""
    index = reverse("compliance:learn")
    detail = reverse("compliance:learn_type", args=["PVT_LTD"])
    signed_in.get(index)

    with django_assert_max_num_queries(11):  # type: ignore[operator]
        signed_in.get(index, headers=HTMX)
    with django_assert_max_num_queries(15):  # type: ignore[operator]
        signed_in.get(detail, headers=HTMX)


def test_what_one_tenant_sees_is_what_every_tenant_sees(
    signed_in: Client, rival_owner: User, materialised: Entity, at_as_of: None
) -> None:
    """A tenant with a live calendar and one with no entities at all get the same
    bytes back: nothing on these pages is anybody's own."""
    rival = sign_in(Client(), rival_owner)

    for url in (reverse("compliance:learn"), reverse("compliance:learn_type", args=["PVT_LTD"])):
        assert signed_in.get(url, headers=HTMX).content == rival.get(url, headers=HTMX).content


def test_the_sidebar_offers_learn_to_whoever_reads_the_catalog(signed_in: Client) -> None:
    body = signed_in.get(reverse("app:dashboard")).content.decode()
    assert f'href="{reverse("compliance:learn")}"' in body


def test_a_dealer_is_neither_offered_learn_nor_let_in(
    client: Client, india: JurisdictionPack
) -> None:
    """A channel partner sees no compliance data, and a navigation page about
    compliance is no exception — same line as the rest of the product."""
    with platform_scope(reason="test-fixture"):
        dealer = Tenant.objects.create(
            type=Tenant.Type.DEALER,
            name="Sharma Associates",
            slug="sharma-channel",
            status=Tenant.Status.ACTIVE,
            jurisdiction_pack=india,
        )
    principal = _make_member(
        dealer, "principal@sharma.example", "Ravi Sharma", "+919800000301", "dealer-principal"
    )
    sign_in(client, principal)

    for url in (reverse("compliance:learn"), reverse("compliance:learn_type", args=["PVT_LTD"])):
        assert client.get(url).status_code == 403, url

    statement = client.get(reverse("dealers:statement"))
    assert statement.status_code == 200
    assert reverse("compliance:learn") not in statement.content.decode()


def test_the_definition_page_leads_back_to_the_legal_form(signed_in: Client) -> None:
    url = reverse("compliance:definition", args=["IN-GST-GSTR3B-MONTHLY"])

    from_learn = signed_in.get(f"{url}?learn=PVT_LTD").content.decode()
    forged = signed_in.get(f"{url}?learn=//elsewhere.example").content.decode()

    assert f'href="{reverse("compliance:learn_type", args=["PVT_LTD"])}"' in from_learn
    assert f'href="{reverse("compliance:calendar")}"' in forged


# ===========================================================================
# The frequency label
# ===========================================================================


@pytest.mark.parametrize(
    ("value", "adjective"),
    [
        ("MONTHLY", "Monthly"),
        ("QUARTERLY", "Quarterly"),
        ("HALF_YEARLY", "Half-yearly"),
        ("ANNUAL", "Annual"),
        ("ONE_TIME", "One-time"),
        ("EVENT_BASED", "Event-based"),
    ],
)
def test_frequency_standing_alone_reads_as_an_adjective(value: str, adjective: str) -> None:
    assert periodicity_adjective(value) == adjective


def test_frequency_after_a_label_keeps_its_noun() -> None:
    assert periodicity_label("MONTHLY") == "Month"
    assert periodicity_label("ANNUAL") == "Year"
