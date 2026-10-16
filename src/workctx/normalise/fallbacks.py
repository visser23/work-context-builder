"""Converters for formats MarkItDown cannot (or should not) handle.

Everything here is *text-only*: images, media and layout are deliberately ignored, so a
deck stuffed with photos still yields its slide text, and a diagram yields its labels and
connections.

* OpenDocument (``.odt`` / ``.ods`` / ``.odp``) - pure Python, reads ``content.xml``.
* draw.io diagrams (``.drawio``) - node labels and edges, compressed or not.
* PowerPoint text-only extraction - used when a deck is too heavy for the normal route.
* LibreOffice (``soffice --headless``) - optional, detected on PATH / standard locations;
  turns legacy or exotic Office files (``.doc``, ``.xlsb``, ``.pptm`` ...) into OOXML
  that the normal converter understands.

All XML is parsed from size-capped members of a ZIP (or capped decompression) so a
hostile file cannot exhaust memory.
"""

from __future__ import annotations

import base64
import html
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_MEMBER_BYTES = 60 * 1024 * 1024
"""Largest XML member (uncompressed) we are willing to parse."""
MAX_OUTPUT_CHARS = 5_000_000
MAX_SHEET_ROWS = 50_000
SOFFICE_TIMEOUT_SECONDS = 180

ODF_EXTENSIONS = {".odt", ".ods", ".odp"}
DRAWIO_EXTENSIONS = {".drawio"}
PURE_PYTHON_EXTENSIONS = ODF_EXTENSIONS | DRAWIO_EXTENSIONS

SOFFICE_TARGETS = {
    ".doc": ".docx",
    ".dot": ".docx",
    ".dotx": ".docx",
    ".dotm": ".docx",
    ".docm": ".docx",
    ".rtf": ".docx",
    ".odt": ".docx",
    ".wpd": ".docx",
    ".xlsb": ".xlsx",
    ".xls": ".xlsx",
    ".ods": ".xlsx",
    ".ppt": ".pptx",
    ".pptm": ".pptx",
    ".ppsx": ".pptx",
    ".potx": ".pptx",
    ".odp": ".pptx",
}
"""Extension -> OOXML extension LibreOffice should produce."""

_SOFFICE_LOCK = threading.Lock()  # one LibreOffice at a time (shared user profile)
_SOFFICE_CANDIDATES = (
    "/opt/homebrew/bin/soffice",
    "/usr/local/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
)


# --------------------------------------------------------------------------- helpers


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_member(zf, name: str) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_MEMBER_BYTES:
        logger.warning("Skipping oversized archive member %s (%d bytes)", name, info.file_size)
        return None
    with zf.open(info) as handle:
        data = handle.read(MAX_MEMBER_BYTES + 1)
    return data if len(data) <= MAX_MEMBER_BYTES else None


def _clean(text: str) -> str:
    return re.sub(r"[ \t\u00a0]+", " ", text).strip()


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------- OpenDocument


def convert_odf(file_path: Path) -> str | None:
    """Extract text from an OpenDocument text, spreadsheet or presentation."""
    import zipfile

    try:
        with zipfile.ZipFile(file_path) as zf:
            data = _read_member(zf, "content.xml")
    except (zipfile.BadZipFile, OSError):
        logger.warning("Not a readable OpenDocument file: %s", file_path.name)
        return None
    if data is None:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        logger.warning("Unparseable OpenDocument content: %s", file_path.name)
        return None

    out: list[str] = []
    suffix = file_path.suffix.lower()
    body = next((el for el in root.iter() if _local(el.tag) == "body"), root)
    if suffix == ".ods":
        _odf_sheets(body, out)
    elif suffix == ".odp":
        _odf_slides(body, out)
    else:
        _odf_blocks(body, out)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_OUTPUT_CHARS] or None


def _odf_inline_text(el: ET.Element) -> str:
    """Text of a paragraph, honouring spaces, tabs, line breaks and links."""
    parts: list[str] = []

    def walk(node: ET.Element) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            name = _local(child.tag)
            if name == "s":
                count = int(child.attrib.get(f"{{{_NS_TEXT}}}c", "1") or 1)
                parts.append(" " * min(count, 20))
            elif name == "tab":
                parts.append(" ")
            elif name == "line-break":
                parts.append("\n")
            elif name in ("note", "annotation"):
                pass  # footnotes / comments: skip
            else:
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(el)
    return _clean("".join(parts))


_NS_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_NS_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_NS_DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"


