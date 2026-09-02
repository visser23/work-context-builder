"""Shared normalisation utilities: front matter, document splitting, slug generation."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from workctx.models import FrontMatter

HEADING_RE = re.compile(r"^(#{1,3})\s+", re.MULTILINE)


def slugify(text: str, max_length: int = 60) -> str:
    """Create a filesystem-safe slug from a title."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text[:max_length]


def wrap_with_front_matter(front_matter: FrontMatter, body: str) -> str:
    """Combine YAML front matter with Markdown body."""
    return f"{front_matter.to_yaml_str()}\n\n{body.strip()}\n"


def content_hash(text: str) -> str:
    """SHA-256 of normalised text content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_large_document(
    front_matter: FrontMatter,
    body: str,
    max_chars: int = 300_000,
    base_path: str = "",
) -> list[tuple[FrontMatter, str, str]]:
    """Split a large document into parts at heading boundaries.

    Returns list of (front_matter, body, output_filename) tuples.
    If the document fits in one part, returns a single-element list.
    """
    if len(body) <= max_chars:
        return [(front_matter, body, base_path)]

    sections = _split_at_headings(body, max_chars)
    total_parts = len(sections)
    parts: list[tuple[FrontMatter, str, str]] = []

    base = Path(base_path)
    parent = str(base.parent) if base.parent != Path(".") else ""
    stem = base.stem if base_path else "document"
    suffix = base.suffix if base_path else ".md"

    for i, section_body in enumerate(sections, 1):
        part_fm = front_matter.model_copy(
            update={
                "part_number": i,
                "total_parts": total_parts,
                "parent_source_id": front_matter.source_id,
            }
        )
        part_name = f"{stem}.part-{i:03d}{suffix}"
        part_path = f"{parent}/{part_name}" if parent else part_name
        parts.append((part_fm, section_body, part_path))

    return parts


def _split_at_headings(text: str, max_chars: int) -> list[str]:
    """Split text at heading boundaries, trying to stay under max_chars."""
    lines = text.split("\n")
    sections: list[str] = []
    current_section: list[str] = []
    current_length = 0

    for line in lines:
        is_heading = HEADING_RE.match(line) is not None
        line_len = len(line) + 1

        if is_heading and current_length + line_len > max_chars and current_section:
            sections.append("\n".join(current_section))
            current_section = []
            current_length = 0

        current_section.append(line)
        current_length += line_len

        if current_length > max_chars * 1.2 and not is_heading:
            sections.append("\n".join(current_section))
            current_section = []
            current_length = 0

    if current_section:
        sections.append("\n".join(current_section))

    return sections if sections else [text]
