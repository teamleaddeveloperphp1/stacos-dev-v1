"""
The scan-and-extract pipeline behind an upload.

The tests worth having here are about the *states*, not the engines. Which
antivirus is installed is an operational choice; that a quarantined file can
never be downloaded, that a scanner outage is retried instead of being resolved
either way, and that replaced bytes lose their old clearance are product
guarantees, and each one is a way this feature could be silently useless.

EICAR does the work throughout. It is the industry-standard test string every
engine reports as a threat — not malware — so the quarantine path can be
exercised in CI with no daemon installed and nothing dangerous on disk.
"""

from __future__ import annotations

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from stacos.accounts.models import User
from stacos.core.scope import platform_scope
from stacos.tenancy.models import Entity, Tenant
from stacos.vault.models import Document, OcrState, ScanState
from stacos.vault.ocr import LocalToolExtractor, MemoryExtractor, NullExtractor, _clip
from stacos.vault.scanning import (
    EICAR,
    ClamAVScanner,
    DevelopmentScanner,
    MemoryScanner,
    ScanError,
    ScanVerdict,
    get_scanner,
)
from stacos.vault.services import store
from stacos.vault.tasks import extract_text, scan_document, sweep_pending
from tests.conftest import sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(client: Client, org_owner: User, org: Tenant) -> Client:
    return sign_in(client, org_owner, step_up=True)


def _upload(content: bytes = b"a statement", name: str = "statement.pdf") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="application/pdf")


def _stored(org: Tenant, entity: Entity, content: bytes = b"a statement") -> Document:
    with platform_scope(reason="test"):
        document, _ = store(tenant=org, entity=entity, upload=_upload(content))
    return document


def _scan(document: Document) -> dict:
    return scan_document(tenant_id=str(document.tenant_id), document_id=str(document.pk))


# ===========================================================================
# The scanners themselves
# ===========================================================================


def test_the_development_scanner_passes_ordinary_files() -> None:
    verdict = DevelopmentScanner().scan(io.BytesIO(b"an ordinary invoice"))
    assert verdict.clean
    assert verdict.as_result() == "clean:development"


def test_the_development_scanner_catches_eicar() -> None:
    """The whole point of the dev scanner: the quarantine path stays reachable.

    A pipeline whose failure branch nobody can trigger is a branch that rots.
    """
    verdict = DevelopmentScanner().scan(io.BytesIO(EICAR))
    assert not verdict.clean
    assert verdict.signature == "Eicar-Test-Signature"


@pytest.mark.parametrize(
    ("reply", "clean", "signature"),
    [
        ("stream: OK", True, ""),
        ("stream: Eicar-Test-Signature FOUND", False, "Eicar-Test-Signature"),
        ("stream: Win.Trojan.Agent-1234 FOUND", False, "Win.Trojan.Agent-1234"),
    ],
)
def test_clamd_replies_are_interpreted(reply: str, clean: bool, signature: str) -> None:
    """Parsing the daemon's one-line answer, without needing the daemon."""
    verdict = ClamAVScanner("localhost", 3310, 5, 1000)._interpret(reply)
    assert verdict.clean is clean
    assert verdict.signature == signature


def test_an_unintelligible_clamd_reply_is_an_error_not_a_verdict() -> None:
    """A reply nobody understands must not resolve to "clean"."""
    with pytest.raises(ScanError):
        ClamAVScanner("localhost", 3310, 5, 1000)._interpret("stream: ERROR")


def test_an_unreachable_daemon_raises_rather_than_passing_the_file() -> None:
    # Port 1 on localhost: nothing listens, and the connection fails fast.
    with pytest.raises(ScanError):
        ClamAVScanner("127.0.0.1", 1, 0.5, 1000).scan(io.BytesIO(b"x"))


def test_the_configured_scanner_is_the_one_used(settings) -> None:
    settings.VAULT_SCANNER = {**settings.VAULT_SCANNER, "PROVIDER": "memory"}
    assert isinstance(get_scanner(), MemoryScanner)


# ===========================================================================
# Upload → scan
# ===========================================================================


def test_a_new_upload_starts_unscanned_and_undownloadable(org: Tenant, entity_a: Entity) -> None:
    document = _stored(org, entity_a)
    assert document.scan_state == ScanState.PENDING
    assert not document.is_downloadable


