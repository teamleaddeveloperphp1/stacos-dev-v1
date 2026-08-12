"""
Virus scanning for the vault.

Every file in this product moves between two organisations that do not otherwise
trust each other: a practice and its client. A client uploads a bank statement, a
partner downloads it; an accountant uploads a working paper, three colleagues
open it. That is precisely the topology a malicious document is designed to
exploit, and it is why nothing here becomes downloadable until an engine has
looked at it.

The interface is a provider, chosen by setting, for the same reason the WhatsApp
channel is: which engine a deployment runs is an operational choice. A firm with
its own infrastructure runs clamd next to the worker; a hosted deployment might
front an API. Neither belongs in a caller.

**The verdict is a three-state, not a boolean.** "Clean", "infected" and "the
engine could not be reached" are different facts with different handling: the
first releases the file, the second quarantines it forever, and the third must
retry rather than either release or condemn. Collapsing the third into either of
the others is how a scanner outage silently turns into an open door — or into a
day of false quarantines that destroys trust in the feature.
"""

from __future__ import annotations

import abc
import socket
from dataclasses import dataclass
from typing import IO, ClassVar

import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

__all__ = [
    "ClamAVScanner",
    "DevelopmentScanner",
    "MemoryScanner",
    "ScanError",
    "ScanVerdict",
    "Scanner",
    "get_scanner",
]

#: The EICAR anti-malware test file. Not a virus: an industry-standard string
#: every engine is required to report as one, so that a scanning path can be
#: proved end to end without handling real malware. The development scanner
#: recognises it, which is what makes the quarantine path demonstrable on a
#: laptop with no engine installed.
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class ScanError(RuntimeError):
    """The engine could not be reached, or answered something unintelligible.

    Deliberately *not* a verdict. A caller that catches this must retry; a caller
    that treats it as "clean" has defeated the feature, and one that treats it as
    "infected" quarantines the customer's real documents during an outage.
    """


@dataclass(frozen=True, slots=True)
class ScanVerdict:
    """What an engine concluded about one file."""

    clean: bool
    #: The engine's own name for what it found, empty when clean. Stored verbatim
    #: because "which signature" is the first question asked about a quarantine.
    signature: str = ""
    engine: str = ""

    def as_result(self) -> str:
        """A short string for ``Document.scan_result``."""
        if self.clean:
            return f"clean:{self.engine}"[:120]
        return f"{self.engine}:{self.signature}"[:120]


class Scanner(abc.ABC):
    """Reads a file-like object and returns a verdict."""

    name: ClassVar[str] = "scanner"

    @abc.abstractmethod
    def scan(self, stream: IO[bytes]) -> ScanVerdict:
        """Scan ``stream`` from its current position.

        Raises :class:`ScanError` when no verdict could be obtained.
        """


