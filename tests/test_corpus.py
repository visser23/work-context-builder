"""Tests for corpus output management."""

import pytest

from workctx.corpus import build_output_path, remove_corpus_file, write_corpus_file
from workctx.models import SourceType


@pytest.fixture
def corpus_dir(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    return d


def test_write_corpus_file(corpus_dir):
    path = write_corpus_file(corpus_dir, "test/document.md", "# Hello\n\nWorld\n")
    assert path.exists()
    assert path.read_text() == "# Hello\n\nWorld\n"


def test_write_creates_directories(corpus_dir):
    write_corpus_file(corpus_dir, "deep/nested/path/file.md", "content")
    assert (corpus_dir / "deep" / "nested" / "path" / "file.md").exists()


def test_write_atomic_replace(corpus_dir):
    write_corpus_file(corpus_dir, "doc.md", "version 1")
    write_corpus_file(corpus_dir, "doc.md", "version 2")
    content = (corpus_dir / "doc.md").read_text()
    assert content == "version 2"


def test_remove_corpus_file(corpus_dir):
    write_corpus_file(corpus_dir, "to-delete.md", "content")
    assert (corpus_dir / "to-delete.md").exists()

    remove_corpus_file(corpus_dir, "to-delete.md")
    assert not (corpus_dir / "to-delete.md").exists()


def test_remove_cleans_empty_dirs(corpus_dir):
    write_corpus_file(corpus_dir, "a/b/c/file.md", "content")
    remove_corpus_file(corpus_dir, "a/b/c/file.md")
    assert not (corpus_dir / "a" / "b" / "c").exists()


def test_build_output_path_confluence():
    path = build_output_path(
        SourceType.CONFLUENCE,
        "wiki",
        "12345",
        title="Service Architecture",
        space="ALPHA",
    )
    assert path.startswith("confluence/wiki/ALPHA/")
    assert "12345" in path
    assert "service-architecture" in path
    assert path.endswith(".md")


def test_build_output_path_jira():
    path = build_output_path(
        SourceType.JIRA,
        "jira-source",
        "100",
        source_key="ALPHA-231",
        project="ALPHA",
    )
    assert path == "jira/jira-source/ALPHA/ALPHA-231.md"


def test_build_output_path_sharepoint():
    path = build_output_path(
        SourceType.SHAREPOINT,
        "docs",
        "Architecture/design.docx",
        relative_source_path="Architecture/design.docx",
    )
    assert path == "sharepoint/docs/Architecture/design.docx.md"
