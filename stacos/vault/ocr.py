"""
Getting text out of documents.

The question this exists to answer is "which document mentions this number" —
a notice reference, a challan number, a PAN. During an assessment somebody has a
number on a letter and forty files in a folder, and without text extraction the
only way through is to open all forty.

Two kinds of file arrive, and conflating them wastes an enormous amount of time:

* A **born-digital PDF** already contains its text. Pulling it out is a
  millisecond of parsing, and running OCR over it instead is both slower and
  *less* accurate than the text that is already there.
* A **scan** — a photographed challan, a posted notice — contains pixels. That
  needs real OCR, which is slow, imperfect, and worth it anyway.

So the extractor tries the cheap path first and escalates only when the cheap
path comes back empty. That single ordering is most of the value here.

**The tools are external processes, not Python packages.** Poppler and Tesseract
are what a self-hosted deployment already has or can `apt install`; binding them
as libraries adds build-time dependencies to every developer laptop for a feature
that is optional by design. When they are absent the extractor says so and the
document is simply not searchable by content — a degraded feature, not an error.
"""

from __future__ import annotations

import abc
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, ClassVar

import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)

__all__ = [
    "ExtractionResult",
    "LocalToolExtractor",
    "MemoryExtractor",
    "NullExtractor",
    "TextExtractor",
    "get_extractor",
]

#: Below this many characters, a PDF's text layer is treated as absent. A scanned
#: page routinely carries a few dozen characters of producer metadata or a
#: stamped page number, which is enough to look like success and not nearly
#: enough to search.
_TEXT_LAYER_MINIMUM = 80

