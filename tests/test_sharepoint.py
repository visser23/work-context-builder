"""Tests for SharePoint local source adapter."""


import pytest

from workctx.config import SharePointSource
from workctx.models import ChangeAction, SourceObject, SourceType, SyncCheckpoint
from workctx.sources.sharepoint import SharePointLocalSource
from workctx.state import StateDB


@pytest.fixture
def sp_dir(tmp_path):
    """Create a sample SharePoint-like directory."""
    docs = tmp_path / "sharepoint"
    docs.mkdir()
    (docs / "README.md").write_text("# Hello")
    (docs / "Architecture").mkdir()
    (docs / "Architecture" / "design.docx").write_bytes(b"fake docx content")
    (docs / "Architecture" / "plan.pdf").write_bytes(b"fake pdf content")
    (docs / ".DS_Store").write_bytes(b"ds store")
    (docs / "~$temp.docx").write_bytes(b"temp file")
    return docs


@pytest.fixture
def sp_source(sp_dir):
    config = SharePointSource(
        name="test-docs",
        mode="onedrive_local",
        local_path=str(sp_dir),
        site_url="https://tenant.sharepoint.com/sites/Test",
    )
    return SharePointLocalSource(config)


@pytest.fixture
def db(tmp_path):
    db = StateDB(tmp_path / "state.sqlite")
    yield db
    db.close()


def test_validate_ok(sp_source):
    issues = sp_source.validate()
    assert len(issues) == 0


def test_validate_missing_path():
    config = SharePointSource(
        name="bad", mode="onedrive_local", local_path="/nonexistent/path"
    )
    source = SharePointLocalSource(config)
    issues = source.validate()
    assert any("does not exist" in i for i in issues)


def test_discover_initial_sync(sp_source, db):
    changes = sp_source.discover_changes(db, None, full=True)
    paths = {c.source_id for c in changes}
    assert "README.md" in paths
    assert "Architecture/design.docx" in paths
    assert "Architecture/plan.pdf" in paths
    # Excluded files should not appear
    assert ".DS_Store" not in paths
    assert "~$temp.docx" not in paths


def test_discover_excludes(sp_dir):
    config = SharePointSource(
        name="test",
        mode="onedrive_local",
        local_path=str(sp_dir),
        exclude=["**/~$*", "**/.DS_Store", "**/*.pdf"],
    )
    source = SharePointLocalSource(config)
    db_path = sp_dir.parent / "state.sqlite"
    db = StateDB(db_path)
    changes = source.discover_changes(db, None, full=True)
    paths = {c.source_id for c in changes}
    assert "Architecture/plan.pdf" not in paths
    db.close()


def test_discover_no_changes_second_run(sp_source, db):
    changes = sp_source.discover_changes(db, None, full=True)
    for c in changes:
        db.upsert_object(
            SourceObject(
                source_name="test-docs",
                source_type=SourceType.SHAREPOINT,
                source_id=c.source_id,
                file_size=c.file_size,
                file_mtime=c.file_mtime,
                content_sha256="fakehash",
                output_path=f"sharepoint/test-docs/{c.source_id}.md",
            )
        )

    cp = SyncCheckpoint(
        source_name="test-docs",
        source_type=SourceType.SHAREPOINT,
    )
    changes2 = sp_source.discover_changes(db, cp)
    non_delete = [c for c in changes2 if c.action != ChangeAction.DELETE]
    assert len(non_delete) == 0


def test_discover_detects_modification(sp_source, db, sp_dir):
    changes = sp_source.discover_changes(db, None, full=True)
    for c in changes:
        db.upsert_object(
            SourceObject(
                source_name="test-docs",
                source_type=SourceType.SHAREPOINT,
                source_id=c.source_id,
                file_size=c.file_size,
                file_mtime=c.file_mtime,
                content_sha256="oldhash",
                output_path=f"sharepoint/test-docs/{c.source_id}.md",
            )
        )

    readme = sp_dir / "README.md"
    readme.write_text("# Updated content")

    cp = SyncCheckpoint(source_name="test-docs", source_type=SourceType.SHAREPOINT)
    changes2 = sp_source.discover_changes(db, cp)
    changed_ids = {c.source_id for c in changes2 if c.action == ChangeAction.UPDATE}
    assert "README.md" in changed_ids


def test_discover_detects_deletion(sp_source, db, sp_dir):
    changes = sp_source.discover_changes(db, None, full=True)
    for c in changes:
        db.upsert_object(
            SourceObject(
                source_name="test-docs",
                source_type=SourceType.SHAREPOINT,
                source_id=c.source_id,
                file_size=c.file_size,
                file_mtime=c.file_mtime,
                content_sha256="hash",
                output_path=f"sharepoint/test-docs/{c.source_id}.md",
            )
        )

    (sp_dir / "README.md").unlink()

    cp = SyncCheckpoint(source_name="test-docs", source_type=SourceType.SHAREPOINT)
    changes2 = sp_source.discover_changes(db, cp)
    deleted = [c for c in changes2 if c.action == ChangeAction.DELETE]
    assert any(c.source_id == "README.md" for c in deleted)


def test_discover_detects_new_file(sp_source, db, sp_dir):
    changes = sp_source.discover_changes(db, None, full=True)
    for c in changes:
        db.upsert_object(
            SourceObject(
                source_name="test-docs",
                source_type=SourceType.SHAREPOINT,
                source_id=c.source_id,
                file_size=c.file_size,
                file_mtime=c.file_mtime,
                content_sha256="hash",
                output_path=f"sharepoint/test-docs/{c.source_id}.md",
            )
        )

    (sp_dir / "new-file.txt").write_text("new content")

    cp = SyncCheckpoint(source_name="test-docs", source_type=SourceType.SHAREPOINT)
    changes2 = sp_source.discover_changes(db, cp)
    added = [c for c in changes2 if c.action == ChangeAction.ADD]
    assert any(c.source_id == "new-file.txt" for c in added)


def test_get_current_ids(sp_source):
    ids = sp_source.get_current_ids()
    assert "README.md" in ids
    assert "Architecture/design.docx" in ids
    assert ".DS_Store" not in ids