def _odf_blocks(node: ET.Element, out: list[str], list_depth: int = 0) -> None:
    for child in node:
        name = _local(child.tag)
        if name == "h":
            level = int(child.attrib.get(f"{{{_NS_TEXT}}}outline-level", "1") or 1)
            text = _odf_inline_text(child)
            if text:
                out.extend(["", f"{'#' * min(max(level, 1), 6)} {text}", ""])
        elif name == "p":
            text = _odf_inline_text(child)
            if text:
                out.extend([text, ""])
        elif name == "list":
            _odf_blocks_list(child, out, list_depth)
        elif name == "table":
            _odf_table(child, out)
        elif name in ("section", "frame", "text-box", "body", "text"):
            _odf_blocks(child, out, list_depth)


def _odf_blocks_list(node: ET.Element, out: list[str], depth: int) -> None:
    for item in node:
        if _local(item.tag) not in ("list-item", "list-header"):
            continue
        for sub in item:
            sname = _local(sub.tag)
            if sname in ("p", "h"):
                text = _odf_inline_text(sub)
                if text:
                    out.append(f"{'  ' * depth}- {text}")
            elif sname == "list":
                _odf_blocks_list(sub, out, depth + 1)
    out.append("")


def _odf_rows(table: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.iter(f"{{{_NS_TABLE}}}table-row"):
        repeat = int(row.attrib.get(f"{{{_NS_TABLE}}}number-rows-repeated", "1") or 1)
        cells: list[str] = []
        for cell in row:
            if _local(cell.tag) not in ("table-cell", "covered-table-cell"):
                continue
            text = " ".join(
                t for p in cell if _local(p.tag) in ("p", "h") if (t := _odf_inline_text(p))
            )
            reps = int(cell.attrib.get(f"{{{_NS_TABLE}}}number-columns-repeated", "1") or 1)
            cells.extend([text] * min(reps, 1 if not text else 50))
        while cells and not cells[-1]:
            cells.pop()
        if cells:
            rows.extend([cells] * min(repeat, 5))
        if len(rows) > MAX_SHEET_ROWS:
            break
    return rows


def _md_table(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    width = max(len(r) for r in rows)
    norm = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(_escape_cell(c) for c in norm[0]) + " |"]
    lines.append("|" + "|".join([" --- "] * width) + "|")
    lines.extend("| " + " | ".join(_escape_cell(c) for c in r) + " |" for r in norm[1:])
    return [*lines, ""]


def _odf_table(table: ET.Element, out: list[str]) -> None:
    out.extend(_md_table(_odf_rows(table)))


def _odf_sheets(body: ET.Element, out: list[str]) -> None:
    for table in body.iter(f"{{{_NS_TABLE}}}table"):
        name = table.attrib.get(f"{{{_NS_TABLE}}}name", "Sheet")
        rows = _odf_rows(table)
        if rows:
            out.extend(["", f"## {name}", "", *_md_table(rows)])


def _odf_slides(body: ET.Element, out: list[str]) -> None:
    for number, page in enumerate(body.iter(f"{{{_NS_DRAW}}}page"), 1):
        name = page.attrib.get(f"{{{_NS_DRAW}}}name") or f"Slide {number}"
        texts: list[str] = []
        for node in page.iter():
            if _local(node.tag) in ("p", "h") and node.tag.startswith(f"{{{_NS_TEXT}}}"):
                text = _odf_inline_text(node)
                if text:
                    texts.append(text)
        if texts:
            out.extend(["", f"## {number}. {name}", "", *[f"- {t}" for t in texts], ""])


# --------------------------------------------------------------------------- draw.io


def _decode_drawio_diagram(diagram: ET.Element) -> ET.Element | None:
    inner = next((c for c in diagram if _local(c.tag) == "mxGraphModel"), None)
    if inner is not None:
        return inner
    payload = (diagram.text or "").strip()
    if not payload:
        return None
    try:
        raw = base64.b64decode(payload)
        inflater = zlib.decompressobj(-15)
        xml = inflater.decompress(raw, MAX_MEMBER_BYTES)
        return ET.fromstring(urllib.parse.unquote(xml.decode("utf-8", errors="replace")))
    except (ValueError, zlib.error, ET.ParseError):
        return None


def _label(value: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return _clean(html.unescape(text).replace("\n", " / "))


def convert_drawio(file_path: Path) -> str | None:
    """Labels and connections of every page of a draw.io diagram."""
    try:
        data = file_path.read_bytes()
        if len(data) > MAX_MEMBER_BYTES:
            return None
        root = ET.fromstring(data)
    except (OSError, ET.ParseError):
        logger.warning("Unreadable draw.io file: %s", file_path.name)
        return None

    diagrams = [el for el in root.iter() if _local(el.tag) == "diagram"] or [root]
    out: list[str] = []
    for number, diagram in enumerate(diagrams, 1):
        model = _decode_drawio_diagram(diagram) if _local(diagram.tag) == "diagram" else diagram
        if model is None:
            continue
        labels: dict[str, str] = {}
        edges: list[tuple[str, str, str]] = []
        for cell in model.iter():
            if _local(cell.tag) != "mxCell":
                continue
            cid = cell.attrib.get("id", "")
            text = _label(cell.attrib.get("value", ""))
            if cell.attrib.get("edge") == "1":
                edges.append((cell.attrib.get("source", ""), cell.attrib.get("target", ""), text))
            elif text:
                labels[cid] = text
        # Cells can also be <object label="..."> wrappers
        for obj in model.iter():
            if _local(obj.tag) == "object":
                text = _label(obj.attrib.get("label", ""))
                if text:
                    labels[obj.attrib.get("id", "")] = text
        if not labels and not edges:
            continue
        title = diagram.attrib.get("name") or f"Page {number}"
        out.extend(["", f"## {title}", ""])
        out.extend(f"- {t}" for t in dict.fromkeys(labels.values()))
        links = [
            f"- {labels.get(s, '?')} -> {labels.get(t, '?')}" + (f": {txt}" if txt else "")
            for s, t, txt in edges
            if labels.get(s) or labels.get(t)
        ]
        if links:
            out.extend(["", "### Connections", "", *links])
    text = "\n".join(out).strip()
    return text[:MAX_OUTPUT_CHARS] or None


# --------------------------------------------------------------------------- PowerPoint text


def convert_pptx_text_only(file_path: Path) -> str | None:
    """Slide and speaker-note text of a deck, without touching any embedded media."""
    import zipfile

    ns_a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    try:
        zf = zipfile.ZipFile(file_path)
    except (zipfile.BadZipFile, OSError):
        return None
    out: list[str] = []
    with zf:
        slides = sorted(
            (n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.findall(r"\d+", n)[-1]),
        )
        for number, name in enumerate(slides, 1):
            texts = _pptx_paragraphs(_read_member(zf, name), ns_a)
            notes = _pptx_paragraphs(
                _read_member(zf, name.replace("slides/slide", "notesSlides/notesSlide")), ns_a
            )
            if not texts and not notes:
                continue
            out.extend(["", f"## Slide {number}", ""])
            out.extend(texts)
            if notes:
                out.extend(["", "**Notes:**", *notes])
    text = "\n".join(out).strip()
    return text[:MAX_OUTPUT_CHARS] or None


def _pptx_paragraphs(data: bytes | None, ns_a: str) -> list[str]:
    if not data:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    paragraphs: list[str] = []
    for para in root.iter(f"{{{ns_a}}}p"):
        text = _clean("".join(t.text or "" for t in para.iter(f"{{{ns_a}}}t")))
        if text and not re.fullmatch(r"\d{1,3}", text):  # bare slide numbers
            paragraphs.append(f"- {text}")
    return paragraphs


# --------------------------------------------------------------------------- LibreOffice


def find_soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    return next((c for c in _SOFFICE_CANDIDATES if Path(c).is_file()), None)


def convert_with_soffice(file_path: Path, target_suffix: str) -> Path | None:
    """Ask LibreOffice for an OOXML copy of ``file_path`` (caller deletes the result's dir).

    Returns the converted file inside a fresh temp directory, or ``None`` when LibreOffice
    is missing, times out or produces nothing.
    """
    binary = find_soffice()
    if not binary:
        return None
    workdir = Path(tempfile.mkdtemp(prefix="workctx-soffice-"))
    source = workdir / f"in{file_path.suffix.lower()}"
    try:
        shutil.copyfile(file_path, source)
        with _SOFFICE_LOCK:
            subprocess.run(
                [
                    binary,
                    "--headless",
                    "--norestore",
                    f"-env:UserInstallation=file://{workdir}/profile",
                    "--convert-to",
                    target_suffix.lstrip("."),
                    "--outdir",
                    str(workdir / "out"),
                    str(source),
                ],
                capture_output=True,
                timeout=SOFFICE_TIMEOUT_SECONDS,
                check=False,
            )
        produced = workdir / "out" / f"in{target_suffix}"
        if produced.is_file() and produced.stat().st_size > 0:
            return produced
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("LibreOffice conversion failed for %s: %s", file_path.name, exc)
    shutil.rmtree(workdir, ignore_errors=True)
    return None


def cleanup_soffice_result(produced: Path) -> None:
    """Remove the temp directory created by :func:`convert_with_soffice`."""
    shutil.rmtree(produced.parent.parent, ignore_errors=True)
