"""Tests for SQLite state database."""

import threading
from datetime import UTC, datetime

import pytest

from workctx.models import SourceObject, SourceType, SyncCheckpoint
from workctx.state import StateDB


@pytest.fixture
def db(tmp_path):
    db = StateDB(tmp_path / "test.sqlite")
    yield db
    db.close()


def test_schema_creation(db):
    tables = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {t["name"] for t in tables}
    assert "schema_version" in table_names
    assert "sync_checkpoints" in table_names
    assert "source_objects" in table_names


def test_checkpoint_crud(db):
    cp = SyncCheckpoint(
        source_name="test-source",
        source_type=SourceType.JIRA,
        last_checkpoint="2026-09-01T00:00:00",
        last_success=datetime(2026, 9, 1, tzinfo=UTC),
    )
    db.save_checkpoint(cp)

    loaded = db.get_checkpoint("test-source")
    assert loaded is not None
    assert loaded.source_name == "test-source"
    assert loaded.source_type == SourceType.JIRA
    assert loaded.last_checkpoint == "2026-09-01T00:00:00"


def test_checkpoint_not_found(db):
    assert db.get_checkpoint("nonexistent") is None


def test_object_upsert_and_retrieve(db):
    obj = SourceObject(
        source_name="test-source",
        source_type=SourceType.SHAREPOINT,
        source_id="doc/test.docx",
        title="Test Document",
        content_sha256="abc123",
        output_path="sharepoint/test-source/doc/test.docx.md",
    )
    row_id = db.upsert_object(obj)
    assert row_id > 0

    loaded = db.get_object("test-source", "doc/test.docx")
    assert loaded is not None
    assert loaded.title == "Test Document"
    assert loaded.content_sha256 == "abc123"


def test_object_update(db):
    obj = SourceObject(
        source_name="test",
        source_type=SourceType.JIRA,
        source_id="123",
        title="Original",
    )
    db.upsert_object(obj)

    obj.title = "Updated"
    obj.content_sha256 = "newhash"
    db.upsert_object(obj)

    loaded = db.get_object("test", "123")
    assert loaded is not None
    assert loaded.title == "Updated"
    assert loaded.content_sha256 == "newhash"


def test_delete_object(db):
    obj = SourceObject(
        source_name="test",
        source_type=SourceType.CONFLUENCE,
        source_id="page-1",
    )
    db.upsert_object(obj)
    assert db.get_object("test", "page-1") is not None

    db.delete_object("test", "page-1")
    assert db.get_object("test", "page-1") is None


def test_get_all_source_ids(db):
    for i in range(5):
        db.upsert_object(
            SourceObject(
                source_name="test",
                source_type=SourceType.JIRA,
                source_id=f"issue-{i}",
            )
        )
    ids = db.get_all_source_ids("test")
    assert len(ids) == 5
    assert "issue-0" in ids
    assert "issue-4" in ids


def test_count_objects(db):
    for i in range(3):
        db.upsert_object(
            SourceObject(
                source_name="src-a",
                source_type=SourceType.JIRA,
                source_id=f"a-{i}",
            )
        )
    for i in range(2):
        db.upsert_object(
            SourceObject(
                source_name="src-b",
                source_type=SourceType.CONFLUENCE,
                source_id=f"b-{i}",
            )
        )

    assert db.count_objects("src-a") == 3
    assert db.count_objects("src-b") == 2
    assert db.count_objects() == 5


def test_migration_idempotent(tmp_path):
    db_path = tmp_path / "migrate.sqlite"
    db1 = StateDB(db_path)
    db1.close()

    db2 = StateDB(db_path)
    version = db2.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == 2
    db2.close()


def test_sp_item_id_column_exists(db):
    """Schema v2 adds the sp_item_id column."""
    cols = {
        row[1]
        for row in db.conn.execute("PRAGMA table_info(source_objects)").fetchall()
    }
    assert "sp_item_id" in cols


def test_sp_item_id_stored_and_queried(db):
    obj = SourceObject(
        source_name="sp-test",
        source_type=SourceType.SHAREPOINT,
        source_id="/sites/team/Shared Documents/doc.docx",
        title="Test Doc",
        sp_item_id=42,
    )
    db.upsert_object(obj)

    found = db.get_object_by_sp_item_id("sp-test", 42)
    assert found is not None
    assert found.source_id == "/sites/team/Shared Documents/doc.docx"
    assert found.sp_item_id == 42


def test_sp_item_id_not_found(db):
    assert db.get_object_by_sp_item_id("nonexistent", 999) is None


def test_sp_item_id_preserved_on_update(db):
    """COALESCE keeps sp_item_id when a subsequent upsert passes None."""
    obj = SourceObject(
        source_name="sp-test",
        source_type=SourceType.SHAREPOINT,
        source_id="doc.docx",
        sp_item_id=77,
    )
    db.upsert_object(obj)

    obj_update = SourceObject(
        source_name="sp-test",
        source_type=SourceType.SHAREPOINT,
        source_id="doc.docx",
        title="Updated",
        sp_item_id=None,
    )
    db.upsert_object(obj_update)

    loaded = db.get_object("sp-test", "doc.docx")
    assert loaded is not None
    assert loaded.sp_item_id == 77
    assert loaded.title == "Updated"


def test_concurrent_get_checkpoint(db):
    """Concurrent get_checkpoint calls must not raise InterfaceError."""
    for name in ("src-a", "src-b", "src-c"):
        db.save_checkpoint(
            SyncCheckpoint(
                source_name=name,
                source_type=SourceType.JIRA,
                last_checkpoint="2026-09-01T00:00:00",
                last_success=datetime(2026, 9, 1, tzinfo=UTC),
            )
        )

    errors: list[Exception] = []
    barrier = threading.Barrier(3)

    def _reader(name: str) -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(50):
                cp = db.get_checkpoint(name)
                assert cp is not None
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_reader, args=(n,)) for n in ("src-a", "src-b", "src-c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"Concurrent reads raised: {errors}"


def test_concurrent_upserts(db):
    """Concurrent upsert_object calls must not corrupt data."""
    errors: list[Exception] = []
    barrier = threading.Barrier(3)

    def _writer(source_name: str, count: int) -> None:
        try:
            barrier.wait(timeout=5)
            for i in range(count):
                db.upsert_object(
                    SourceObject(
                        source_name=source_name,
                        source_type=SourceType.JIRA,
                        source_id=f"issue-{i}",
                        title=f"{source_name}-{i}",
                    )
                )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_writer, args=("src-a", 20)),
        threading.Thread(target=_writer, args=("src-b", 20)),
        threading.Thread(target=_writer, args=("src-c", 20)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"Concurrent writes raised: {errors}"
    assert db.count_objects("src-a") == 20
    assert db.count_objects("src-b") == 20
    assert db.count_objects("src-c") == 20