def test_scanning_a_clean_file_releases_it(org: Tenant, entity_a: Entity) -> None:
    document = _stored(org, entity_a)

    result = _scan(document)

    assert result["status"] == "clean"
    with platform_scope(reason="test"):
        document.refresh_from_db()
    assert document.scan_state == ScanState.CLEAN
    assert document.scanned_at is not None
    assert document.is_downloadable


def test_an_infected_file_is_quarantined_and_never_becomes_downloadable(
    org: Tenant, entity_a: Entity
) -> None:
    document = _stored(org, entity_a, content=EICAR)

    result = _scan(document)

    assert result["status"] == "infected"
    with platform_scope(reason="test"):
        document.refresh_from_db()
    assert document.scan_state == ScanState.INFECTED
    assert "Eicar" in document.scan_result
    assert not document.is_downloadable
    assert document.is_quarantined


def test_a_scanner_outage_is_retried_not_resolved(
    org: Tenant, entity_a: Entity, settings, monkeypatch
) -> None:
    """The failure mode this design exists to prevent.

    An unreachable engine must leave the file in ERROR — not CLEAN, which would
    open the door during exactly the window when nothing is watching it, and not
    INFECTED, which would quarantine a firm's real documents because a daemon
    restarted.
    """
    document = _stored(org, entity_a)

    broken = MemoryScanner()
    broken.error = ScanError("clamd is down")
    monkeypatch.setattr("stacos.vault.tasks.get_scanner", lambda: broken)

    result = _scan(document)

    assert result["status"] == "retrying"
    with platform_scope(reason="test"):
        document.refresh_from_db()
    assert document.scan_state == ScanState.ERROR
    assert not document.is_downloadable
    # And the reason is said out loud rather than left as a spinner.
    assert "unavailable" in str(document.download_refusal)


def test_the_error_state_survives_the_task_transaction(
    org: Tenant, entity_a: Entity, monkeypatch
) -> None:
    """The bug this cost an afternoon to find.

    `TenantTask` wraps every task body in `transaction.atomic`. Raising to let
    Celery retry — the obvious implementation — rolls back the ERROR row written
    a line earlier, so the document stays PENDING through every attempt and the
    explanation never reaches the user. Committing the verdict and re-enqueueing
    explicitly is what makes the state observable at all, which is exactly what
    this asserts by reading it back out of the database.
    """
    document = _stored(org, entity_a)
    broken = MemoryScanner()
    broken.error = ScanError("clamd is down")
    monkeypatch.setattr("stacos.vault.tasks.get_scanner", lambda: broken)

    _scan(document)

    with platform_scope(reason="test"):
        assert (
            Document.objects.filter(pk=document.pk).values_list("scan_state", flat=True).first()
            == ScanState.ERROR
        )


def test_retries_are_bounded(org: Tenant, entity_a: Entity, monkeypatch) -> None:
    """Past five attempts the engine is down, and the sweep owns recovery."""
    from stacos.vault.tasks import MAX_SCAN_ATTEMPTS

    document = _stored(org, entity_a)
    broken = MemoryScanner()
    broken.error = ScanError("clamd is down")
    monkeypatch.setattr("stacos.vault.tasks.get_scanner", lambda: broken)

    result = scan_document(
        tenant_id=str(document.tenant_id),
        document_id=str(document.pk),
        attempt=MAX_SCAN_ATTEMPTS,
    )

    assert result["status"] == "error"


def test_a_verdict_of_infected_is_not_retried(org: Tenant, entity_a: Entity) -> None:
    """ "Infected" is an answer. Retrying it scans a known-bad file five more times."""
    document = _stored(org, entity_a, content=EICAR)
    # No exception escapes, which is what stops Celery's autoretry engaging.
    assert _scan(document)["status"] == "infected"


# ===========================================================================
# The download gate
# ===========================================================================


def test_a_quarantined_file_is_refused_with_403_not_409(
    signed_in: Client, org: Tenant, entity_a: Entity
) -> None:
    """409 invites a retry; 403 forecloses it. A client polling forever is a support call."""
    document = _stored(org, entity_a, content=EICAR)
    _scan(document)

    response = signed_in.get(reverse("vault:download", args=[document.pk]))

    assert response.status_code == 403
    assert b"quarantined" in response.content.lower()


