"""Tests for file convertibility checking and pre-flight logic."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from workctx.normalise.convertibility import (
    can_convert,
    should_skip_download,
)
from workctx.normalise.office import preflight_office
from workctx.normalise.pdf import preflight_pdf


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("doc.docx", True),
        ("report.pdf", True),
        ("code.py", True),
        ("config.yaml", True),
        ("readme.md", True),
        ("data.csv", True),
        ("notes.txt", True),
        ("script.sh", True),
        ("app.tsx", True),
        ("video.mov", False),
        ("song.mp3", False),
        ("photo.jpg", False),
        ("design.fig", False),
        ("image.png", False),
        ("binary.exe", False),
        ("font.ttf", False),
        ("movie.mp4", False),
        ("archive.dmg", False),
    ],
)
def test_can_convert(filename: str, expected: bool) -> None:
    assert can_convert(filename) == expected


def test_can_convert_rejects_unknown_extensions() -> None:
    """Default-deny: unknown extensions should not be accepted."""
    assert can_convert("thing.literallyanything") is False
    assert can_convert("data.xyz") is False
    assert can_convert("report.custom") is False


def test_can_convert_rejects_no_extension() -> None:
    assert can_convert("Makefile") is False
    assert can_convert("README") is False


def test_should_skip_download_by_extension() -> None:
    skip, reason = should_skip_download("video.mov")
    assert skip is True
    assert "unconvertible" in reason

    skip, reason = should_skip_download("doc.docx")
    assert skip is False


def test_should_skip_download_by_size() -> None:
    skip, reason = should_skip_download("huge.docx", 300 * 1024 * 1024)
    assert skip is True
    assert "too large" in reason

    skip, reason = should_skip_download("small.docx", 1024)
    assert skip is False


def test_should_skip_download_size_under_limit() -> None:
    skip, _ = should_skip_download("report.pdf", 50 * 1024 * 1024)
    assert skip is False


# ------------------------------------------------------------------
# PDF pre-flight tests
# ------------------------------------------------------------------

class TestPreflightPdf:
    def test_missing_file(self, tmp_path: Path) -> None:
        result = preflight_pdf(tmp_path / "nonexistent.pdf")
        assert result.should_process is False
        assert "unreadable" in result.skip_reason

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.pdf"
        p.write_bytes(b"")
        result = preflight_pdf(p)
        assert result.should_process is False
        assert "empty" in result.skip_reason

    def test_corrupt_file(self, tmp_path: Path) -> None:
        p = tmp_path / "garbage.pdf"
        p.write_bytes(b"not a pdf at all")
        result = preflight_pdf(p)
        assert result.should_process is False

    def test_timeout_scales_with_pages(self, tmp_path: Path) -> None:
        """The adaptive timeout formula should be BASE + pages * PER_PAGE."""
        from unittest.mock import MagicMock, patch

        from workctx.normalise.pdf import BASE_TIMEOUT_SECONDS, SECONDS_PER_PAGE

        mock_doc = MagicMock()
        mock_doc.is_encrypted = False
        mock_doc.page_count = 100
        mock_page = MagicMock()
        mock_page.get_text.return_value = "A" * 200
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)

        p = tmp_path / "big.pdf"
        p.write_bytes(b"%PDF-1.4 fake")

        with patch("fitz.open", return_value=mock_doc):
            result = preflight_pdf(p)

        assert result.should_process is True
        assert result.timeout == int(BASE_TIMEOUT_SECONDS + 100 * SECONDS_PER_PAGE)
        assert result.page_count == 100

    def test_encrypted_skipped(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        mock_doc = MagicMock()
        mock_doc.is_encrypted = True

        p = tmp_path / "locked.pdf"
        p.write_bytes(b"%PDF-1.4 fake")

        with patch("fitz.open", return_value=mock_doc):
            result = preflight_pdf(p)

        assert result.should_process is False
        assert "encrypted" in result.skip_reason

    def test_image_only_skipped(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        mock_doc = MagicMock()
        mock_doc.is_encrypted = False
        mock_doc.page_count = 10
        mock_page = MagicMock()
        mock_page.get_text.return_value = ""
        mock_doc.__getitem__ = MagicMock(return_value=mock_page)

        p = tmp_path / "scanned.pdf"
        p.write_bytes(b"%PDF-1.4 fake")

        with patch("fitz.open", return_value=mock_doc):
            result = preflight_pdf(p)

        assert result.should_process is False
        assert "image-only" in result.skip_reason


# ------------------------------------------------------------------
# Office pre-flight tests
# ------------------------------------------------------------------

class TestPreflightOffice:
    def test_non_ooxml_passes(self, tmp_path: Path) -> None:
        p = tmp_path / "doc.rtf"
        p.write_text("hello")
        ok, reason = preflight_office(p)
        assert ok is True
        assert reason == ""

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.docx"
        p.write_bytes(b"")
        ok, reason = preflight_office(p)
        assert ok is False
        assert "empty" in reason

    def test_encrypted_docx(self, tmp_path: Path) -> None:
        p = tmp_path / "locked.docx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("EncryptedPackage", b"x")
        ok, reason = preflight_office(p)
        assert ok is False
        assert "encrypted" in reason

    def test_valid_docx_passes(self, tmp_path: Path) -> None:
        p = tmp_path / "good.docx"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("word/document.xml", "<body>hello</body>")
        ok, _reason = preflight_office(p)
        assert ok is True

    def test_corrupt_zip(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.xlsx"
        p.write_bytes(b"PK\x03\x04corrupted")
        ok, _reason = preflight_office(p)
        assert ok is False
