"""OCR of scanned PDFs (engine faked; the real Vision/Tesseract engines are optional)."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from workctx.normalise import ocr
from workctx.normalise.pdf import convert_pdf


def scanned_pdf(path: Path, pages: int = 2) -> Path:
    """A PDF whose pages are only an image (no text layer), like a scan."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
    pix.set_rect(pix.irect, (200, 200, 200))
    png = pix.tobytes("png")
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, stream=png)
    doc.save(str(path))
    doc.close()
    return path


class TestOcrPdf:
    def test_text_is_collected_per_page_and_labelled(self, tmp_path):
        pdf = scanned_pdf(tmp_path / "s.pdf", pages=2)
        replies = iter(["Cyber Essentials Plus certificate of assurance", "Valid until 2027-05-26"])

        md = ocr.ocr_pdf(pdf, engine=lambda png: next(replies))

        assert md.startswith(ocr.OCR_NOTICE)
        assert "## Page 1" in md and "Cyber Essentials Plus" in md
        assert "## Page 2" in md and "Valid until" in md

    def test_picture_without_text_is_not_turned_into_noise(self, tmp_path):
        pdf = scanned_pdf(tmp_path / "photo.pdf", pages=1)

        assert ocr.ocr_pdf(pdf, engine=lambda png: "ab ~ |") is None

    def test_no_engine_means_no_output(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ocr, "get_engine", lambda: None)

        assert ocr.ocr_pdf(scanned_pdf(tmp_path / "s.pdf")) is None

    def test_page_cap_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ocr, "OCR_MAX_PAGES", 1)
        pdf = scanned_pdf(tmp_path / "s.pdf", pages=3)

        md = ocr.ocr_pdf(pdf, engine=lambda png: "A reasonably long line of recognised words")

        assert "OCR stopped after 1 of 3 pages" in md
        assert "## Page 2" not in md

    def test_engine_crash_degrades_to_none(self, tmp_path):
        def boom(png):
            raise RuntimeError("model missing")

        assert ocr.ocr_pdf(scanned_pdf(tmp_path / "s.pdf"), engine=boom) is None


class TestPdfRouting:
    def test_scanned_pdf_goes_through_ocr(self, tmp_path, monkeypatch):
        pdf = scanned_pdf(tmp_path / "s.pdf")
        monkeypatch.setattr(
            ocr, "get_engine", lambda: lambda png: "Signed agreement between the two parties"
        )

        md = convert_pdf(pdf)

        assert md is not None and "Signed agreement" in md

    def test_scanned_pdf_without_engine_is_still_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ocr, "get_engine", lambda: None)

        assert convert_pdf(scanned_pdf(tmp_path / "s.pdf")) is None

    def test_corrupt_pdf_never_reaches_ocr(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"%PDF-1.4 garbage")
        monkeypatch.setattr(ocr, "ocr_pdf", lambda p: pytest.fail("OCR must not run"))

        assert convert_pdf(bad) is None
