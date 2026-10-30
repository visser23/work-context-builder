"""SQLite FTS5 full-text search indexing."""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_INDEX_BODY_CHARS = 400_000
"""Per-document cap on indexed body text.

Sized above ``sync.large_document_chars`` (300k default, the split threshold) so
every part of a long document - e.g. a multi-hour meeting transcript - is fully
searchable rather than only its first few pages.
"""

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


PATH_MAP_CREATE = """
CREATE TABLE IF NOT EXISTS fts_paths (
    output_path TEXT PRIMARY KEY,
    fts_rowid INTEGER NOT NULL
)
"""


class SearchIndex:
    """SQLite FTS5 search index over the normalised corpus.

    FTS5 cannot index a column, so ``WHERE output_path = ?`` on ``fts_index`` scans the
    whole index (~5 s at 30k documents). ``fts_paths`` maps each output path to its FTS
    rowid so replacing, removing and renaming a document address it by rowid instead.
    """

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._ensure_table()
        self._ensure_path_map()

    def _ensure_table(self) -> None:
        try:
            self.conn.execute("SELECT COUNT(*) FROM fts_index")
        except sqlite3.OperationalError:
            self.conn.execute(FTS_CREATE)
            self.conn.commit()

    def _ensure_path_map(self) -> None:
        """Create the path map, and (re)build it when it disagrees with the index.

        Runs once for databases created before the map existed, and again only if the two
        ever diverge (e.g. a crash mid-update). Historical duplicate paths are collapsed,
        keeping the newest row, which is what ``upsert`` always intended.
        """
        self.conn.execute(PATH_MAP_CREATE)
        try:
            indexed = self.conn.execute("SELECT COUNT(*) FROM fts_index_docsize").fetchone()[0]
        except sqlite3.OperationalError:
            indexed = self.conn.execute("SELECT COUNT(*) FROM fts_index").fetchone()[0]
        mapped = self.conn.execute("SELECT COUNT(*) FROM fts_paths").fetchone()[0]
        if indexed == mapped:
            self.conn.commit()
            return

        logger.info("Rebuilding FTS path map (%d indexed, %d mapped)", indexed, mapped)
        latest: dict[str, int] = {}
        superseded: list[int] = []
        for rowid, path in self.conn.execute(
            "SELECT rowid, output_path FROM fts_index ORDER BY rowid"
        ):
            if path in latest:
                superseded.append(latest[path])
            latest[path] = rowid
        for rowid in superseded:
            self.conn.execute("DELETE FROM fts_index WHERE rowid = ?", (rowid,))
        self.conn.execute("DELETE FROM fts_paths")
        self.conn.executemany("INSERT INTO fts_paths VALUES (?, ?)", latest.items())
        self.conn.commit()

    def _rowid_of(self, output_path: str) -> int | None:
        row = self.conn.execute(
            "SELECT fts_rowid FROM fts_paths WHERE output_path = ?", (output_path,)
        ).fetchone()
        return int(row[0]) if row else None

    def _drop(self, output_path: str) -> None:
        """Remove ``output_path`` from the index and the map (no commit)."""
        rowid = self._rowid_of(output_path)
        if rowid is not None:
            self.conn.execute("DELETE FROM fts_index WHERE rowid = ?", (rowid,))
            self.conn.execute("DELETE FROM fts_paths WHERE output_path = ?", (output_path,))

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
        self._drop(output_path)
        cur = self.conn.execute(
            """
            INSERT INTO fts_index
            (title, body, source_type, source_name, source_key, source_url, output_path, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title or "",
                body[:MAX_INDEX_BODY_CHARS],
                source_type,
                source_name,
                source_key or "",
                source_url or "",
                output_path,
                updated_at or "",
            ),
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO fts_paths VALUES (?, ?)", (output_path, cur.lastrowid)
        )
        self.conn.commit()

    def remove(self, output_path: str) -> None:
        self._drop(output_path)
        self.conn.commit()

    def rename(self, old_path: str, new_path: str) -> None:
        """Re-point an indexed document at a moved corpus file (body is untouched)."""
        rowid = self._rowid_of(old_path)
        if rowid is None or old_path == new_path:
            return
        self._drop(new_path)  # the moved document replaces whatever sat at the destination
        self.conn.execute("UPDATE fts_index SET output_path = ? WHERE rowid = ?", (new_path, rowid))
        self.conn.execute(
            "UPDATE fts_paths SET output_path = ? WHERE output_path = ?", (new_path, old_path)
        )
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
        self.conn.execute("DELETE FROM fts_paths")

        count = 0
        for md_file in output_root.rglob("*.md"):
            rel_path = str(md_file.relative_to(output_root))
            if rel_path.startswith("_meta/") or rel_path in (
                "CONTEXT.md",
                "AGENTS.md",
                "CLAUDE.md",
                "README.md",
                "CHATGPT_INSTRUCTIONS.md",
                "PROJECT_BRIEF.md",
            ):
                continue

            text = md_file.read_text(encoding="utf-8", errors="replace")
            front_matter, body = _split_front_matter(text)

            cur = self.conn.execute(
                """
                INSERT INTO fts_index
                (title, body, source_type, source_name,
                 source_key, source_url, output_path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    front_matter.get("title", md_file.stem),
                    body[:MAX_INDEX_BODY_CHARS],
                    front_matter.get("source_type", ""),
                    front_matter.get("source_name", ""),
                    front_matter.get("source_key", front_matter.get("issue_key", "")),
                    front_matter.get("source_url", ""),
                    rel_path,
                    front_matter.get("updated_at", ""),
                ),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO fts_paths VALUES (?, ?)", (rel_path, cur.lastrowid)
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
    if '"' not in cleaned and not any(op in cleaned.upper() for op in ("AND", "OR", "NOT", "NEAR")):
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
    body = text[end + 4 :].strip()

    fm: dict[str, str] = {}
    for line in fm_text.split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip().strip('"')

    return fm, body