#: Extracted text is stored in a row that gets loaded by list views. A thousand
#: pages of OCR in one column is a slow query for no benefit — nobody searches
#: past the first few hundred thousand characters.
_MAX_CHARS = 200_000


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Text, and an honest account of how it was obtained."""

    text: str
    engine: str
    #: False when the file type is not one text can be extracted from — a
    #: spreadsheet, an archive. Distinct from "tried and got nothing", which is a
    #: blank page and worth recording as attempted so it is not retried forever.
    attempted: bool = True

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


class TextExtractor(abc.ABC):
    name: ClassVar[str] = "extractor"

    @abc.abstractmethod
    def extract(self, stream: IO[bytes], *, content_type: str, filename: str) -> ExtractionResult:
        """Read ``stream`` and return whatever text it holds."""


class NullExtractor(TextExtractor):
    """Extracts nothing. The default, and correct when no tools are installed."""

    name = "none"

    # The arguments are the extractor contract, not this provider's needs.
    def extract(
        self,
        stream: IO[bytes],  # noqa: ARG002
        *,
        content_type: str,  # noqa: ARG002
        filename: str,  # noqa: ARG002
    ) -> ExtractionResult:
        return ExtractionResult(text="", engine=self.name, attempted=False)


class LocalToolExtractor(TextExtractor):
    """Poppler for PDFs, Tesseract for images and for PDFs that are really scans."""

    name = "local"

    #: Formats worth trying. Anything else returns `attempted=False` rather than
    #: burning a worker slot proving that a ZIP has no text layer.
    IMAGE_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/tiff", "image/bmp", "image/webp")

    def __init__(self, *, languages: str = "eng", timeout: int = 120, max_pages: int = 20) -> None:
        self.languages = languages
        self.timeout = timeout
        self.max_pages = max_pages

    # -- capability -------------------------------------------------------

    @staticmethod
    def _tool(name: str) -> str | None:
        return shutil.which(name)

    def available(self) -> bool:
        return bool(self._tool("pdftotext") or self._tool("tesseract"))

    # -- entry point ------------------------------------------------------

    def extract(self, stream: IO[bytes], *, content_type: str, filename: str) -> ExtractionResult:
        suffix = Path(filename or "").suffix.lower()
        kind = (content_type or "").lower()

        is_pdf = kind == "application/pdf" or suffix == ".pdf"
        is_image = kind.startswith("image/") or suffix in {
            ".png",
            ".jpg",
            ".jpeg",
            ".tif",
            ".tiff",
            ".bmp",
            ".webp",
        }

        if not (is_pdf or is_image):
            return ExtractionResult(text="", engine=self.name, attempted=False)

        # A temporary file rather than a pipe: both tools seek, and neither is
        # reliable reading a PDF from stdin. The directory is removed on exit
        # even if a tool crashes, which matters because these are client
        # documents sitting unencrypted on a worker's disk.
        with tempfile.TemporaryDirectory(prefix="stacos-ocr-") as workspace:
            path = Path(workspace) / f"input{suffix or ('.pdf' if is_pdf else '.png')}"
            with path.open("wb") as handle:
                shutil.copyfileobj(stream, handle)

            if is_pdf:
                return self._from_pdf(path)
            return self._from_image(path)

    # -- the two paths ----------------------------------------------------

    def _from_pdf(self, path: Path) -> ExtractionResult:
        text = ""
        if self._tool("pdftotext"):
            text = self._run(["pdftotext", "-layout", "-q", str(path), "-"])

        if len(text.strip()) >= _TEXT_LAYER_MINIMUM:
            return ExtractionResult(text=_clip(text), engine="pdftotext")

        # No usable text layer: this is a scan. Rasterise a bounded number of
        # pages and OCR them. Bounded because a 400-page assessment record would
        # otherwise occupy an OCR worker for an hour to make page 380 findable.
        if not (self._tool("pdftoppm") and self._tool("tesseract")):
            return ExtractionResult(text=_clip(text), engine="pdftotext", attempted=True)

        pages = self._run(
            [
                "pdftoppm",
                "-r",
                "200",
                "-f",
                "1",
                "-l",
                str(self.max_pages),
                "-png",
                str(path),
                str(path.parent / "page"),
            ],
            expect_output=False,
        )
        del pages

        chunks = [self._tesseract(image) for image in sorted(path.parent.glob("page-*.png"))]
        joined = "\n\n".join(chunk for chunk in chunks if chunk.strip())
        return ExtractionResult(text=_clip(joined), engine="tesseract")

    def _from_image(self, path: Path) -> ExtractionResult:
        if not self._tool("tesseract"):
            return ExtractionResult(text="", engine=self.name, attempted=False)
        return ExtractionResult(text=_clip(self._tesseract(path)), engine="tesseract")

    def _tesseract(self, image: Path) -> str:
        return self._run(["tesseract", str(image), "stdout", "-l", self.languages])

    def _run(self, argv: list[str], *, expect_output: bool = True) -> str:
        """Run a tool, and treat any failure as "no text".

        A malformed PDF makes poppler exit non-zero, and that is a normal event
        in a product where users upload whatever a government portal produced.
        It is not worth failing a task over: the document is stored, it is
        scanned, it is simply not searchable by content.
        """
        try:
            completed = subprocess.run(  # noqa: S603 - argv is built here, never from input
                argv,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("vault.ocr.tool_failed", tool=argv[0], error=str(exc))
            return ""

        if completed.returncode != 0:
            logger.info(
                "vault.ocr.tool_nonzero",
                tool=argv[0],
                code=completed.returncode,
                stderr=completed.stderr[:300].decode("utf-8", "replace"),
            )
        if not expect_output:
            return ""
        return completed.stdout.decode("utf-8", "replace")


class MemoryExtractor(TextExtractor):
    """Returns whatever it is told to. For tests."""

    name = "memory"

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls: list[str] = []

    def extract(
        self,
        stream: IO[bytes],
        *,
        content_type: str,  # noqa: ARG002 - see NullExtractor
        filename: str,
    ) -> ExtractionResult:
        self.calls.append(filename)
        stream.read()
        return ExtractionResult(text=self.text, engine=self.name, attempted=True)


def _clip(text: str) -> str:
    """Normalise whitespace and cap the length.

    OCR output is full of ragged spacing that helps nobody and inflates the row;
    collapsing it makes `icontains` behave the way a user expects when they paste
    a reference number that wrapped across two lines in the original.
    """
    collapsed = " ".join(text.split())
    return collapsed[:_MAX_CHARS]


def get_extractor() -> TextExtractor:
    config = getattr(settings, "VAULT_OCR", {})
    provider = str(config.get("PROVIDER", "none")).lower()

    if provider == "local":
        extractor = LocalToolExtractor(
            languages=str(config.get("LANGUAGES", "eng")),
            timeout=int(config.get("TIMEOUT", 120)),
            max_pages=int(config.get("MAX_PAGES", 20)),
        )
        if not extractor.available():
            # Configured but the binaries are not on PATH. Degrading loudly
            # beats every document silently coming back with no text and nobody
            # knowing why for a month.
            logger.warning("vault.ocr.tools_missing", hint="install poppler-utils and tesseract")
            return NullExtractor()
        return extractor
    if provider == "memory":
        return MemoryExtractor()
    return NullExtractor()
