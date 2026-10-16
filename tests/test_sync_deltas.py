"""Updates (deltas) must overwrite the original document and leave no stray files behind.

Covers single files and large documents that are split into ``.part-NNN`` files, including
growing, shrinking, renaming and deleting them, and names containing glob characters.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workctx.corpus import document_files
from workctx.models import RunStatus
from workctx.sync import run_sync


@pytest.fixture
def project(sp_project):
    sp_project.sync.large_document_chars = 400  # force small documents to split
    return sp_project


def lines(n: int, tag: str = "x") -> bytes:
    return "\n".join(f"{tag} line {i:04d} " + "." * 40 for i in range(n)).encode()


def corpus(project) -> set[str]:
    root: Path = project.output_root_path / "sharepoint"
    return {str(p.relative_to(project.output_root_path)) for p in root.rglob("*") if p.is_file()}


def db_paths(project) -> set[str]:
    con = sqlite3.connect(project.state_dir / "state.sqlite")
    try:
        return {r[0] for r in con.execute("select output_path from source_objects")}
    finally:
        con.close()


def fts_paths(project) -> set[str]:
    con = sqlite3.connect(project.state_dir / "state.sqlite")
    try:
        return {r[0] for r in con.execute("select output_path from fts_index")}
    finally:
        con.close()


def sync(project, run_id: str):
    result = run_sync(project, run_id=run_id, quiet=True)
    assert result.aggregate_status() == RunStatus.HEALTHY
    return result


def parts(project, name: str) -> set[str]:
    return {p for p in corpus(project) if p.startswith(f"sharepoint/sp/{name}")}


class TestSingleFileDeltas:
    def test_edit_overwrites_the_same_file_and_adds_nothing(self, sp_library, project):
        sp_library.add("doc.txt", b"version one", modified="2026-10-01T10:00:00Z")
        sync(project, "t1")
        before = corpus(project)

        sp_library.add("doc.txt", b"version two!!", modified="2026-10-02T10:00:00Z")
        result = sync(project, "t2")

        assert corpus(project) == before == {"sharepoint/sp/doc.txt.md"}
        text = (project.output_root_path / "sharepoint/sp/doc.txt.md").read_text()
        assert "version two" in text and "version one" not in text
        assert result.source_results[0].objects_updated == 1
        assert result.source_results[0].objects_added == 0

    def test_move_between_folders_removes_the_old_copy(self, sp_library, project):
        sp_library.add("a/doc.txt", b"same", modified="2026-10-01T10:00:00Z")
        sync(project, "t1")

        sp_library.remove("a/doc.txt")
        sp_library.folders.discard(f"{sp_library.base}/a")
        sp_library.add("b/doc.txt", b"same", modified="2026-10-01T10:00:00Z")
        sync(project, "t2")

        assert corpus(project) == {"sharepoint/sp/b/doc.txt.md"}
        assert db_paths(project) == {"sharepoint/sp/b/doc.txt.md"}
        assert fts_paths(project) == {"sharepoint/sp/b/doc.txt.md"}


class TestMultipartDeltas:
    def test_growing_a_document_replaces_the_single_file_with_parts(self, sp_library, project):
        sp_library.add("big.txt", b"tiny", modified="2026-10-01T10:00:00Z")
        sync(project, "t1")
        assert corpus(project) == {"sharepoint/sp/big.txt.md"}

        sp_library.add("big.txt", lines(40), modified="2026-10-02T10:00:00Z")
        sync(project, "t2")

        found = parts(project, "big.txt")
        assert len(found) > 1
        assert "sharepoint/sp/big.txt.md" not in found  # no stale single-file copy
        assert fts_paths(project) == found

    def test_shrinking_a_document_removes_every_old_part(self, sp_library, project):
        sp_library.add("big.txt", lines(60), modified="2026-10-01T10:00:00Z")
        sync(project, "t1")
        assert len(parts(project, "big.txt")) > 2

        sp_library.add("big.txt", b"small now", modified="2026-10-02T10:00:00Z")
        sync(project, "t2")

        assert corpus(project) == {"sharepoint/sp/big.txt.md"}
        assert fts_paths(project) == {"sharepoint/sp/big.txt.md"}

    def test_fewer_parts_after_an_edit_leave_no_higher_numbered_leftovers(
        self, sp_library, project
    ):
        sp_library.add("big.txt", lines(60), modified="2026-10-01T10:00:00Z")
        sync(project, "t1")
        first = parts(project, "big.txt")

        sp_library.add("big.txt", lines(30), modified="2026-10-02T10:00:00Z")
        sync(project, "t2")

        now = parts(project, "big.txt")
        assert 1 < len(now) < len(first)
        assert fts_paths(project) == now
        assert all((project.output_root_path / p).exists() for p in db_paths(project))

    def test_rename_moves_all_parts(self, sp_library, project):
        sp_library.add("old/big.txt", lines(40), modified="2026-10-01T10:00:00Z")
        sync(project, "t1")

        sp_library.remove("old/big.txt")
        sp_library.folders.discard(f"{sp_library.base}/old")
        sp_library.add("new/big.txt", lines(40), modified="2026-10-01T10:00:00Z")
        sync(project, "t2")

        assert corpus(project)
        assert all(p.startswith("sharepoint/sp/new/") for p in corpus(project))
        assert not (project.output_root_path / "sharepoint/sp/old").exists()

    def test_deleting_a_multipart_document_removes_all_parts(self, sp_library, project):
        sp_library.add("keep.txt", b"keep")
        sp_library.add("big.txt", lines(40))
        sync(project, "t1")
        assert len(parts(project, "big.txt")) > 1

        sp_library.remove("big.txt")
        sync(project, "t2")

        assert corpus(project) == {"sharepoint/sp/keep.txt.md"}
        assert fts_paths(project) == {"sharepoint/sp/keep.txt.md"}

    def test_glob_characters_in_names_do_not_confuse_cleanup(self, sp_library, project):
        sp_library.add("Report [v2] (final).txt", lines(60), modified="2026-10-01T10:00:00Z")
        sp_library.add("Report [v3] (final).txt", lines(60), modified="2026-10-01T10:00:00Z")
        sync(project, "t1")

        sp_library.add("Report [v2] (final).txt", b"short", modified="2026-10-02T10:00:00Z")
        sync(project, "t2")

        assert parts(project, "Report [v2]") == {"sharepoint/sp/Report [v2] (final).txt.md"}
        assert len(parts(project, "Report [v3]")) > 2  # neighbour untouched

    def test_corpus_matches_database_after_a_mixed_sequence(self, sp_library, project):
        sp_library.add("a.txt", lines(60), modified="2026-10-01T10:00:00Z")
        sp_library.add("b.txt", b"b", modified="2026-10-01T10:00:00Z")
        sync(project, "t1")
        sp_library.add("a.txt", lines(20), modified="2026-10-02T10:00:00Z")
        sp_library.add("b.txt", lines(50), modified="2026-10-02T10:00:00Z")
        sp_library.add("c/d.txt", b"d")
        sync(project, "t2")
        sp_library.remove("a.txt")
        sync(project, "t3")

        on_disk = corpus(project)
        # every stored object has its first file; every file on disk belongs to a stored object
        assert all(
            any(f == p or f.startswith(p.rsplit(".part-", 1)[0]) for f in on_disk)
            for p in db_paths(project)
        )
        owned = set()
        for p in db_paths(project):
            owned.update(document_files(project.output_root_path, p))
        assert on_disk == owned
        assert fts_paths(project) == on_disk


class TestStaleTwinCleanup:
    def test_sync_removes_a_single_file_copy_left_next_to_the_parts(self, sp_library, project):
        sp_library.add("big.txt", lines(40))
        sync(project, "t1")
        twin = project.output_root_path / "sharepoint/sp/big.txt.md"
        twin.write_text("stale copy from an older version")  # what v1.3.1 left behind

        sync(project, "t2")

        assert not twin.exists()
        assert len(parts(project, "big.txt")) > 1


class TestConverterUpgradeRetry:
    def test_given_up_stubs_are_retried_once_after_a_converter_upgrade(self, sp_library, project):
        sp_library.add("odd.docx", b"not really a docx")  # cannot convert -> stub
        for n in range(4):
            sync(project, f"t{n}")  # gives up after the retry cap
        con = sqlite3.connect(project.state_dir / "state.sqlite")
        stuck = con.execute("select retry_count from source_objects").fetchone()[0]
        assert stuck >= 3
        (project.state_dir / "converter.rev").unlink(missing_ok=True)  # as before the upgrade

        sync(project, "after-upgrade")

        again = con.execute("select retry_count from source_objects").fetchone()[0]
        assert again == 1  # reset to 0, then one fresh attempt
        assert (project.state_dir / "converter.rev").read_text().strip().isdigit()
        con.close()


class TestDocumentFiles:
    def test_lists_single_file(self, tmp_path):
        (tmp_path / "d").mkdir()
        (tmp_path / "d/x.pdf.md").write_text("x")
        (tmp_path / "d/y.pdf.md").write_text("y")

        assert document_files(tmp_path, "d/x.pdf.md") == ["d/x.pdf.md"]

    def test_from_a_part_path_returns_every_part_and_any_base(self, tmp_path):
        (tmp_path / "d").mkdir()
        for name in ("x.pdf.md", "x.pdf.part-001.md", "x.pdf.part-002.md", "x.pdf.partial.md"):
            (tmp_path / "d" / name).write_text("z")

        assert document_files(tmp_path, "d/x.pdf.part-001.md") == [
            "d/x.pdf.md",
            "d/x.pdf.part-001.md",
            "d/x.pdf.part-002.md",
        ]

    def test_square_brackets_are_literal(self, tmp_path):
        (tmp_path / "[a]").mkdir()
        (tmp_path / "[a]/x [1].md").write_text("z")
        (tmp_path / "[a]/x 1.md").write_text("z")

        assert document_files(tmp_path, "[a]/x [1].md") == ["[a]/x [1].md"]

    def test_missing_directory_is_empty(self, tmp_path):
        assert document_files(tmp_path, "nope/x.md") == []
