"""Corpus drift: the state DB says a file exists but it is gone from disk.

Real incident (Oct 2026): the cloud-synced output folder was reset, so ~26k of ~30k files
vanished while ``state.sqlite`` (kept outside the cloud folder) still said "all synced".
Incremental discovery only looks at what changed, so nothing was ever re-created.
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

from workctx import drift
from workctx.corpus import document_matcher
from workctx.models import RunStatus, SourceObject, SourceType
from workctx.state import StateDB
from workctx.sync import run_sync


def obj(source_id: str, path: str | None, **kw) -> SourceObject:
    return SourceObject(
        source_name="s",
        source_type=SourceType.JIRA,
        source_id=source_id,
        source_version=kw.pop("source_version", "v1"),
        content_sha256=kw.pop("content_sha256", "abc"),
        output_path=path,
        **kw,
    )


def touch(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")


# ------------------------------------------------------------------ matching


class TestDocumentMatcher:
    def test_single_file(self):
        parent, match = document_matcher("a/b/x.pdf.md")
        assert parent == "a/b"
        assert match("x.pdf.md") and not match("y.pdf.md")

    def test_any_part_names_the_whole_document(self):
        parent, match = document_matcher("a/x.pdf.part-001.md")
        assert parent == "a"
        assert match("x.pdf.part-003.md") and match("x.pdf.md") and not match("z.pdf.md")

    def test_names_with_brackets_match_literally(self):
        _, match = document_matcher("a/[draft] x.md")
        assert match("[draft] x.md") and not match("d x.md")


# ----------------------------------------------------------------- detection


class TestFindMissingOutputs:
    def test_reports_only_the_missing(self, tmp_path):
        touch(tmp_path, "jira/p/A-1.md")
        rows = [obj("1", "jira/p/A-1.md"), obj("2", "jira/p/A-2.md")]
        assert [o.source_id for o in drift.find_missing_outputs(tmp_path, rows)] == ["2"]

    def test_missing_directory_means_missing(self, tmp_path):
        rows = [obj("1", "gone/dir/a.md")]
        assert [o.source_id for o in drift.find_missing_outputs(tmp_path, rows)] == ["1"]

    def test_a_split_document_is_present_if_any_part_exists(self, tmp_path):
        touch(tmp_path, "d/x.pdf.part-002.md")
        rows = [obj("1", "d/x.pdf.part-001.md")]
        assert drift.find_missing_outputs(tmp_path, rows) == []

    def test_rows_without_an_output_path_are_ignored(self, tmp_path):
        assert drift.find_missing_outputs(tmp_path, [obj("1", None)]) == []

    def test_unreadable_directory_is_unknown_not_missing(self, tmp_path, monkeypatch):
        """A busy cloud folder answers EDEADLK/ETIMEDOUT: never conclude a file is gone."""
        touch(tmp_path, "d/a.md")
        real = os.listdir

        def flaky(path):
            if str(path).endswith("/d"):
                raise OSError(errno.EDEADLK, "Resource deadlock avoided")
            return real(path)

        monkeypatch.setattr(drift.os, "listdir", flaky)
        assert drift.find_missing_outputs(tmp_path, [obj("1", "d/a.md")]) == []

    def test_reads_each_directory_once(self, tmp_path, monkeypatch):
        for i in range(50):
            touch(tmp_path, f"d/{i}.md")
        calls: list[str] = []
        real = os.listdir
        monkeypatch.setattr(drift.os, "listdir", lambda p: (calls.append(str(p)), real(p))[1])
        rows = [obj(str(i), f"d/{i}.md") for i in range(50)]
        assert drift.find_missing_outputs(tmp_path, rows) == []
        assert len(calls) == 1


# ------------------------------------------------------------------- marking


class TestMarkForRefetch:
    @pytest.fixture
    def db(self, tmp_path):
        d = StateDB(tmp_path / "state.sqlite")
        yield d
        d.close()

    def test_clears_what_makes_sources_skip_the_row(self, db):
        db.upsert_object(obj("1", "a.md", retry_count=3, last_error="stub:conversion_failed"))
        db.upsert_object(obj("2", "b.md"))
        assert db.mark_for_refetch("s", ["1"]) == 1
        one, two = db.get_object("s", "1"), db.get_object("s", "2")
        assert one.source_version is None and one.content_sha256 is None
        assert one.last_error == drift.MISSING_OUTPUT_ERROR and one.retry_count == 0
        assert one.output_path == "a.md"  # the row (and its history) is kept
        assert two.source_version == "v1" and two.content_sha256 == "abc"  # untouched

    def test_handles_more_ids_than_sqlite_variables(self, db):
        ids = [str(i) for i in range(2500)]
        for i in ids:
            db.upsert_object(obj(i, f"{i}.md"))
        assert db.mark_for_refetch("s", ids) == 2500


class TestRepairDrift:
    def test_marks_missing_and_returns_count(self, tmp_path):
        db = StateDB(tmp_path / "state.sqlite")
        root = tmp_path / "out"
        touch(root, "a.md")
        db.upsert_object(obj("1", "a.md"))
        db.upsert_object(obj("2", "b.md"))
        assert drift.repair_drift("s", db, root) == 1
        assert db.get_object("s", "2").last_error == drift.MISSING_OUTPUT_ERROR
        assert db.get_object("s", "1").last_error is None
        assert drift.repair_drift("s", db, root) == 1  # still missing: still reported
        db.close()

    def test_dry_run_reports_without_touching_the_db(self, tmp_path):
        db = StateDB(tmp_path / "state.sqlite")
        db.upsert_object(obj("2", "b.md"))
        assert drift.repair_drift("s", db, tmp_path, dry_run=True) == 1
        assert db.get_object("s", "2").content_sha256 == "abc"
        db.close()


# ---------------------------------------------------------------- sync wiring


class RecordingSource:
    """Minimal Source that records whether the sync asked for a full listing."""

    def __init__(self, *, needs_full: bool):
        self.name = "s"
        self.source_type = SourceType.JIRA
        self.refetch_needs_full = needs_full
        self.full_args: list[bool] = []

    def discover_changes(self, db, checkpoint, *, full=False):
        self.full_args.append(full)
        return []

    def reconcile_supported(self):
        return False


@pytest.fixture
def wiring(sp_project, tmp_path):
    from workctx.indexing import SearchIndex
    from workctx.sync import _sync_source

    db = StateDB(tmp_path / "w.sqlite")
    idx = SearchIndex(tmp_path / "w.sqlite")
    root = tmp_path / "wiring-out"
    root.mkdir()

    def run(source, **kw):
        return _sync_source(source, sp_project, db, idx, root, **kw)

    yield db, run
    db.close()
    idx.close()


class TestSyncForcesFullOnlyWhereNeeded:
    def test_checkpoint_bound_source_goes_full_when_files_are_missing(self, wiring):
        db, run = wiring
        db.upsert_object(obj("1", "gone.md"))
        src = RecordingSource(needs_full=True)
        run(src)
        assert src.full_args == [True]

    def test_listing_sources_are_not_forced_full(self, wiring):
        db, run = wiring
        db.upsert_object(obj("1", "gone.md"))
        src = RecordingSource(needs_full=False)
        run(src)
        assert src.full_args == [False]
        assert db.get_object("s", "1").last_error == drift.MISSING_OUTPUT_ERROR

    def test_no_drift_means_no_forced_full(self, wiring):
        _db, run = wiring
        src = RecordingSource(needs_full=True)
        run(src)
        assert src.full_args == [False]

    def test_dry_run_does_not_mark_anything(self, wiring):
        db, run = wiring
        db.upsert_object(obj("1", "gone.md"))
        run(RecordingSource(needs_full=True), dry_run=True)
        assert db.get_object("s", "1").content_sha256 == "abc"

    def test_checkpoint_bound_adapters_declare_it(self):
        from workctx.sources.confluence import ConfluenceAdapter
        from workctx.sources.jira import JiraAdapter
        from workctx.sources.slack import SlackAdapter

        for adapter in (JiraAdapter, ConfluenceAdapter, SlackAdapter):
            assert adapter.refetch_needs_full is True


# ----------------------------------------------------------------- end to end


def md_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in (root / "sharepoint").rglob("*.md")}


class TestEndToEnd:
    def test_wiping_the_output_folder_is_healed_by_the_next_sync(self, sp_library, sp_project):
        """The incident: output folder reset, state DB intact."""
        sp_library.add("A/one.txt", b"first document")
        sp_library.add("B/two.txt", b"second document")
        out = sp_project.output_root_path

        assert run_sync(sp_project, run_id="r1", quiet=True).aggregate_status() == RunStatus.HEALTHY
        before = md_files(out)
        assert len(before) == 2

        shutil.rmtree(out)  # OneDrive reset: every file gone, DB still says "synced"

        result = run_sync(sp_project, run_id="r2", quiet=True)
        assert result.aggregate_status() == RunStatus.HEALTHY
        assert md_files(out) == before
        assert "first document" in (out / sorted(before)[0]).read_text()

        # converged: a third run finds nothing to repair and rewrites nothing
        mtimes = {p: (out / p).stat().st_mtime_ns for p in before}
        run_sync(sp_project, run_id="r3", quiet=True)
        assert {p: (out / p).stat().st_mtime_ns for p in before} == mtimes

    def test_a_single_deleted_file_is_restored_and_others_untouched(self, sp_library, sp_project):
        sp_library.add("A/one.txt", b"first document")
        sp_library.add("B/two.txt", b"second document")
        out = sp_project.output_root_path
        run_sync(sp_project, run_id="r1", quiet=True)
        files = sorted(md_files(out))
        keep_mtime = (out / files[1]).stat().st_mtime_ns

        (out / files[0]).unlink()
        run_sync(sp_project, run_id="r2", quiet=True)

        assert (out / files[0]).exists()
        assert (out / files[1]).stat().st_mtime_ns == keep_mtime


class TestMailAndCalendarSkipRulesHonourTheMarker:
    """Mail/calendar skip a row unless it carries an error, so the marker must defeat that."""

    def test_missing_mail_file_is_rediscovered_without_a_full_listing(self, outlook, tmp_path):
        from tests.fake_web import OUTLOOK_HOST, days_ago
        from workctx.config import MailSource
        from workctx.models import ChangeAction
        from workctx.sources.outlook import MailAdapter

        outlook.add_message("m1", "Kept", days_ago(1))
        outlook.add_message("m2", "Lost", days_ago(2))
        adapter = MailAdapter(
            MailSource(
                name="mail",
                profile="p",
                api_base=f"https://{OUTLOOK_HOST}/api/v2.0",
            )
        )
        db = StateDB(tmp_path / "state.sqlite")
        root = tmp_path / "out"
        touch(root, "email/mail/kept.md")
        for sid, path in (("m1", "email/mail/kept.md"), ("m2", "email/mail/lost.md")):
            db.upsert_object(
                SourceObject(
                    source_name="mail",
                    source_type=SourceType.EMAIL,
                    source_id=sid,
                    source_version="r:x",
                    content_sha256="h",
                    output_path=path,
                )
            )

        assert {c.source_id for c in adapter.discover_changes(db, None)} == set()  # in sync

        assert drift.repair_drift("mail", db, root) == 1
        changes = adapter.discover_changes(db, None)
        assert [(c.source_id, c.action) for c in changes] == [("m2", ChangeAction.UPDATE)]
        db.close()


class TestVerifyCommand:
    def test_reports_and_exits_nonzero_when_files_are_missing(
        self, sp_library, sp_project, tmp_path
    ):
        import yaml
        from click.testing import CliRunner

        from workctx.cli import main

        sp_library.add("A/one.txt", b"first document")
        out = sp_project.output_root_path
        run_sync(sp_project, run_id="r1", quiet=True)
        cfg_path = tmp_path / "c.yaml"
        assert yaml.safe_load(cfg_path.read_text())  # the fixture's config file

        ok = CliRunner().invoke(main, ["verify", "-c", str(cfg_path)])
        assert ok.exit_code == 0 and "All indexed documents are present" in ok.output

        shutil.rmtree(out / "sharepoint")
        bad = CliRunner().invoke(main, ["verify", "-c", str(cfg_path)])
        assert bad.exit_code == 1 and "missing on disk" in bad.output

        # read-only: it must not have repaired anything
        assert not (out / "sharepoint").exists()
