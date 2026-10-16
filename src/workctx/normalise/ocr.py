"""Optional OCR for scanned (image-only) PDFs.

Scanned documents - certificates, signed contracts, org charts saved as images - hold real
text that pymupdf cannot extract. When an OCR engine is installed we read it; pictures that
are only pictures (photos, logos) never reach this module because they are skipped by
extension.

Engines, first one available wins:

1. **macOS Vision** through ``pyobjc-framework-Vision`` (``pip install work-context-mirror[ocr]``)
   - on-device, fast (about a second a page) and accurate.
2. **Tesseract** (``tesseract`` on PATH).

With neither installed :func:`ocr_pdf` returns ``None`` and the document stays a stub.
Output is labelled as machine-read so a reader (or LLM) knows to treat it with care.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

OCR_MAX_PAGES = 150
OCR_DPI = 200
MIN_CONFIDENCE = 0.4
MIN_ALNUM_CHARS = 40
OCR_NOTICE = "*Text recovered by OCR from a scanned document; it may contain recognition errors.*"

Engine = Callable[[bytes], str]


def _vision_engine() -> Engine | None:
    if sys.platform != "darwin":
        return None
    try:
        import objc  # type: ignore[import-not-found]
        import Vision  # type: ignore[import-not-found]
        from Foundation import NSData  # type: ignore[import-not-found]
    except ImportError:
        return None

    def run(png: bytes) -> str:
        with objc.autorelease_pool():
            data = NSData.dataWithBytes_length_(png, len(png))
            handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
            request = Vision.VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            request.setUsesLanguageCorrection_(True)
            ok, _err = handler.performRequests_error_([request], None)
            if not ok:
                return ""
            lines = []
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if candidates and candidates[0].confidence() >= MIN_CONFIDENCE:
                    lines.append(str(candidates[0].string()))
            return "\n".join(lines)

    return run


def _tesseract_engine() -> Engine | None:
    binary = shutil.which("tesseract")
    if not binary:
        return None

    def run(png: bytes) -> str:
        proc = subprocess.run(  # fixed argv, image on stdin, no shell
            [binary, "stdin", "stdout"],
            input=png,
            capture_output=True,
            timeout=120,
            check=False,
        )
        return proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else ""

    return run


def get_engine() -> Engine | None:
    """The best OCR engine available on this machine, if any."""
    return _vision_engine() or _tesseract_engine()


def ocr_pdf(file_path: Path, *, engine: Engine | None = None) -> str | None:
    """OCR every page (up to :data:`OCR_MAX_PAGES`) of a scanned PDF into Markdown."""
    engine = engine or get_engine()
    if engine is None:
        return None
    try:
        import pymupdf
    except ImportError:
        return None

    pages: list[str] = []
    try:
        with pymupdf.open(str(file_path)) as doc:
            total = doc.page_count
            for number in range(min(total, OCR_MAX_PAGES)):
                png = doc[number].get_pixmap(dpi=OCR_DPI).tobytes("png")
                text = engine(png).strip()
                if text:
                    pages.append(f"## Page {number + 1}\n\n{text}")
            if total > OCR_MAX_PAGES:
                pages.append(f"*(OCR stopped after {OCR_MAX_PAGES} of {total} pages.)*")
    except Exception:
        logger.warning("OCR failed for %s", file_path.name, exc_info=True)
        return None

    body = "\n\n".join(pages)
    if sum(ch.isalnum() for ch in body) < MIN_ALNUM_CHARS:
        logger.info("OCR found no usable text in %s (probably a picture)", file_path.name)
        return None
    logger.info("OCR recovered text from %s (%d pages)", file_path.name, len(pages))
    return f"{OCR_NOTICE}\n\n{body}"
