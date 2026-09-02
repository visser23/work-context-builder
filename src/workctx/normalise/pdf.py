"""PDF conversion using PyMuPDF4LLM via subprocess with a hard timeout."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

logger = logging.getLogger(__name__)

CONVERSION_TIMEOUT_SECONDS = 30

_WORKER_SCRIPT = textwrap.dedent("""\
    import json, sys
    try:
        import pymupdf4llm
        text = pymupdf4llm.to_markdown(sys.argv[1])
        json.dump({"ok": True, "text": text.strip() if text else ""}, sys.stdout)
    except Exception as e:
        json.dump({"ok": False, "error": str(e)}, sys.stdout)
""")


def can_handle(file_path: Path) -> bool:
    return file_path.suffix.lower() == ".pdf"


def convert_pdf(file_path: Path, *, use_docling_fallback: bool = False) -> str | None:
    """Convert a PDF to Markdown.

    Runs pymupdf4llm in a subprocess with a hard timeout so a hung PDF
    never blocks the calling thread for more than CONVERSION_TIMEOUT_SECONDS.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _WORKER_SCRIPT, str(file_path)],
            capture_output=True,
            text=True,
            timeout=CONVERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "PDF conversion timed out after %ds: %s",
            CONVERSION_TIMEOUT_SECONDS,
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

    if _quality_ok(text):
        return text

    return text


def _quality_ok(text: str) -> bool:
    """Basic quality check — reject near-empty or garbled output."""
    stripped = text.strip()
    if len(stripped) < 50:
        return False
    alpha_ratio = sum(1 for c in stripped if c.isalpha()) / max(len(stripped), 1)
    return not alpha_ratio < 0.3
