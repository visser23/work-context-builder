"""SQLite FTS5 full-text search indexing."""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FTS_CREATE = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_index USING fts5(
    title,
    body,
    source_type,
    source_name,
    source_key,
    source_url,
    output_path,
    updated_at
)
"""


class SearchIndex:
    """SQLite FTS5 search index over the normalised corpus."""

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._ensure_table()

    def _ensure_table(self) -> None:
        try:
            self.conn.execute("SELECT COUNT(*) FROM fts_index")
        except sqlite3.OperationalError:
            self.conn.execute(FTS_CREATE)
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def upsert(
        self,
        output_path: str,
        title: str,
        body: str,
        source_type: str,
        source_name: str,
        source_key: str | None,
        source_url: str | None,
        updated_at: str | None,
    ) -> None:
        """Insert or replace a document in the FTS index."""
        self.conn.execute(
            "DELETE FROM fts_index WHERE output_path = ?",
            (output_path,),
        )
        self.conn.execute(
            """
            INSERT INTO fts_index
            (title, body, source_type, source_name, source_key, source_url, output_path, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title or "",
                body[:50000],
                source_type,
                source_name,
                source_key or "",
                source_url or "",
                output_path,
                updated_at or "",
            ),
        )
        self.conn.commit()

    def remove(self, output_path: str) -> None:
        self.conn.execute("DELETE FROM fts_index WHERE output_path = ?", (output_path,))
        self.conn.commit()

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search the FTS index."""
        fts_query = _sanitise_fts_query(query)
        try:
            rows = self.conn.execute(
                """
                SELECT output_path, title, source_type, source_name,
                       source_key, source_url, updated_at,
                       snippet(fts_index, 1, '>>>', '<<<', '...', 48) AS snippet
                FROM fts_index
                WHERE fts_index MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            logger.warning("FTS query failed: %s", fts_query, exc_info=True)
            return []

        return [dict(r) for r in rows]

    def rebuild_from_corpus(self, output_root: Path) -> int:
        """Rebuild the entire FTS index from corpus Markdown files."""
        self.conn.execute("DELETE FROM fts_index")

        count = 0
        for md_file in output_root.rglob("*.md"):
            rel_path = str(md_file.relative_to(output_root))
            if rel_path.startswith("_meta/") or rel_path in (
                "CONTEXT.md",
                "AGENTS.md",
                "CLAUDE.md",
                "README.md",
            ):
                continue

            text = md_file.read_text(encoding="utf-8", errors="replace")
            front_matter, body = _split_front_matter(text)

            self.conn.execute(
                """
                INSERT INTO fts_index
                (title, body, source_type, source_name,
                 source_key, source_url, output_path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    front_matter.get("title", md_file.stem),
                    body[:50000],
                    front_matter.get("source_type", ""),
                    front_matter.get("source_name", ""),
                    front_matter.get("source_key", front_matter.get("issue_key", "")),
                    front_matter.get("source_url", ""),
                    rel_path,
                    front_matter.get("updated_at", ""),
                ),
            )
            count += 1

        self.conn.commit()
        logger.info("FTS index rebuilt: %d documents", count)
        return count


def _sanitise_fts_query(query: str) -> str:
    """Sanitise a user query for FTS5 MATCH."""
    cleaned = re.sub(r"[^\w\s\"-]", " ", query)
    cleaned = cleaned.strip()
    if not cleaned:
        return '""'
    if '"' not in cleaned and not any(
        op in cleaned.upper() for op in ("AND", "OR", "NOT", "NEAR")
    ):
        words = cleaned.split()
        if len(words) > 1:
            return " ".join(f'"{w}"' for w in words)
    return cleaned


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split YAML front matter from Markdown body."""
    if not text.startswith("---"):
        return {}, text

    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    fm_text = text[3:end].strip()
    body = text[end + 4:].strip()

    fm: dict[str, str] = {}
    for line in fm_text.split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"')

    return fm, body