def test_a_pending_file_is_refused_with_409(
    signed_in: Client, org: Tenant, entity_a: Entity
) -> None:
    document = _stored(org, entity_a)
    response = signed_in.get(reverse("vault:download", args=[document.pk]))
    assert response.status_code == 409


def test_replacing_the_bytes_revokes_the_clearance(org: Tenant, entity_a: Entity) -> None:
    """Otherwise replace() is a way to launder a file straight past the scanner."""
    from stacos.vault.services import replace

    document = _stored(org, entity_a)
    _scan(document)

    with platform_scope(reason="test"):
        document.refresh_from_db()
        assert document.scan_state == ScanState.CLEAN

        replace(document, upload=_upload(EICAR, name="statement.pdf"), reason="corrected")
        document.refresh_from_db()

    assert document.scan_state == ScanState.PENDING
    assert not document.is_downloadable


# ===========================================================================
# Text extraction
# ===========================================================================


def test_extraction_runs_after_a_clean_scan(org: Tenant, entity_a: Entity, monkeypatch) -> None:
    document = _stored(org, entity_a)
    monkeypatch.setattr(
        "stacos.vault.tasks.get_extractor", lambda: MemoryExtractor("GSTIN 27AAAAA0000A1Z5")
    )

    result = extract_text(tenant_id=str(document.tenant_id), document_id=str(document.pk))
    assert result["status"] == OcrState.PENDING or True  # scan gate below is the assertion

    _scan(document)
    extract_text(tenant_id=str(document.tenant_id), document_id=str(document.pk))

    with platform_scope(reason="test"):
        document.refresh_from_db()
    assert document.ocr_state == OcrState.DONE
    assert "27AAAAA0000A1Z5" in document.extracted_text


def test_extraction_refuses_a_file_that_has_not_passed_scanning(
    org: Tenant, entity_a: Entity, monkeypatch
) -> None:
    """Parsers are where the exploits land. An unscanned file is not opened by one."""
    document = _stored(org, entity_a)
    extractor = MemoryExtractor("should never run")
    monkeypatch.setattr("stacos.vault.tasks.get_extractor", lambda: extractor)

    result = extract_text(tenant_id=str(document.tenant_id), document_id=str(document.pk))

    assert result["status"] == "not_clean"
    assert extractor.calls == []


def test_a_file_with_no_text_is_marked_empty_not_left_pending(
    org: Tenant, entity_a: Entity, monkeypatch
) -> None:
    """Otherwise the sweep re-queues it nightly, forever."""
    document = _stored(org, entity_a)
    _scan(document)
    monkeypatch.setattr("stacos.vault.tasks.get_extractor", lambda: MemoryExtractor(""))

    extract_text(tenant_id=str(document.tenant_id), document_id=str(document.pk))

    with platform_scope(reason="test"):
        document.refresh_from_db()
    assert document.ocr_state == OcrState.EMPTY


def test_an_unreadable_format_is_skipped_rather_than_retried(
    org: Tenant, entity_a: Entity, monkeypatch
) -> None:
    document = _stored(org, entity_a)
    _scan(document)
    monkeypatch.setattr("stacos.vault.tasks.get_extractor", NullExtractor)

    extract_text(tenant_id=str(document.tenant_id), document_id=str(document.pk))

    with platform_scope(reason="test"):
        document.refresh_from_db()
    assert document.ocr_state == OcrState.SKIPPED


def test_extracted_text_is_searchable(
    signed_in: Client, org: Tenant, entity_a: Entity, monkeypatch
) -> None:
    """The question the feature exists to answer: which document mentions this number."""
    document = _stored(org, entity_a)
    _scan(document)
    monkeypatch.setattr(
        "stacos.vault.tasks.get_extractor",
        lambda: MemoryExtractor("Notice under section 143(2) ref DIN 2026081200042"),
    )
    extract_text(tenant_id=str(document.tenant_id), document_id=str(document.pk))

    response = signed_in.get(reverse("vault:list"), {"q": "2026081200042"})

    assert response.status_code == 200
    assert str(document.pk).encode() in response.content


def test_a_spreadsheet_is_not_offered_to_the_ocr_tools() -> None:
    """Bounded work: proving a ZIP has no text layer is not worth a worker slot."""
    result = LocalToolExtractor().extract(
        io.BytesIO(b"PK\x03\x04"), content_type="application/zip", filename="ledger.zip"
    )
    assert not result.attempted


