"""PDF conversion using PyMuPDF4LLM (primary) with optional Docling fallback."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def can_handle(file_path: Path) -> bool:
    return file_path.suffix.lower() == ".pdf"


def convert_pdf(file_path: Path, *, use_docling_fallback: bool = False) -> str | None:
    """Convert a PDF to Markdown.

    Primary: PyMuPDF4LLM (fast, layout-aware).
    Fallback: Docling if primary extraction is poor and Docling is installed.
    """
    text = _pymupdf_convert(file_path)

    if text and _quality_ok(text):
        return text

    if use_docling_fallback:
        logger.info("Trying Docling fallback for: %s", file_path)
        docling_text = _docling_convert(file_path)
        if docling_text and _quality_ok(docling_text):
            return docling_text

    if text:
        return text

    logger.warning("PDF conversion produced no usable text: %s", file_path)
    return None


def _pymupdf_convert(file_path: Path) -> str | None:
    try:
        import pymupdf4llm

        text = pymupdf4llm.to_markdown(str(file_path))
        if text and text.strip():
            return text.strip()
        return None
    except Exception:
        logger.error("PyMuPDF4LLM failed: %s", file_path, exc_info=True)
        return None


def _docling_convert(file_path: Path) -> str | None:
    try:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        result = converter.convert(str(file_path))
        text = result.document.export_to_markdown()
        if text and text.strip():
            return text.strip()
        return None
    except ImportError:
        logger.debug("Docling not installed")
        return None
    except Exception:
        logger.error("Docling failed: %s", file_path, exc_info=True)
        return None


def _quality_ok(text: str) -> bool:
    """Basic quality check — reject near-empty or garbled output."""
    stripped = text.strip()
    if len(stripped) < 50:
        return False
    alpha_ratio = sum(1 for c in stripped if c.isalpha()) / max(len(stripped), 1)
    return not alpha_ratio < 0.3