class ClamAVScanner(Scanner):
    """Streams the file to a clamd daemon over its INSTREAM protocol.

    Speaking the wire protocol directly rather than depending on a client
    library: it is a length-prefixed byte stream and a one-line reply, the whole
    implementation is the method below, and it removes a dependency from the
    path that every uploaded byte travels through.

    Streaming rather than passing a path, because in any real deployment the
    worker and the daemon do not share a filesystem — storage is object storage,
    and clamd cannot open an S3 key.
    """

    name = "clamav"

    #: clamd's default StreamMaxLength is 25 MB and it aborts the connection when
    #: exceeded. Chunking below that is not enough on its own — the *total* is
    #: what matters — so oversize files are reported rather than silently passed.
    CHUNK = 64 * 1024

    def __init__(self, host: str, port: int, timeout: float, max_bytes: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.max_bytes = max_bytes

    def scan(self, stream: IO[bytes]) -> ScanVerdict:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(b"zINSTREAM\0")

                sent = 0
                while chunk := stream.read(self.CHUNK):
                    sent += len(chunk)
                    if sent > self.max_bytes:
                        # Abort the stream rather than letting the daemon close
                        # it under us, which would be indistinguishable from a
                        # crash and would retry forever.
                        sock.sendall(b"\0\0\0\0")
                        raise ScanError(f"File exceeds the scanner's {self.max_bytes} byte limit.")
                    sock.sendall(len(chunk).to_bytes(4, "big") + chunk)

                sock.sendall(b"\0\0\0\0")
                reply = self._read_reply(sock)
        except (OSError, socket.timeout) as exc:  # noqa: UP041 - explicit for clarity
            raise ScanError(f"clamd at {self.host}:{self.port} is unreachable: {exc}") from exc

        return self._interpret(reply)

    @staticmethod
    def _read_reply(sock: socket.socket) -> str:
        parts: list[bytes] = []
        while chunk := sock.recv(4096):
            parts.append(chunk)
            if b"\0" in chunk:
                break
        return b"".join(parts).rstrip(b"\0").decode("utf-8", "replace").strip()

    def _interpret(self, reply: str) -> ScanVerdict:
        # `stream: OK` / `stream: Eicar-Signature FOUND` / `... ERROR`
        if reply.endswith("OK"):
            return ScanVerdict(clean=True, engine=self.name)
        if reply.endswith("FOUND"):
            signature = reply.split(":", 1)[-1].strip().removesuffix("FOUND").strip()
            return ScanVerdict(clean=False, signature=signature or "unknown", engine=self.name)
        raise ScanError(f"clamd returned an unrecognised reply: {reply!r}")


class DevelopmentScanner(Scanner):
    """Passes everything except EICAR. For development only.

    A developer needs uploads to become downloadable without installing an
    antivirus daemon, and a scanning pipeline nobody can exercise is a pipeline
    that breaks unnoticed. So this passes real files and catches the one string
    every engine agrees is a threat, which makes the quarantine path reachable
    from a test and from a laptop.

    ``stacos.vault.E001`` refuses to let this run with ``DEBUG = False``.
    """

    name = "development"

    def scan(self, stream: IO[bytes]) -> ScanVerdict:
        head = stream.read(4096)
        if EICAR in head:
            return ScanVerdict(clean=False, signature="Eicar-Test-Signature", engine=self.name)
        return ScanVerdict(clean=True, engine=self.name)


class MemoryScanner(Scanner):
    """Records what it was asked to scan and answers as instructed. For tests."""

    name = "memory"

    def __init__(self) -> None:
        self.calls: list[bytes] = []
        self.verdict: ScanVerdict = ScanVerdict(clean=True, engine=self.name)
        self.error: ScanError | None = None

    def scan(self, stream: IO[bytes]) -> ScanVerdict:
        self.calls.append(stream.read())
        if self.error is not None:
            raise self.error
        return self.verdict


def get_scanner() -> Scanner:
    """The configured scanner.

    Built per call rather than cached: a socket-holding object shared across
    threads in a worker is a subtle source of interleaved streams, and
    construction is a few microseconds.
    """
    config = getattr(settings, "VAULT_SCANNER", {})
    provider = str(config.get("PROVIDER", "development")).lower()

    if provider == "clamav":
        return ClamAVScanner(
            host=str(config.get("HOST", "127.0.0.1")),
            port=int(config.get("PORT", 3310)),
            timeout=float(config.get("TIMEOUT", 30)),
            max_bytes=int(config.get("MAX_BYTES", 25 * 1024 * 1024)),
        )
    if provider == "memory":
        return MemoryScanner()
    if provider == "development":
        return DevelopmentScanner()
    raise ImproperlyConfiguredScanner(provider)


class ImproperlyConfiguredScanner(ValueError):
    def __init__(self, provider: str) -> None:
        super().__init__(
            f"VAULT_SCANNER['PROVIDER'] = {provider!r} is not a scanner. "
            f"Choose 'clamav', 'development' or 'memory'."
        )
