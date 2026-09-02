"""PDF conversion with smart pre-flight checks and adaptive timeouts.

Before attempting the expensive conversion, we cheaply read PDF metadata
(< 100ms even for large files) to skip files that will never produce
useful text (encrypted, image-only, corrupt) and to scale the timeout
proportionally to page count.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_TIMEOUT_SECONDS = 30
SECONDS_PER_PAGE = 1.5
MAX_TIMEOUT_SECONDS = 300
TEXT_SAMPLE_PAGES = 3
MIN_TEXT_PER_PAGE = 15

_WORKER_SCRIPT = textwrap.dedent("""\
    import json, sys
    try:
        import pymupdf4llm
        text = pymupdf4llm.to_markdown(sys.argv[1])
        json.dump({"ok": True, "text": text.strip() if text else ""}, sys.stdout)
    except Exception as e:
        json.dump({"ok": False, "error": str(e)}, sys.stdout)
""")


@dataclass
class PreflightResult:
    should_process: bool
    timeout: int = BASE_TIMEOUT_SECONDS
    skip_reason: str = ""
    page_count: int = 0


def can_handle(file_path: Path) -> bool:
    return file_path.suffix.lower() == ".pdf"


def preflight_pdf(file_path: Path) -> PreflightResult:
    """Read PDF metadata to decide whether conversion will succeed.

    This is nearly instant (reads the document catalog only, not page
    content) and catches the common failure modes that would otherwise
    block a worker thread for the full timeout.
    """
    try:
        file_size = file_path.stat().st_size
    except OSError:
        return PreflightResult(False, skip_reason="unreadable file")

    if file_size == 0:
        return PreflightResult(False, skip_reason="empty file")

    try:
        import fitz  # PyMuPDF — already installed as pymupdf4llm dep
    except ImportError:
        return PreflightResult(True, timeout=BASE_TIMEOUT_SECONDS)

    try:
        doc = fitz.open(str(file_path))
    except Exception as exc:
        return PreflightResult(False, skip_reason=f"corrupt/unreadable ({exc})")

    try:
        if doc.is_encrypted:
            return PreflightResult(False, skip_reason="encrypted/password-protected")

        page_count = doc.page_count
        if page_count == 0:
            return PreflightResult(False, skip_reason="zero pages")

        sample_n = min(TEXT_SAMPLE_PAGES, page_count)
        total_chars = 0
        for i in range(sample_n):
            with contextlib.suppress(Exception):
                total_chars += len(doc[i].get_text("text"))

        avg_chars = total_chars / sample_n if sample_n else 0

        if avg_chars < MIN_TEXT_PER_PAGE:
            # pymupdf4llm won't extract text without OCR
            size_mb = file_size / (1024 * 1024)
            return PreflightResult(
                False,
                skip_reason=(
                    f"image-only/scanned ({page_count} pages, "
                    f"~{avg_chars:.0f} chars/page, {size_mb:.1f} MB)"
                ),
                page_count=page_count,
            )

        timeout = int(min(
            BASE_TIMEOUT_SECONDS + page_count * SECONDS_PER_PAGE,
            MAX_TIMEOUT_SECONDS,
        ))

        return PreflightResult(
            should_process=True,
            timeout=timeout,
            page_count=page_count,
        )
    finally:
        doc.close()


def convert_pdf(file_path: Path) -> str | None:
    """Convert a PDF to Markdown.

    Runs a pre-flight metadata check first, then spawns pymupdf4llm in a
    subprocess with a timeout scaled to the document's page count.
    """
    check = preflight_pdf(file_path)

    if not check.should_process:
        logger.info(
            "PDF skipped (pre-flight): %s — %s", file_path.name, check.skip_reason,
        )
        return None

    if check.page_count > 0:
        logger.debug(
            "PDF pre-flight OK: %s — %d pages, timeout %ds",
            file_path.name, check.page_count, check.timeout,
        )

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _WORKER_SCRIPT, str(file_path)],
            capture_output=True,
            text=True,
            timeout=check.timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "PDF conversion timed out after %ds (%d pages): %s",
            check.timeout,
            check.page_count,
            file_path,
        )
        return None

    if proc.returncode != 0:
        logger.error("PDF subprocess failed (rc=%d): %s", proc.returncode, file_path)
        return None

    try:
        result = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        logger.error("PDF subprocess returned invalid JSON: %s", file_path)
        return None

    if not result.get("ok"):
        logger.error("PyMuPDF4LLM failed: %s — %s", file_path, result.get("error"))
        return None

    text = result.get("text", "")
    if not text:
        logger.warning("PDF conversion produced no usable text: %s", file_path)
        return None

    return text
