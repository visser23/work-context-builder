"""PDF conversion using PyMuPDF4LLM with a hard timeout to prevent infinite loops."""

from __future__ import annotations

import logging
import multiprocessing
from pathlib import Path

logger = logging.getLogger(__name__)

CONVERSION_TIMEOUT_SECONDS = 120


def can_handle(file_path: Path) -> bool:
    return file_path.suffix.lower() == ".pdf"


def convert_pdf(file_path: Path, *, use_docling_fallback: bool = False) -> str | None:
    """Convert a PDF to Markdown.

    Primary: PyMuPDF4LLM (fast, layout-aware).
    Runs in a subprocess with a hard timeout to prevent hangs on malformed PDFs.
    """
    text = _pymupdf_convert_with_timeout(file_path)

    if text and _quality_ok(text):
        return text

    if text:
        return text

    logger.warning("PDF conversion produced no usable text: %s", file_path)
    return None


def _pymupdf_worker(file_path_str: str, result_queue: multiprocessing.Queue) -> None:
    """Worker function that runs in a subprocess."""
    try:
        import pymupdf4llm

        text = pymupdf4llm.to_markdown(file_path_str)
        if text and text.strip():
            result_queue.put(text.strip())
        else:
            result_queue.put(None)
    except Exception as e:
        result_queue.put(e)


def _pymupdf_convert_with_timeout(file_path: Path) -> str | None:
    """Run PyMuPDF conversion in a subprocess with a timeout."""
    ctx = multiprocessing.get_context("spawn")
    result_queue: multiprocessing.Queue = ctx.Queue()
    proc = ctx.Process(
        target=_pymupdf_worker,
        args=(str(file_path), result_queue),
        daemon=True,
    )
    proc.start()
    proc.join(timeout=CONVERSION_TIMEOUT_SECONDS)

    if proc.is_alive():
        logger.warning(
            "PDF conversion timed out after %ds, killing: %s",
            CONVERSION_TIMEOUT_SECONDS,
            file_path,
        )
        proc.kill()
        proc.join(timeout=5)
        return None

    if result_queue.empty():
        logger.warning("PDF conversion produced no result: %s", file_path)
        return None

    result = result_queue.get_nowait()
    if isinstance(result, Exception):
        logger.error("PyMuPDF4LLM failed: %s — %s", file_path, result)
        return None
    return result


def _quality_ok(text: str) -> bool:
    """Basic quality check — reject near-empty or garbled output."""
    stripped = text.strip()
    if len(stripped) < 50:
        return False
    alpha_ratio = sum(1 for c in stripped if c.isalpha()) / max(len(stripped), 1)
    return not alpha_ratio < 0.3
