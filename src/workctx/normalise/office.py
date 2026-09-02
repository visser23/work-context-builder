"""Office document conversion using MarkItDown with pre-flight checks."""

from __future__ import annotations

import logging
import zipfile
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

# Extensions where the zip-based pre-flight is applicable
_OOXML_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".xlsm", ".xlsb", ".epub"}

# Skip OOXML files whose uncompressed content exceeds this (150 MB)
_MAX_UNCOMPRESSED_SIZE = 150 * 1024 * 1024

_converter = None


def _get_converter():  # type: ignore[no-untyped-def]
    global _converter
    if _converter is None:
        from markitdown import MarkItDown

        _converter = MarkItDown()
    return _converter


def can_handle(file_path: Path) -> bool:
    return file_path.suffix.lower() in SUPPORTED_EXTENSIONS


def preflight_office(file_path: Path) -> tuple[bool, str]:
    """Quick sanity check on Office files before expensive conversion.

    OOXML formats (.docx, .pptx, .xlsx etc.) are ZIP archives — we can
    cheaply peek at the manifest to detect embedded media bloat or
    password protection without reading any content.

    Returns (should_process, skip_reason).
    """
    suffix = file_path.suffix.lower()

    if suffix not in _OOXML_EXTENSIONS:
        return True, ""

    try:
        file_size = file_path.stat().st_size
    except OSError:
        return False, "unreadable file"

    if file_size == 0:
        return False, "empty file"

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if zf.testzip() is not None:
                return False, "corrupt ZIP structure"

            # Check for encryption marker
            if "EncryptedPackage" in zf.namelist():
                return False, "encrypted/password-protected"

            # Calculate total uncompressed size
            total_uncompressed = sum(info.file_size for info in zf.infolist())
            if total_uncompressed > _MAX_UNCOMPRESSED_SIZE:
                mb = total_uncompressed / (1024 * 1024)
                return False, f"uncompressed content too large ({mb:.0f} MB)"

            # For PPTX: count slides and check for huge embedded media
            if suffix == ".pptx":
                media_files = [
                    f for f in zf.namelist()
                    if f.startswith("ppt/media/")
                ]
                media_size = sum(
                    zf.getinfo(f).file_size
                    for f in media_files
                    if f in [i.filename for i in zf.infolist()]
                )
                if media_size > 80 * 1024 * 1024:
                    mb = media_size / (1024 * 1024)
                    return False, f"heavy embedded media ({mb:.0f} MB)"

    except zipfile.BadZipFile:
        return False, "not a valid ZIP/OOXML file"
    except Exception:
        pass

    return True, ""


def convert_office(file_path: Path) -> str | None:
    """Convert an Office/supported document to Markdown using MarkItDown.

    Runs a pre-flight check on OOXML formats first to skip files that
    are encrypted, corrupt, or bloated with embedded media.
    Returns Markdown text or None if conversion fails.
    """
    should_process, reason = preflight_office(file_path)
    if not should_process:
        logger.info(
            "Office skipped (pre-flight): %s — %s", file_path.name, reason,
        )
        return None

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
