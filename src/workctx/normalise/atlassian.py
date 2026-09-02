"""Atlassian format converters: Confluence storage XML and Jira ADF to Markdown."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Confluence Storage Format → Markdown
# ──────────────────────────────────────────────────────────────


def confluence_storage_to_markdown(storage_xml: str, title: str = "") -> str:
    """Convert Confluence storage format (XHTML-like) to clean Markdown."""
    if not storage_xml or not storage_xml.strip():
        return ""

    try:
        return _convert_storage_xml(storage_xml)
    except Exception:
        logger.warning("Confluence storage conversion failed, using basic fallback", exc_info=True)
        return _basic_strip_xml(storage_xml)


def _convert_storage_xml(xml: str) -> str:
    """Parse Confluence storage format and convert to Markdown."""
    text = xml

    text = re.sub(
        r"<ac:structured-macro[^>]*ac:name=\"([^\"]+)\"[^>]*>.*?</ac:structured-macro>",
        lambda m: _handle_macro(m.group(1), m.group(0)),
        text,
        flags=re.DOTALL,
    )

    text = re.sub(r"<h1[^>]*>(.*?)</h1>", r"\n# \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n## \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<h3[^>]*>(.*?)</h3>", r"\n### \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<h4[^>]*>(.*?)</h4>", r"\n#### \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<h5[^>]*>(.*?)</h5>", r"\n##### \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<h6[^>]*>(.*?)</h6>", r"\n###### \1\n", text, flags=re.DOTALL)

    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\n\1\n", text, flags=re.DOTALL)
    text = re.sub(
        r"<blockquote[^>]*>(.*?)</blockquote>",
        lambda m: _blockquote(m.group(1)),
        text,
        flags=re.DOTALL,
    )

    text = re.sub(r"<ul[^>]*>(.*?)</ul>", r"\1\n", text, flags=re.DOTALL)
    text = re.sub(r"<ol[^>]*>(.*?)</ol>", r"\1\n", text, flags=re.DOTALL)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", text, flags=re.DOTALL)

    text = _convert_tables(text)

    text = re.sub(
        r"<a[^>]*href=[\"'](.*?)[\"'][^>]*>(.*?)</a>",
        r"[\2](\1)",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"<ac:link[^>]*>.*?<ri:page[^>]*ri:content-title=\"([^\"]+)\"[^>]*/>"
        r".*?</ac:link>",
        r"[\1]",
        text,
        flags=re.DOTALL,
    )

    text = re.sub(r"<strong[^>]*>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<b[^>]*>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<em[^>]*>(.*?)</em>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<i[^>]*>(.*?)</i>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<pre[^>]*>(.*?)</pre>", r"\n```\n\1\n```\n", text, flags=re.DOTALL)

    text = re.sub(
        r"<ac:task-list[^>]*>(.*?)</ac:task-list>",
        r"\1",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"<ac:task[^>]*>.*?<ac:task-status>(.*?)</ac:task-status>.*?<ac:task-body>(.*?)</ac:task-body>.*?</ac:task>",
        lambda m: f"\n- [{'x' if m.group(1) == 'complete' else ' '}] {m.group(2).strip()}",
        text,
        flags=re.DOTALL,
    )

    text = re.sub(r"<ri:user[^>]*ri:userkey=\"[^\"]*\"[^>]*/?>", "", text)
    text = re.sub(r"<ac:emoticon[^>]*/>", "", text)
    text = re.sub(r"<ac:image[^>]*>.*?</ac:image>", "[image]", text, flags=re.DOTALL)
    text = re.sub(r"<ri:attachment[^>]*ri:filename=\"([^\"]+)\"[^>]*/>", r"[attachment: \1]", text)

    text = re.sub(r"<[^>]+>", "", text)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _handle_macro(macro_name: str, full_match: str) -> str:
    """Handle Confluence macros with best-effort conversion."""
    body_match = re.search(
        r"<ac:rich-text-body>(.*?)</ac:rich-text-body>",
        full_match,
        re.DOTALL,
    )
    body = body_match.group(1) if body_match else ""

    if macro_name == "code":
        lang_match = re.search(
            r'<ac:parameter ac:name="language">(.*?)</ac:parameter>',
            full_match,
        )
        lang = lang_match.group(1) if lang_match else ""
        plain = re.sub(r"<[^>]+>", "", body)
        return f"\n```{lang}\n{plain.strip()}\n```\n"

    if macro_name in ("info", "note", "warning", "tip", "panel"):
        inner = re.sub(r"<[^>]+>", "", body)
        prefix = f"**{macro_name.title()}:** " if macro_name != "panel" else ""
        return f"\n> {prefix}{inner.strip()}\n"

    if macro_name == "expand":
        title_match = re.search(
            r'<ac:parameter ac:name="title">(.*?)</ac:parameter>',
            full_match,
        )
        title = title_match.group(1) if title_match else "Details"
        inner = re.sub(r"<[^>]+>", "", body)
        return f"\n**{title}**\n\n{inner.strip()}\n"

    if macro_name == "toc":
        return ""

    return (
        f"\n> [Confluence macro: {macro_name}]\n> This content could not be represented directly.\n"
    )


def _blockquote(text: str) -> str:
    plain = re.sub(r"<[^>]+>", "", text).strip()
    lines = plain.split("\n")
    return "\n" + "\n".join(f"> {line}" for line in lines) + "\n"


def _convert_tables(text: str) -> str:
    """Convert HTML tables to Markdown tables."""

    def _table_replace(match: re.Match[str]) -> str:
        table_html = match.group(0)
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL)
        if not rows:
            return table_html

        md_rows: list[str] = []
        for i, row_html in enumerate(rows):
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, re.DOTALL)
            clean_cells = [re.sub(r"<[^>]+>", "", c).strip().replace("|", "\\|") for c in cells]
            md_rows.append("| " + " | ".join(clean_cells) + " |")
            if i == 0:
                md_rows.append("|" + "|".join(" --- " for _ in clean_cells) + "|")

        return "\n" + "\n".join(md_rows) + "\n"

    return re.sub(r"<table[^>]*>.*?</table>", _table_replace, text, flags=re.DOTALL)


def _basic_strip_xml(xml: str) -> str:
    text = re.sub(r"<[^>]+>", " ", xml)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ──────────────────────────────────────────────────────────────
# Jira Atlassian Document Format (ADF) → Markdown
# ──────────────────────────────────────────────────────────────


def adf_to_markdown(adf: dict[str, Any]) -> str:
    """Convert Jira Atlassian Document Format to Markdown."""
    if not adf or not isinstance(adf, dict):
        return ""

    node_type = adf.get("type", "")

    if node_type == "doc":
        return _adf_children(adf)

    return _adf_node(adf)


def _adf_children(node: dict[str, Any]) -> str:
    content = node.get("content", [])
    parts = [_adf_node(child) for child in content]
    return "\n\n".join(p for p in parts if p)


def _adf_node(node: dict[str, Any]) -> str:
    node_type = node.get("type", "")
    handler = _ADF_HANDLERS.get(node_type, _adf_unknown)
    return handler(node)


def _adf_text(node: dict[str, Any]) -> str:
    text = node.get("text", "")
    marks = node.get("marks", [])
    for mark in marks:
        mark_type = mark.get("type", "")
        if mark_type == "strong":
            text = f"**{text}**"
        elif mark_type == "em":
            text = f"*{text}*"
        elif mark_type == "code":
            text = f"`{text}`"
        elif mark_type == "strike":
            text = f"~~{text}~~"
        elif mark_type == "link":
            href = mark.get("attrs", {}).get("href", "")
            text = f"[{text}]({href})"
        elif mark_type == "subsup":
            pass
    return text


def _adf_paragraph(node: dict[str, Any]) -> str:
    parts = [_adf_node(c) for c in node.get("content", [])]
    return "".join(parts)


def _adf_heading(node: dict[str, Any]) -> str:
    level = node.get("attrs", {}).get("level", 1)
    parts = [_adf_node(c) for c in node.get("content", [])]
    text = "".join(parts)
    return f"{'#' * level} {text}"


def _adf_bullet_list(node: dict[str, Any]) -> str:
    items = [_adf_list_item(c, "- ") for c in node.get("content", [])]
    return "\n".join(items)


def _adf_ordered_list(node: dict[str, Any]) -> str:
    items = []
    for i, c in enumerate(node.get("content", []), 1):
        items.append(_adf_list_item(c, f"{i}. "))
    return "\n".join(items)


def _adf_list_item(node: dict[str, Any], prefix: str) -> str:
    parts = [_adf_node(c) for c in node.get("content", [])]
    text = "".join(parts)
    lines = text.split("\n")
    result = f"{prefix}{lines[0]}"
    for line in lines[1:]:
        result += f"\n{'  ' * len(prefix)}{line}"
    return result


def _adf_code_block(node: dict[str, Any]) -> str:
    lang = node.get("attrs", {}).get("language", "")
    parts = [_adf_node(c) for c in node.get("content", [])]
    text = "".join(parts)
    return f"```{lang}\n{text}\n```"


def _adf_blockquote(node: dict[str, Any]) -> str:
    inner = _adf_children(node)
    lines = inner.split("\n")
    return "\n".join(f"> {line}" for line in lines)


def _adf_table(node: dict[str, Any]) -> str:
    rows = node.get("content", [])
    if not rows:
        return ""

    md_rows: list[str] = []
    for i, row in enumerate(rows):
        cells = row.get("content", [])
        cell_texts = [_adf_children(cell).replace("|", "\\|").replace("\n", " ") for cell in cells]
        md_rows.append("| " + " | ".join(cell_texts) + " |")
        if i == 0:
            md_rows.append("|" + "|".join(" --- " for _ in cell_texts) + "|")

    return "\n".join(md_rows)


def _adf_media_single(node: dict[str, Any]) -> str:
    return "[media]"


def _adf_mention(node: dict[str, Any]) -> str:
    attrs = node.get("attrs", {})
    text = attrs.get("text", "")
    return f"@{text}" if text else "@mention"


def _adf_inline_card(node: dict[str, Any]) -> str:
    attrs = node.get("attrs", {})
    url = attrs.get("url", "")
    return f"[{url}]({url})" if url else "[link]"


def _adf_panel(node: dict[str, Any]) -> str:
    panel_type = node.get("attrs", {}).get("panelType", "info")
    inner = _adf_children(node)
    return f"> **{panel_type.title()}:** {inner}"


def _adf_rule(node: dict[str, Any]) -> str:
    return "---"


def _adf_status(node: dict[str, Any]) -> str:
    attrs = node.get("attrs", {})
    text = attrs.get("text", "")
    return f"[{text}]"


def _adf_hard_break(node: dict[str, Any]) -> str:
    return "\n"


def _adf_emoji(node: dict[str, Any]) -> str:
    attrs = node.get("attrs", {})
    return attrs.get("text", attrs.get("shortName", ""))


def _adf_unknown(node: dict[str, Any]) -> str:
    content = node.get("content")
    if content:
        return _adf_children(node)
    text = node.get("text", "")
    return text


_AdfHandler = Callable[[dict[str, Any]], str]

_ADF_HANDLERS: dict[str, _AdfHandler] = {
    "text": _adf_text,
    "paragraph": _adf_paragraph,
    "heading": _adf_heading,
    "bulletList": _adf_bullet_list,
    "orderedList": _adf_ordered_list,
    "codeBlock": _adf_code_block,
    "blockquote": _adf_blockquote,
    "table": _adf_table,
    "tableRow": lambda n: "",
    "tableHeader": lambda n: "",
    "tableCell": lambda n: "",
    "mediaSingle": _adf_media_single,
    "mediaGroup": _adf_media_single,
    "media": _adf_media_single,
    "mention": _adf_mention,
    "inlineCard": _adf_inline_card,
    "panel": _adf_panel,
    "rule": _adf_rule,
    "status": _adf_status,
    "hardBreak": _adf_hard_break,
    "emoji": _adf_emoji,
}
