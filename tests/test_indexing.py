"""Tests for FTS5 search indexing."""

import re

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


# ------------------------------------------------- path map (no full-index scans)
#
# FTS5 cannot index a column, so ``DELETE ... WHERE output_path = ?`` scanned the whole
# index: ~5 s per document at 30k documents, i.e. a 27k-document repair would take days.


def _doc(index, path, body="alpha beta"):
    index.upsert(
        output_path=path,
        title=path,
        body=body,
        source_type="jira",
        source_name="s",
        source_key=None,
        source_url=None,
        updated_at=None,
    )


def _consistent(index) -> bool:
    fts = {r[0]: r[1] for r in index.conn.execute("SELECT output_path, rowid FROM fts_index")}
    mapped = {
        r[0]: r[1] for r in index.conn.execute("SELECT output_path, fts_rowid FROM fts_paths")
    }
    return fts == mapped


def test_no_statement_filters_the_fts_table_by_path(index):
    """Behavioural guard: any WHERE on the FTS ``output_path`` column is a full scan."""
    seen: list[str] = []
    index.conn.set_trace_callback(seen.append)
    _doc(index, "a.md")
    _doc(index, "a.md", body="changed")
    index.rename("a.md", "b.md")
    index.remove("b.md")
    index.remove("never-existed.md")
    offenders = [
        s
        for s in seen
        if "fts_index" in s and "fts_paths" not in s and re.search(r"WHERE\s+output_path", s)
    ]
    assert offenders == []


def test_map_follows_upsert_rename_remove(index):
    _doc(index, "a.md")
    _doc(index, "b.md")
    _doc(index, "a.md", body="new text gamma")  # replace, not duplicate
    assert _consistent(index)
    assert index.conn.execute("SELECT COUNT(*) FROM fts_index").fetchone()[0] == 2
    assert [r["output_path"] for r in index.search("gamma")] == ["a.md"]

    index.rename("a.md", "c.md")
    assert _consistent(index)
    assert [r["output_path"] for r in index.search("gamma")] == ["c.md"]

    index.remove("c.md")
    index.remove("c.md")  # idempotent
    assert _consistent(index)
    assert index.search("gamma") == []


def test_rename_over_an_existing_path_keeps_one_row(index):
    _doc(index, "a.md", body="from a")
    _doc(index, "b.md", body="from b")
    index.rename("a.md", "b.md")
    assert _consistent(index)
    assert index.conn.execute("SELECT COUNT(*) FROM fts_index").fetchone()[0] == 1
    assert [r["output_path"] for r in index.search("from")] == ["b.md"]


def test_existing_database_without_a_map_is_upgraded_and_deduplicated(tmp_path):
    import sqlite3

    from workctx.indexing import FTS_CREATE, SearchIndex

    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute(FTS_CREATE)
    for path, body in (("a.md", "old a"), ("b.md", "bee"), ("a.md", "newer duplicate a")):
        con.execute(
            "INSERT INTO fts_index (title, body, source_type, source_name, source_key,"
            " source_url, output_path, updated_at) VALUES ('t', ?, '', '', '', '', ?, '')",
            (body, path),
        )
    con.commit()
    con.close()

    idx = SearchIndex(db)
    try:
        assert _consistent(idx)
        assert idx.conn.execute("SELECT COUNT(*) FROM fts_index").fetchone()[0] == 2
        assert [r["output_path"] for r in idx.search("duplicate")] == ["a.md"]  # newest kept
        idx.remove("a.md")
        assert [r["output_path"] for r in idx.search("bee")] == ["b.md"]
    finally:
        idx.close()


def test_map_is_rebuilt_if_it_ever_disagrees_with_the_index(tmp_path):
    from workctx.indexing import SearchIndex

    idx = SearchIndex(tmp_path / "s.sqlite")
    _doc(idx, "a.md")
    _doc(idx, "b.md")
    idx.conn.execute("DELETE FROM fts_paths WHERE output_path = 'b.md'")  # simulated crash
    idx.conn.commit()
    idx.close()

    again = SearchIndex(tmp_path / "s.sqlite")
    try:
        assert _consistent(again)
    finally:
        again.close()


def test_rebuild_from_corpus_resets_the_map(index, tmp_path):
    _doc(index, "stale.md")
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "doc.md").write_text("---\ntitle: T\n---\nbody text\n")
    index.rebuild_from_corpus(tmp_path)
    assert _consistent(index)
    assert [r[0] for r in index.conn.execute("SELECT output_path FROM fts_paths")] == ["x/doc.md"]
