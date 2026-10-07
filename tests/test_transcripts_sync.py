"""End-to-end sync tests for Teams transcripts (fake tenant, real pipeline).

Exercises discovery -> render -> corpus write -> state DB -> FTS index, plus
incremental updates, reconciliation and the ``--source`` filter.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from click.testing import CliRunner

from tests.fake_sharepoint import make_source, uid
from workctx.cli import main as cli_main
from workctx.config import load_config
from workctx.indexing import SearchIndex
from workctx.models import RunStatus
from workctx.state import StateDB
from workctx.sync import _reconcile_source, run_sync


@pytest.fixture
def project(tmp_path):
    out = tmp_path / "out"
    state = tmp_path / "state"
    cfg = {
        "version": 1,
        "project": {
            "id": "tx-test",
            "name": "Transcript Test",
            "output_root": str(out),
            "state_dir": str(state),
        },
        "sources": {
            "sharepoint": [
                {
                    "name": "sp",
                    "site_url": "https://contoso.sharepoint.com/sites/Anything",
                    "mode": "browser",
                    "auth": {"mode": "browser", "secret_ref": "sp-cookies"},
                }
            ],
            "transcripts": [{"name": "teams", "sharepoint_source": "sp"}],
        },
        "notifications": {"telegram": {"enabled": False}, "macos": {"enabled": False}},
    }
    path = tmp_path / "workctx.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return {"config": load_config(path), "path": path, "out": out, "state": state}


def run(project, **kwargs):
    return run_sync(
        project["config"], run_id="t", quiet=True, only_sources=frozenset({"teams"}), **kwargs
    )


def transcript_files(out):
    return sorted((out / "transcripts" / "teams").rglob("*.md"))


def test_initial_sync_writes_indexed_markdown(tenant, project):
    tenant.add(uid(1), "Weekly sync")
    tenant.add(
        uid(2),
        "Customer call",
        scope="shared",
        file_name="Customer call-20250102_090000UTC-Meeting Recording.mp4",
    )

    result = run(project)

    assert result.status == RunStatus.HEALTHY
    (sr,) = result.source_results
    assert (sr.objects_added, sr.objects_failed) == (2, 0)

    files = {f.name: f for f in transcript_files(project["out"])}
    assert set(files) == {
        f"2026-06-25-weekly-sync-{uid(1)[:8]}.md",
        f"2025-01-02-customer-call-{uid(2)[:8]}.md",
    }
    assert files[f"2026-06-25-weekly-sync-{uid(1)[:8]}.md"].parent.name == "2026"

    text = files[f"2026-06-25-weekly-sync-{uid(1)[:8]}.md"].read_text()
    front, _, body = text.partition("\n---\n")
    meta = yaml.safe_load(front.strip("-\n"))
    assert meta["source_type"] == "transcripts"
    assert meta["source_name"] == "teams"
    assert meta["title"] == "Weekly sync"
    assert str(meta["meeting_date"]) == "2026-06-25"
    assert meta["participants"] == ["Ada Example", "Bob Example"]
    assert meta["duration_minutes"] == 30
    assert "**Ada Example:** Hello team." in body

    # Indexed and searchable
    idx = SearchIndex(project["state"] / "state.sqlite")
    try:
        hits = idx.search("Hello team", limit=5)
    finally:
        idx.close()
    assert {h["source_type"] for h in hits} == {"transcripts"}
    assert len(hits) == 2

    # Corpus-level docs mention the new source
    index_md = (project["out"] / "_meta" / "INDEX.md").read_text()
    assert "Teams Transcripts: teams" in index_md and "Meeting transcripts: 2" in index_md
    assert "transcripts/" in (project["out"] / "CLAUDE.md").read_text()
    assert "Teams transcripts" in (project["out"] / "PROJECT_BRIEF.md").read_text()
    manifest = [
        json.loads(line)
        for line in (project["out"] / "_meta" / "manifest.jsonl").read_text().splitlines()
    ]
    assert {m["source_type"] for m in manifest} == {"transcripts"}


def test_second_sync_is_a_noop_and_edits_are_picked_up(tenant, project):
    recent = datetime.now(UTC) - timedelta(days=2)
    tenant.add(uid(1), "Weekly sync", modified=recent)
    run(project)

    again = run(project).source_results[0]
    assert (again.objects_added, again.objects_updated) == (0, 0)

    rec = tenant.recordings[uid(1)]
    rec["transcripts"][0]["cTag"] = "ctag-new"
    rec["entries"].append(
        {
            "text": "A late correction.",
            "speakerDisplayName": "Ada Example",
            "startOffset": "00:10:00",
            "endOffset": "00:10:02",
        }
    )
    updated = run(project).source_results[0]
    assert (updated.objects_added, updated.objects_updated) == (0, 1)
    (file,) = transcript_files(project["out"])
    assert "A late correction." in file.read_text()


def test_long_transcript_is_fully_searchable(tenant, project):
    def clock(sec: int) -> str:
        return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"

    filler = [
        {
            "text": "ordinary filler sentence about nothing in particular " * 2,
            "speakerDisplayName": "Ada Example",
            "startOffset": clock(i),
            "endOffset": clock(i + 1),
        }
        for i in range(0, 40_000, 30)  # gaps > merge threshold => many turns
    ]
    filler.append(
        {
            "text": "The closing keyword is zanzibarquux.",
            "speakerDisplayName": "Bob Example",
            "startOffset": "13:00:00",
            "endOffset": "13:00:03",
        }
    )
    tenant.add(uid(1), "Marathon", entries=filler)
    run(project)

    (file,) = transcript_files(project["out"])
    assert file.stat().st_size > 50_000, "fixture should exceed the old 50k index cap"
    idx = SearchIndex(project["state"] / "state.sqlite")
    try:
        assert idx.search("zanzibarquux", limit=3)
    finally:
        idx.close()


def test_source_filter_leaves_other_sources_alone(tenant, project):
    tenant.add(uid(1), "Weekly sync")
    result = run_sync(project["config"], run_id="t", quiet=True, only_sources=frozenset({"teams"}))
    assert [r.source_name for r in result.source_results] == ["teams"]
    db = StateDB(project["state"] / "state.sqlite")
    try:
        assert db.get_checkpoint("sp") is None
        assert db.get_checkpoint("teams") is not None
    finally:
        db.close()


def test_unknown_source_filter_fails_the_run(tenant, project):
    result = run_sync(project["config"], run_id="t", quiet=True, only_sources=frozenset({"nope"}))
    assert result.status == RunStatus.FAILED


def test_reconciliation_removes_recordings_that_disappeared(tenant, project):
    tenant.add(uid(1), "Keeps")
    tenant.add(uid(2), "Goes away")
    run(project)
    assert len(transcript_files(project["out"])) == 2

    del tenant.recordings[uid(2)]
    db = StateDB(project["state"] / "state.sqlite")
    idx = SearchIndex(project["state"] / "state.sqlite")
    source = make_source()
    try:
        _reconcile_source(source, db, idx, project["out"])
        assert db.get_all_source_ids("teams") == {uid(1)}
    finally:
        source.close()
        db.close()
        idx.close()
    names = [f.name for f in transcript_files(project["out"])]
    assert names == [f"2026-06-25-keeps-{uid(1)[:8]}.md"]


def test_reconciliation_aborts_instead_of_mass_deleting_on_search_failure(tenant, project):
    tenant.add(uid(1), "Keeps")
    run(project)
    tenant.search_status = 500
    db = StateDB(project["state"] / "state.sqlite")
    idx = SearchIndex(project["state"] / "state.sqlite")
    source = make_source()
    try:
        _reconcile_source(source, db, idx, project["out"])
        assert db.get_all_source_ids("teams") == {uid(1)}
    finally:
        source.close()
        db.close()
        idx.close()
    assert len(transcript_files(project["out"])) == 1


def test_expired_session_marks_source_failed(tenant, project):
    tenant.add(uid(1), "Weekly sync")
    tenant.auth_status = 401
    result = run(project)
    assert result.source_results[0].status == RunStatus.FAILED
    assert result.aggregate_status() == RunStatus.FAILED
    assert "session" in result.source_results[0].errors[0].lower()


# ---------------------------------------------------------------------------- CLI
def test_cli_sync_rejects_unknown_source(project):
    result = CliRunner().invoke(
        cli_main, ["sync", "-c", str(project["path"]), "--source", "ghost", "--dry-run"]
    )
    assert result.exit_code == 2
    assert "Unknown source" in result.output
    assert "teams" in result.output


def test_cli_login_for_reused_login_points_at_sharepoint_source(project):
    result = CliRunner().invoke(
        cli_main, ["auth", "login-sharepoint", "-c", str(project["path"]), "--source", "teams"]
    )
    assert result.exit_code == 1
    assert "reuses the login of SharePoint source" in result.output
    assert "--source sp" in result.output


def test_cli_login_unknown_source(project):
    result = CliRunner().invoke(
        cli_main, ["auth", "login-sharepoint", "-c", str(project["path"]), "--source", "ghost"]
    )
    assert result.exit_code == 1
    assert "not found" in result.output
