"""End-to-end SharePoint (browser mode) sync over a fake library, and the layout migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fake_sp_library import BASE
from workctx.indexing import SearchIndex
from workctx.models import RunStatus, SourceObject, SourceType
from workctx.relayout import relayout_sharepoint
from workctx.state import StateDB
from workctx.sync import run_sync

# v1.3.x stored paths with a doubled slash after the source folder; keep that quirk here.
OLD_PREFIX = f"sharepoint/sp//{BASE.lstrip('/')}"


@pytest.fixture
def project(sp_project):
    return sp_project


def md_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in (root / "sharepoint").rglob("*.md")}


class TestEndToEnd:
    def test_documents_land_directly_under_the_source_folder(self, sp_library, project):
        sp_library.add("0.DDTx Portfolio/strategy.txt", b"strategy text")
        sp_library.add("4. Remote Care/PHR/plan.txt", b"phr plan")

        result = run_sync(project, run_id="t1", quiet=True)

        assert result.aggregate_status() == RunStatus.HEALTHY
        assert md_files(project.output_root_path) == {
            "sharepoint/sp/0.DDTx Portfolio/strategy.txt.md",
            "sharepoint/sp/4. Remote Care/PHR/plan.txt.md",
        }

    def test_changes_renames_and_new_folders_flow_through_later_runs(self, sp_library, project):
        """The Oct 2026 failure: after the first sync nothing ever changed again."""
        sp_library.add("keep.txt", b"v1", modified="2026-10-01T10:00:00Z")
        sp_library.add("old folder/a.txt", b"a", modified="2026-10-01T10:00:00Z")
        run_sync(project, run_id="t1", quiet=True)

        # edit a file, rename a folder, add a brand-new folder
        sp_library.add("keep.txt", b"v2 changed", modified="2026-10-14T10:00:00Z")
        sp_library.remove("old folder/a.txt")
        sp_library.folders.discard(f"{BASE}/old folder")
        sp_library.add("renamed folder/a.txt", b"a", modified="2026-10-01T10:00:00Z")
        sp_library.add("4. Remote Care/new.txt", b"fresh")
        result = run_sync(project, run_id="t2", quiet=True)

        assert result.aggregate_status() == RunStatus.HEALTHY
        root = project.output_root_path
        assert md_files(root) == {
            "sharepoint/sp/keep.txt.md",
            "sharepoint/sp/renamed folder/a.txt.md",
            "sharepoint/sp/4. Remote Care/new.txt.md",
        }
        assert "v2 changed" in (root / "sharepoint/sp/keep.txt.md").read_text()
        assert not (root / "sharepoint/sp/old folder").exists()

    def test_second_run_with_no_changes_does_no_work(self, sp_library, project):
        sp_library.add("a.txt", b"a")
        run_sync(project, run_id="t1", quiet=True)

        result = run_sync(project, run_id="t2", quiet=True)

        (sr,) = result.source_results
        assert (sr.objects_added, sr.objects_updated, sr.objects_deleted) == (0, 0, 0)

    def test_failed_listing_fails_the_source_and_deletes_nothing(self, sp_library, project):
        sp_library.add("a/one.txt", b"1")
        sp_library.add("b/two.txt", b"2")
        run_sync(project, run_id="t1", quiet=True)
        before = md_files(project.output_root_path)

        sp_library.fail("/b')/Files", *([503] * 40))
        result = run_sync(project, run_id="t2", quiet=True)

        (sr,) = result.source_results
        assert sr.status == RunStatus.FAILED
        assert "incomplete" in sr.errors[0]
        assert md_files(project.output_root_path) == before


class TestRelayout:
    def _old_layout(self, project, rels: list[str]):
        """Write documents the way v1.3.x did: under the full server path."""
        root = project.output_root_path
        db = StateDB(project.state_dir / "state.sqlite")
        idx = SearchIndex(project.state_dir / "state.sqlite")
        for rel in rels:
            stored = f"{OLD_PREFIX}/{rel}.md"
            target = root / stored
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"content of {rel}")
            db.upsert_object(
                SourceObject(
                    source_name="sp",
                    source_type=SourceType.SHAREPOINT,
                    source_id=f"{BASE}/{rel}",
                    title=rel,
                    output_path=stored,
                    source_version="v",
                )
            )
            idx.upsert(stored, rel, f"content of {rel}", "sharepoint", "sp", None, None, None)
        return db, idx

    def test_moves_files_updates_db_and_index_and_prunes_old_folders(self, project):
        db, idx = self._old_layout(project, ["3. Pilot/plan.txt", "root.txt"])

        report = relayout_sharepoint(project, db, idx)

        root = project.output_root_path
        assert report.moved == 2
        assert md_files(root) == {
            "sharepoint/sp/3. Pilot/plan.txt.md",
            "sharepoint/sp/root.txt.md",
        }
        assert not (root / "sharepoint/sp/sites").exists()
        obj = db.get_object("sp", f"{BASE}/root.txt")
        assert obj is not None and obj.output_path == "sharepoint/sp/root.txt.md"
        hits = idx.search("content", limit=10)
        assert {h["output_path"] for h in hits} == {
            "sharepoint/sp/3. Pilot/plan.txt.md",
            "sharepoint/sp/root.txt.md",
        }
        db.close()
        idx.close()

    def test_is_idempotent(self, project):
        db, idx = self._old_layout(project, ["a.txt"])
        relayout_sharepoint(project, db, idx)

        again = relayout_sharepoint(project, db, idx)

        assert (again.moved, again.already_ok) == (0, 1)
        db.close()
        idx.close()

    def test_dry_run_changes_nothing(self, project):
        db, idx = self._old_layout(project, ["a.txt"])

        report = relayout_sharepoint(project, db, idx, dry_run=True)

        assert report.moved == 1
        assert (project.output_root_path / f"{OLD_PREFIX}/a.txt.md").exists()
        obj = db.get_object("sp", f"{BASE}/a.txt")
        assert obj is not None and obj.output_path == f"{OLD_PREFIX}/a.txt.md"
        db.close()
        idx.close()

    def test_multipart_documents_move_together(self, project):
        root = project.output_root_path
        db, idx = self._old_layout(project, ["big.txt"])
        old = root / f"{OLD_PREFIX}/big.txt.md"
        old.unlink()
        for i in (1, 2):
            (root / f"{OLD_PREFIX}/big.txt.part-00{i}.md").write_text(f"part {i}")
        db.update_output_path("sp", f"{BASE}/big.txt", f"{OLD_PREFIX}/big.txt.part-001.md")

        relayout_sharepoint(project, db, idx)

        assert md_files(root) == {
            "sharepoint/sp/big.txt.part-001.md",
            "sharepoint/sp/big.txt.part-002.md",
        }
        obj = db.get_object("sp", f"{BASE}/big.txt")
        assert obj is not None and obj.output_path == "sharepoint/sp/big.txt.part-001.md"
        db.close()
        idx.close()

    def test_object_whose_file_is_missing_is_reset_for_refetch(self, project):
        db, idx = self._old_layout(project, ["gone.txt"])
        (project.output_root_path / f"{OLD_PREFIX}/gone.txt.md").unlink()

        report = relayout_sharepoint(project, db, idx)

        assert report.reset == 1
        assert db.get_object("sp", f"{BASE}/gone.txt") is None
        db.close()
        idx.close()

    def test_existing_target_is_reported_not_overwritten(self, project):
        root = project.output_root_path
        db, idx = self._old_layout(project, ["a.txt"])
        (root / "sharepoint/sp").mkdir(parents=True, exist_ok=True)
        (root / "sharepoint/sp/a.txt.md").write_text("someone else's file")

        report = relayout_sharepoint(project, db, idx)

        assert len(report.conflicts) == 1
        assert (root / "sharepoint/sp/a.txt.md").read_text() == "someone else's file"
        assert (root / f"{OLD_PREFIX}/a.txt.md").exists()
        db.close()
        idx.close()

    def test_unreadable_corpus_aborts_instead_of_resetting_everything(self, project, monkeypatch):
        db, idx = self._old_layout(project, ["a.txt"])
        import os

        real = os.listdir

        def flaky(path):
            if "sharepoint" in str(path):
                raise OSError(60, "Operation timed out")
            return real(path)

        monkeypatch.setattr("workctx.relayout.os.listdir", flaky)

        with pytest.raises(OSError, match="timed out"):
            relayout_sharepoint(project, db, idx)

        assert db.get_object("sp", f"{BASE}/a.txt") is not None
        db.close()
        idx.close()
