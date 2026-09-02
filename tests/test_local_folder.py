"""Tests for local folder source adapter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from workctx.config import LocalFolderSource
from workctx.models import ChangeAction, SourceType
from workctx.sources.local_folder import LocalFolderAdapter


@pytest.fixture
def tmp_folder(tmp_path: Path) -> Path:
    """Create a sample folder tree."""
    d = tmp_path / "docs"
    d.mkdir()
    (d / "readme.md").write_text("# Hello")
    (d / "notes.txt").write_text("Some notes")
    (d / "data.csv").write_text("a,b\n1,2")
    sub = d / "subdir"
    sub.mkdir()
    (sub / "code.py").write_text("print('hi')")
    (sub / "image.png").write_bytes(b"\x89PNG\r\n")
    (d / ".hidden").write_text("hidden")
    (d / "~$temp.docx").write_text("temp")
    return d


@pytest.fixture
def config(tmp_folder: Path) -> LocalFolderSource:
    return LocalFolderSource(
        name="test-local",
        paths=[str(tmp_folder)],
    )


def test_source_type(config: LocalFolderSource) -> None:
    adapter = LocalFolderAdapter(config)
    assert adapter.source_type == SourceType.LOCAL_FOLDER
    assert adapter.name == "test-local"


def test_discover_changes_initial(config: LocalFolderSource, tmp_folder: Path) -> None:
    adapter = LocalFolderAdapter(config)
    db = MagicMock()
    db.get_objects_for_source.return_value = []

    changes = adapter.discover_changes(db, None, full=True)

    names = {c.source_id for c in changes}
    assert any("readme.md" in n for n in names)
    assert any("notes.txt" in n for n in names)
    assert any("code.py" in n for n in names)
    assert all(c.action == ChangeAction.ADD for c in changes)
    assert all(".hidden" not in c.source_id for c in changes)
    assert all("~$" not in c.source_id for c in changes)


def test_skips_unconvertible(config: LocalFolderSource, tmp_folder: Path) -> None:
    adapter = LocalFolderAdapter(config)
    db = MagicMock()
    db.get_objects_for_source.return_value = []

    changes = adapter.discover_changes(db, None, full=True)
    names = {c.source_id for c in changes}
    assert all("image.png" not in n for n in names)


def test_skips_own_state_dir(tmp_path: Path) -> None:
    docs = tmp_path / "workspace"
    docs.mkdir()
    (docs / "file.md").write_text("hello")
    state = docs / "state"
    state.mkdir()
    (state / "state.sqlite").write_text("db")
    (state / "other.txt").write_text("data")

    config = LocalFolderSource(name="test", paths=[str(docs)])
    adapter = LocalFolderAdapter(config, state_dir=state)
    db = MagicMock()
    db.get_objects_for_source.return_value = []

    changes = adapter.discover_changes(db, None, full=True)
    ids = {c.source_id for c in changes}
    assert any("file.md" in s for s in ids)
    assert all("state.sqlite" not in s for s in ids)
    assert all("other.txt" not in s for s in ids)


def test_skips_output_root(tmp_path: Path) -> None:
    docs = tmp_path / "workspace"
    docs.mkdir()
    (docs / "file.md").write_text("hello")
    output = docs / "output"
    output.mkdir()
    (output / "corpus.md").write_text("generated")

    config = LocalFolderSource(name="test", paths=[str(docs)])
    adapter = LocalFolderAdapter(config, output_root=output)
    db = MagicMock()
    db.get_objects_for_source.return_value = []

    changes = adapter.discover_changes(db, None, full=True)
    ids = {c.source_id for c in changes}
    assert any("file.md" in s for s in ids)
    assert all("corpus.md" not in s for s in ids)


def test_skips_git_dirs(tmp_path: Path) -> None:
    docs = tmp_path / "repo"
    docs.mkdir()
    (docs / "code.py").write_text("x = 1")
    git = docs / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main")

    config = LocalFolderSource(name="test", paths=[str(docs)])
    adapter = LocalFolderAdapter(config)
    db = MagicMock()
    db.get_objects_for_source.return_value = []

    changes = adapter.discover_changes(db, None, full=True)
    assert all(".git" not in c.source_id for c in changes)


def test_get_current_ids(config: LocalFolderSource, tmp_folder: Path) -> None:
    adapter = LocalFolderAdapter(config)
    ids = adapter.get_current_ids()
    assert any("readme.md" in s for s in ids)
    assert any("code.py" in s for s in ids)


def test_validate_missing_path() -> None:
    config = LocalFolderSource(name="bad", paths=["/nonexistent/path/abc123"])
    adapter = LocalFolderAdapter(config)
    issues = adapter.validate()
    assert len(issues) > 0
    assert "does not exist" in issues[0]


def test_multiple_paths(tmp_path: Path) -> None:
    d1 = tmp_path / "folder1"
    d2 = tmp_path / "folder2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "a.md").write_text("A")
    (d2 / "b.md").write_text("B")

    config = LocalFolderSource(name="multi", paths=[str(d1), str(d2)])
    adapter = LocalFolderAdapter(config)
    db = MagicMock()
    db.get_objects_for_source.return_value = []

    changes = adapter.discover_changes(db, None, full=True)
    ids = {c.source_id for c in changes}
    assert any("a.md" in s for s in ids)
    assert any("b.md" in s for s in ids)


def test_exclude_patterns(tmp_path: Path) -> None:
    d = tmp_path / "data"
    d.mkdir()
    (d / "keep.md").write_text("keep")
    (d / "skip.log").write_text("skip")

    config = LocalFolderSource(
        name="filtered",
        paths=[str(d)],
        exclude=["**/*.log"],
    )
    adapter = LocalFolderAdapter(config)
    db = MagicMock()
    db.get_objects_for_source.return_value = []

    changes = adapter.discover_changes(db, None, full=True)
    ids = {c.source_id for c in changes}
    assert any("keep.md" in s for s in ids)
    assert all("skip.log" not in s for s in ids)