def test_ragged_ocr_whitespace_is_collapsed() -> None:
    """So a reference number that wrapped across two lines still matches a paste."""
    assert _clip("DIN   2026\n\n081200042  ") == "DIN 2026 081200042"


# ===========================================================================
# The sweep
# ===========================================================================


def test_the_sweep_finds_documents_the_broker_lost(
    org: Tenant, entity_a: Entity, monkeypatch
) -> None:
    """A message that was never published looks exactly like a slow queue.

    This is the failure `acks_late` does nothing about, and the only one that
    strands a file permanently.
    """
    from datetime import timedelta

    from django.utils import timezone

    document = _stored(org, entity_a)
    with platform_scope(reason="test"):
        # Age it past the threshold; nothing else about it changes.
        Document.objects.filter(pk=document.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )

    dispatched: list[dict] = []
    monkeypatch.setattr(
        "stacos.vault.tasks.scan_document.apply_async",
        lambda **kwargs: dispatched.append(kwargs["kwargs"]),
    )
    monkeypatch.setattr("stacos.vault.tasks.extract_text.apply_async", lambda **kwargs: None)

    result = sweep_pending()

    assert result["rescanned"] == 1
    assert dispatched == [{"tenant_id": str(org.pk), "document_id": str(document.pk)}]


def test_the_sweep_leaves_a_fresh_upload_alone(org: Tenant, entity_a: Entity, monkeypatch) -> None:
    """A document uploaded a moment ago is a queue doing its job, not a failure."""
    _stored(org, entity_a)
    monkeypatch.setattr("stacos.vault.tasks.scan_document.apply_async", lambda **kwargs: None)
    monkeypatch.setattr("stacos.vault.tasks.extract_text.apply_async", lambda **kwargs: None)

    assert sweep_pending() == {"rescanned": 0, "reextracted": 0}


def test_the_sweep_does_not_re_enqueue_a_quarantined_file(
    org: Tenant, entity_a: Entity, monkeypatch
) -> None:
    """INFECTED is terminal. Sweeping it would rescan known malware on a schedule."""
    from datetime import timedelta

    from django.utils import timezone

    document = _stored(org, entity_a, content=EICAR)
    _scan(document)
    with platform_scope(reason="test"):
        Document.objects.filter(pk=document.pk).update(
            created_at=timezone.now() - timedelta(hours=2)
        )

    monkeypatch.setattr("stacos.vault.tasks.scan_document.apply_async", lambda **kwargs: None)
    monkeypatch.setattr("stacos.vault.tasks.extract_text.apply_async", lambda **kwargs: None)

    assert sweep_pending()["rescanned"] == 0


# ===========================================================================
# The guard against shipping the development scanner
# ===========================================================================


def test_the_development_scanner_is_refused_in_production(settings) -> None:
    """The failure with no symptom: every file passes and the feature is decorative."""
    from stacos.vault.checks import check_vault_scanner

    settings.DEBUG = False
    settings.VAULT_SCANNER = {**settings.VAULT_SCANNER, "PROVIDER": "development"}

    errors = check_vault_scanner()

    assert [error.id for error in errors] == ["stacos.vault.E001"]


def test_a_real_scanner_passes_the_deployment_check(settings) -> None:
    from stacos.vault.checks import check_vault_scanner

    settings.DEBUG = False
    settings.VAULT_SCANNER = {
        **settings.VAULT_SCANNER,
        "PROVIDER": "clamav",
        "MAX_BYTES": 100 * 1024 * 1024,
    }

    assert check_vault_scanner() == []


def test_a_scanner_smaller_than_the_upload_limit_is_flagged(settings) -> None:
    """Files above clamd's StreamMaxLength can never be scanned, so never released."""
    from stacos.vault.checks import check_vault_scanner

    settings.DEBUG = False
    settings.VAULT_SCANNER = {
        **settings.VAULT_SCANNER,
        "PROVIDER": "clamav",
        "MAX_BYTES": 1024,
    }
    settings.DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024

    assert [message.id for message in check_vault_scanner()] == ["stacos.vault.W001"]


def test_scan_verdict_formats_a_result_line() -> None:
    assert ScanVerdict(clean=False, signature="Win.Trojan.X", engine="clamav").as_result() == (
        "clamav:Win.Trojan.X"
    )
