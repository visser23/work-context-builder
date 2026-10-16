"""Text-only converters for ODF, draw.io, heavy decks and LibreOffice-only formats."""

from __future__ import annotations

import base64
import subprocess
import urllib.parse
import zipfile
import zlib
from pathlib import Path

import pytest

from workctx.normalise import fallbacks as fb
from workctx.normalise import office
from workctx.normalise.convertibility import can_convert, should_skip_download

T = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
TB = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
D = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"


def odf(path: Path, body: str) -> Path:
    xml = (
        f'<office:document-content xmlns:office="{OFFICE}" xmlns:text="{T}" '
        f'xmlns:table="{TB}" xmlns:draw="{D}"><office:body>{body}</office:body>'
        "</office:document-content>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("content.xml", xml)
    return path


class TestOdf:
    def test_text_document(self, tmp_path):
        path = odf(
            tmp_path / "a.odt",
            '<office:text><text:h text:outline-level="2">Scope</text:h>'
            '<text:p>Hello<text:s text:c="2"/>world<text:line-break/>next</text:p>'
            "<text:list><text:list-item><text:p>one</text:p></text:list-item>"
            "<text:list-item><text:p>two</text:p></text:list-item></text:list>"
            "</office:text>",
        )

        md = fb.convert_odf(path)

        assert "## Scope" in md
        assert "Hello world" in md and "next" in md
        assert "- one" in md and "- two" in md

    def test_spreadsheet_becomes_tables_per_sheet(self, tmp_path):
        path = odf(
            tmp_path / "a.ods",
            '<office:spreadsheet><table:table table:name="Budget">'
            "<table:table-row><table:table-cell><text:p>Item</text:p></table:table-cell>"
            "<table:table-cell><text:p>Cost</text:p></table:table-cell></table:table-row>"
            "<table:table-row><table:table-cell><text:p>Kit | a</text:p></table:table-cell>"
            "<table:table-cell><text:p>10</text:p></table:table-cell>"
            '<table:table-cell table:number-columns-repeated="1000"/></table:table-row>'
            "</table:table></office:spreadsheet>",
        )

        md = fb.convert_odf(path)

        assert "## Budget" in md
        assert "| Item | Cost |" in md
        assert "Kit \\| a" in md
        assert md.count("|") < 40  # the 1000 empty repeated cells did not explode

    def test_presentation_lists_slide_text(self, tmp_path):
        path = odf(
            tmp_path / "a.odp",
            '<office:presentation><draw:page draw:name="Intro"><text:p>Welcome</text:p>'
            '</draw:page><draw:page draw:name="Plan"><text:p>Phase 1</text:p></draw:page>'
            "</office:presentation>",
        )

        md = fb.convert_odf(path)

        assert "## 1. Intro" in md and "- Welcome" in md and "## 2. Plan" in md

    def test_garbage_returns_none(self, tmp_path):
        bad = tmp_path / "bad.odt"
        bad.write_bytes(b"not a zip")

        assert fb.convert_odf(bad) is None

    def test_oversized_member_is_refused(self, tmp_path, monkeypatch):
        path = odf(tmp_path / "a.odt", "<office:text><text:p>x</text:p></office:text>")
        monkeypatch.setattr(fb, "MAX_MEMBER_BYTES", 10)

        assert fb.convert_odf(path) is None


class TestDrawio:
    MODEL = (
        '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<mxCell id="a" value="Start &lt;b&gt;here&lt;/b&gt;" vertex="1" parent="1"/>'
        '<mxCell id="b" value="Review" vertex="1" parent="1"/>'
        '<mxCell id="e" value="yes" edge="1" source="a" target="b" parent="1"/>'
        "</root></mxGraphModel>"
    )

    def test_plain_diagram(self, tmp_path):
        path = tmp_path / "x.drawio"
        path.write_text(f'<mxfile><diagram name="Flow">{self.MODEL}</diagram></mxfile>')

        md = fb.convert_drawio(path)

        assert "## Flow" in md and "- Start here" in md and "- Review" in md
        assert "Start here -> Review: yes" in md

    def test_compressed_diagram(self, tmp_path):
        comp = zlib.compressobj(wbits=-15)
        raw = comp.compress(urllib.parse.quote(self.MODEL).encode()) + comp.flush()
        path = tmp_path / "x.drawio"
        path.write_text(
            f'<mxfile><diagram name="Packed">{base64.b64encode(raw).decode()}</diagram></mxfile>'
        )

        md = fb.convert_drawio(path)

        assert "## Packed" in md and "Start here -> Review" in md

    def test_empty_or_bad_file(self, tmp_path):
        bad = tmp_path / "x.drawio"
        bad.write_text("<mxfile><diagram>%%%</diagram></mxfile>")

        assert fb.convert_drawio(bad) is None


def make_pptx(path: Path, slides: list[tuple[str, str]]) -> Path:
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, (text, note) in enumerate(slides, 1):
            zf.writestr(
                f"ppt/slides/slide{i}.xml",
                f'<p:sld xmlns:p="p" xmlns:a="{a}"><a:p><a:r><a:t>{text}</a:t></a:r></a:p>'
                f"<a:p><a:r><a:t>{i}</a:t></a:r></a:p></p:sld>",
            )
            if note:
                zf.writestr(
                    f"ppt/notesSlides/notesSlide{i}.xml",
                    f'<p:notes xmlns:p="p" xmlns:a="{a}"><a:p><a:r><a:t>{note}</a:t></a:r></a:p>'
                    "</p:notes>",
                )
        zf.writestr("ppt/media/huge.png", b"\0" * 4096)
    return path


class TestHeavyDeck:
    def test_text_only_extraction_orders_slides_and_includes_notes(self, tmp_path):
        deck = make_pptx(
            tmp_path / "d.pptx",
            [("First", ""), ("Second", "say this")] + [(f"S{n}", "") for n in range(3, 12)],
        )

        md = fb.convert_pptx_text_only(deck)

        assert md.index("## Slide 2") < md.index("## Slide 10")
        assert "- Second" in md and "**Notes:**" in md and "- say this" in md
        assert "- 1" not in md  # bare slide numbers dropped

    def test_deck_rejected_by_preflight_still_yields_text(self, tmp_path, monkeypatch):
        deck = make_pptx(tmp_path / "d.pptx", [("Budget plan", "")])
        monkeypatch.setattr(office, "_MAX_UNCOMPRESSED_SIZE", 100)

        md = office.convert_office(deck)

        assert md is not None and "Budget plan" in md

    def test_corrupt_deck_is_still_skipped(self, tmp_path):
        bad = tmp_path / "d.pptx"
        bad.write_bytes(b"nope")

        assert office.convert_office(bad) is None


def minimal_docx(path: Path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        zf.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        zf.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )


class TestRouting:
    def test_new_formats_are_accepted(self):
        for name in ("a.odt", "a.ods", "a.odp", "a.pptm", "a.dotx", "a.drawio", "a.vtt", "a.doc"):
            assert can_convert(name), name

    def test_metafile_pictures_are_skipped_up_front(self):
        assert should_skip_download("evidence.emf")[0]
        assert should_skip_download("photo.jpg")[0]

    def test_soffice_missing_means_no_conversion(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fb, "find_soffice", lambda: None)
        legacy = tmp_path / "old.doc"
        legacy.write_bytes(b"\xd0\xcf\x11\xe0 legacy")

        assert office.convert_office(legacy) is None

    def test_legacy_file_is_converted_via_soffice_result(self, tmp_path, monkeypatch):
        produced_dir = tmp_path / "w" / "out"
        produced_dir.mkdir(parents=True)
        minimal_docx(produced_dir / "in.docx", "Converted by LibreOffice")
        monkeypatch.setattr(fb, "convert_with_soffice", lambda p, t: produced_dir / "in.docx")
        legacy = tmp_path / "old.doc"
        legacy.write_bytes(b"\xd0\xcf\x11\xe0")

        md = office.convert_office(legacy)

        assert md is not None and "Converted by LibreOffice" in md
        assert not (tmp_path / "w").exists()  # temp dir removed


@pytest.mark.skipif(fb.find_soffice() is None, reason="LibreOffice not installed")
class TestRealLibreOffice:
    def test_doc_round_trip(self, tmp_path):
        src = tmp_path / "note.txt"
        src.write_text("Legacy words survive\n")
        binary = fb.find_soffice()
        subprocess.run(
            [
                binary,
                "--headless",
                f"-env:UserInstallation=file://{tmp_path}/p",
                "--convert-to",
                "doc",
                "--outdir",
                str(tmp_path),
                str(src),
            ],
            capture_output=True,
            timeout=120,
            check=True,
        )

        md = office.convert_office(tmp_path / "note.doc")

        assert md is not None and "Legacy words survive" in md
