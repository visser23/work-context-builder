"""Office document conversion using MarkItDown."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".xlsx",
    ".xls",
    ".xlsm",
    ".xlsb",
    ".csv",
    ".tsv",
    ".html",
    ".htm",
    ".mhtml",
    ".mht",
    ".rtf",
    ".msg",
    ".eml",
    ".json",
    ".jsonl",
    ".xml",
    ".zip",
    ".epub",
    ".ipynb",
}

_converter = None


def _get_converter():  # type: ignore[no-untyped-def]
    global _converter
    if _converter is None:
        from markitdown import MarkItDown

        _converter = MarkItDown()
    return _converter


def can_handle(file_path: Path) -> bool:
    return file_path.suffix.lower() in SUPPORTED_EXTENSIONS


def convert_office(file_path: Path) -> str | None:
    """Convert an Office/supported document to Markdown using MarkItDown.

    Returns Markdown text or None if conversion fails.
    """
    try:
        converter = _get_converter()
        result = converter.convert(str(file_path))
        text = result.text_content if hasattr(result, "text_content") else str(result)
        if not text or not text.strip():
            logger.warning("Empty conversion result: %s", file_path)
            return None
        return text.strip()
    except Exception:
        logger.error("MarkItDown conversion failed: %s", file_path, exc_info=True)
        return None
