"""SQLite state database with schema migrations."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from workctx.models import SourceObject, SourceType, SyncCheckpoint

CURRENT_SCHEMA_VERSION = 2

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sync_checkpoints (
            source_name TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            last_checkpoint TEXT,
            last_success TEXT,
            last_reconciliation TEXT,
            metadata TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_objects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_key TEXT,
            title TEXT,
            source_url TEXT,
            source_version TEXT,
            source_updated_at TEXT,
            content_sha256 TEXT,
            output_path TEXT,
            file_size INTEGER,
            file_mtime REAL,
            last_processed_at TEXT,
            last_error TEXT,
            retry_count INTEGER DEFAULT 0,
            UNIQUE(source_name, source_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_source_objects_source
        ON source_objects(source_name, source_type)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_source_objects_output
        ON source_objects(output_path)
        """,
    ],
    2: [
        """ALTER TABLE source_objects ADD COLUMN sp_item_id INTEGER""",
        """
        CREATE INDEX IF NOT EXISTS idx_source_objects_sp_item
        ON source_objects(source_name, sp_item_id)
        """,
    ],
}


class StateDB:
    """SQLite state database for tracking sync state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        current = self._get_schema_version()
        for version in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
            if version in MIGRATIONS:
                for sql in MIGRATIONS[version]:
                    self.conn.execute(sql)
                self.conn.execute(
                    "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )
                self.conn.commit()

    def _get_schema_version(self) -> int:
        try:
            row = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            return row[0] if row and row[0] is not None else 0
        except sqlite3.OperationalError:
            return 0

    def close(self) -> None:
        self.conn.close()

    def get_checkpoint(self, source_name: str) -> SyncCheckpoint | None:
        row = self.conn.execute(
            "SELECT * FROM sync_checkpoints WHERE source_name = ?",
            (source_name,),
        ).fetchone()
        if not row:
            return None
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        return SyncCheckpoint(
            source_name=row["source_name"],
            source_type=SourceType(row["source_type"]),
            last_checkpoint=row["last_checkpoint"],
            last_success=_parse_dt(row["last_success"]),
            last_reconciliation=_parse_dt(row["last_reconciliation"]),
            metadata=meta,
        )

    def save_checkpoint(self, cp: SyncCheckpoint) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO sync_checkpoints
            (source_name, source_type, last_checkpoint, last_success, last_reconciliation, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cp.source_name,
                cp.source_type.value,
                cp.last_checkpoint,
                _fmt_dt(cp.last_success),
                _fmt_dt(cp.last_reconciliation),
                json.dumps(cp.metadata) if cp.metadata else None,
            ),
        )
        self.conn.commit()

    def get_object(self, source_name: str, source_id: str) -> SourceObject | None:
        row = self.conn.execute(
            "SELECT * FROM source_objects WHERE source_name = ? AND source_id = ?",
            (source_name, source_id),
        ).fetchone()
        if not row:
            return None
        return _row_to_object(row)

    def get_objects_for_source(self, source_name: str) -> list[SourceObject]:
        rows = self.conn.execute(
            "SELECT * FROM source_objects WHERE source_name = ?",
            (source_name,),
        ).fetchall()
        return [_row_to_object(r) for r in rows]

    def get_all_source_ids(self, source_name: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT source_id FROM source_objects WHERE source_name = ?",
            (source_name,),
        ).fetchall()
        return {r["source_id"] for r in rows}

    def upsert_object(self, obj: SourceObject) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO source_objects
            (source_name, source_type, source_id, source_key, title, source_url,
             source_version, source_updated_at, content_sha256, output_path,
             file_size, file_mtime, last_processed_at, last_error, retry_count,
             sp_item_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name, source_id) DO UPDATE SET
                source_key = excluded.source_key,
                title = excluded.title,
                source_url = excluded.source_url,
                source_version = excluded.source_version,
                source_updated_at = excluded.source_updated_at,
                content_sha256 = excluded.content_sha256,
                output_path = excluded.output_path,
                file_size = excluded.file_size,
                file_mtime = excluded.file_mtime,
                last_processed_at = excluded.last_processed_at,
                last_error = excluded.last_error,
                retry_count = excluded.retry_count,
                sp_item_id = COALESCE(excluded.sp_item_id, source_objects.sp_item_id)
            """,
            (
                obj.source_name,
                obj.source_type.value,
                obj.source_id,
                obj.source_key,
                obj.title,
                obj.source_url,
                obj.source_version,
                _fmt_dt(obj.source_updated_at),
                obj.content_sha256,
                obj.output_path,
                obj.file_size,
                obj.file_mtime,
                _fmt_dt(obj.last_processed_at),
                obj.last_error,
                obj.retry_count,
                obj.sp_item_id,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def get_object_by_sp_item_id(
        self, source_name: str, sp_item_id: int
    ) -> SourceObject | None:
        """Look up a source object by its SharePoint list item ID."""
        row = self.conn.execute(
            "SELECT * FROM source_objects WHERE source_name = ? AND sp_item_id = ?",
            (source_name, sp_item_id),
        ).fetchone()
        if not row:
            return None
        return _row_to_object(row)

    def delete_object(self, source_name: str, source_id: str) -> None:
        self.conn.execute(
            "DELETE FROM source_objects WHERE source_name = ? AND source_id = ?",
            (source_name, source_id),
        )
        self.conn.commit()

    def delete_objects_for_source(self, source_name: str) -> int:
        cursor = self.conn.execute(
            "DELETE FROM source_objects WHERE source_name = ?",
            (source_name,),
        )
        self.conn.commit()
        return cursor.rowcount

    def get_object_by_output_path(self, output_path: str) -> SourceObject | None:
        row = self.conn.execute(
            "SELECT * FROM source_objects WHERE output_path = ?",
            (output_path,),
        ).fetchone()
        if not row:
            return None
        return _row_to_object(row)

    def update_version(self, source_name: str, source_id: str, source_version: str | None) -> None:
        """Update only the source_version for an object (no file rewrite needed)."""
        self.conn.execute(
            "UPDATE source_objects SET source_version = ? WHERE source_name = ? AND source_id = ?",
            (source_version, source_name, source_id),
        )
        self.conn.commit()

    def count_objects(self, source_name: str | None = None) -> int:
        if source_name:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM source_objects WHERE source_name = ?",
                (source_name,),
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM source_objects").fetchone()
        return row[0] if row else 0


def _row_to_object(row: sqlite3.Row) -> SourceObject:
    return SourceObject(
        id=row["id"],
        source_name=row["source_name"],
        source_type=SourceType(row["source_type"]),
        source_id=row["source_id"],
        source_key=row["source_key"],
        title=row["title"],
        source_url=row["source_url"],
        source_version=row["source_version"],
        source_updated_at=_parse_dt(row["source_updated_at"]),
        content_sha256=row["content_sha256"],
        output_path=row["output_path"],
        file_size=row["file_size"],
        file_mtime=row["file_mtime"],
        last_processed_at=_parse_dt(row["last_processed_at"]),
        last_error=row["last_error"],
        retry_count=row["retry_count"],
        sp_item_id=row["sp_item_id"],
    )


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None


def _fmt_dt(val: datetime | None) -> str | None:
    if not val:
        return None
    return val.isoformat()
