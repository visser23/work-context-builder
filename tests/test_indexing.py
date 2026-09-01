"""Tests for FTS5 search indexing."""

import pytest

from workctx.indexing import SearchIndex


@pytest.fixture
def index(tmp_path):
    idx = SearchIndex(tmp_path / "test.sqlite")
    yield idx
    idx.close()


def test_upsert_and_search(index):
    index.upsert(
        output_path="jira/test/TEST-1.md",
        title="Implement supplier onboarding",
        body="This issue covers the supplier onboarding flow including validation and approval.",
        source_type="jira",
        source_name="test-jira",
        source_key="TEST-1",
        source_url="https://test.atlassian.net/browse/TEST-1",
        updated_at="2026-09-01",
    )

    results = index.search("supplier onboarding")
    assert len(results) == 1
    assert results[0]["output_path"] == "jira/test/TEST-1.md"
    assert results[0]["title"] == "Implement supplier onboarding"


def test_search_no_results(index):
    results = index.search("nonexistent term xyz")
    assert len(results) == 0


def test_upsert_replaces(index):
    index.upsert(
        output_path="test.md",
        title="Original",
        body="original body",
        source_type="test",
        source_name="test",
        source_key=None,
        source_url=None,
        updated_at=None,
    )
    index.upsert(
        output_path="test.md",
        title="Updated",
        body="updated body",
        source_type="test",
        source_name="test",
        source_key=None,
        source_url=None,
        updated_at=None,
    )

    results = index.search("original")
    assert len(results) == 0

    results = index.search("updated")
    assert len(results) == 1
    assert results[0]["title"] == "Updated"


def test_remove(index):
    index.upsert(
        output_path="to-delete.md",
        title="Delete Me",
        body="will be removed",
        source_type="test",
        source_name="test",
        source_key=None,
        source_url=None,
        updated_at=None,
    )
    results = index.search("removed")
    assert len(results) == 1

    index.remove("to-delete.md")
    results = index.search("removed")
    assert len(results) == 0


def test_multiple_results(index):
    for i in range(5):
        index.upsert(
            output_path=f"doc-{i}.md",
            title=f"Architecture document {i}",
            body=f"Architecture details for component {i}",
            source_type="confluence",
            source_name="wiki",
            source_key=None,
            source_url=None,
            updated_at=None,
        )

    results = index.search("architecture", limit=3)
    assert len(results) == 3

    results = index.search("architecture", limit=10)
    assert len(results) == 5


def test_rebuild_from_corpus(index, tmp_path):
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()

    (corpus_dir / "test.md").write_text(
        "---\ntitle: Test Document\nsource_type: test\nsource_name: s\n"
        "---\n\n# Test\n\nBody text here"
    )
    (corpus_dir / "CONTEXT.md").write_text("Should be skipped")
    meta = corpus_dir / "_meta"
    meta.mkdir()
    (meta / "INDEX.md").write_text("Should also be skipped")

    count = index.rebuild_from_corpus(corpus_dir)
    assert count == 1

    results = index.search("Body text")
    assert len(results) == 1
