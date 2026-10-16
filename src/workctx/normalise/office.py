"""Office document conversion using MarkItDown with pre-flight checks."""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from workctx.normalise import fallbacks as fb

if TYPE_CHECKING:
    from markitdown import MarkItDown

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
    # handled by normalise.fallbacks (pure Python or LibreOffice)
    ".odt",
    ".ods",
    ".odp",
    ".pptm",
    ".ppsx",
    ".potx",
    ".docm",
    ".dotx",
    ".dotm",
    ".dot",
    ".drawio",
}

_MARKITDOWN_CANNOT = {
    ".doc",
    ".dot",
    ".dotx",
    ".dotm",
    ".docm",
    ".ppt",
    ".pptm",
    ".ppsx",
    ".potx",
    ".xlsb",
    ".odt",
    ".ods",
    ".odp",
    ".drawio",
}
"""Formats MarkItDown has no converter for: go straight to the fallbacks."""

_OOXML_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".xlsm", ".xlsb", ".epub"}
_MAX_UNCOMPRESSED_SIZE = 150 * 1024 * 1024

_converter: MarkItDown | None = None


def _get_converter() -> MarkItDown:
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

            if "EncryptedPackage" in zf.namelist():
                return False, "encrypted/password-protected"

            total_uncompressed = sum(info.file_size for info in zf.infolist())
            if total_uncompressed > _MAX_UNCOMPRESSED_SIZE:
                mb = total_uncompressed / (1024 * 1024)
                return False, f"uncompressed content too large ({mb:.0f} MB)"

            if suffix == ".pptx":
                media_files = [f for f in zf.namelist() if f.startswith("ppt/media/")]
                media_size = sum(zf.getinfo(f).file_size for f in media_files)
                if media_size > 80 * 1024 * 1024:
                    mb = media_size / (1024 * 1024)
                    return False, f"heavy embedded media ({mb:.0f} MB)"

    except zipfile.BadZipFile:
        return False, "not a valid ZIP/OOXML file"
    except Exception:
        pass

    return True, ""


def convert_office(file_path: Path, *, _allow_fallback: bool = True) -> str | None:
    """Convert an Office/supported document to Markdown.

    Runs a pre-flight check on OOXML formats first to skip files that are encrypted,
    corrupt, or bloated with embedded media (decks that are only too heavy still yield
    their slide text). Formats MarkItDown cannot read, and files it fails on, go through
    :mod:`workctx.normalise.fallbacks` (pure-Python ODF/draw.io readers, then LibreOffice
    if installed). Returns Markdown text or None if nothing could be extracted.
    """
    suffix = file_path.suffix.lower()

    if suffix in fb.PURE_PYTHON_EXTENSIONS:
        text = (
            fb.convert_odf(file_path)
            if suffix in fb.ODF_EXTENSIONS
            else fb.convert_drawio(file_path)
        )
        if text:
            return text

    should_process, reason = preflight_office(file_path)
    if not should_process:
        logger.info("Office skipped (pre-flight): %s — %s", file_path.name, reason)
        if suffix == ".pptx" and reason.startswith(("heavy embedded media", "uncompressed")):
            text = fb.convert_pptx_text_only(file_path)
            if text:
                logger.info("Recovered slide text only: %s", file_path.name)
                return text
        return None

    if suffix not in _MARKITDOWN_CANNOT:
        try:
            result = _get_converter().convert(str(file_path))
            text = result.text_content if hasattr(result, "text_content") else str(result)
            if text and text.strip():
                return text.strip()
            logger.warning("Empty conversion result: %s", file_path)
        except Exception:
            logger.error("MarkItDown conversion failed: %s", file_path, exc_info=True)

    if _allow_fallback and suffix in fb.SOFFICE_TARGETS:
        return _convert_via_soffice(file_path, suffix)
    return None


def _convert_via_soffice(file_path: Path, suffix: str) -> str | None:
    produced = fb.convert_with_soffice(file_path, fb.SOFFICE_TARGETS[suffix])
    if produced is None:
        logger.info("No LibreOffice conversion available for %s", file_path.name)
        return None
    try:
        text = convert_office(produced, _allow_fallback=False)
        if text:
            logger.info("Converted %s via LibreOffice", file_path.name)
        return text
    finally:
        fb.cleanup_soffice_result(produced)
