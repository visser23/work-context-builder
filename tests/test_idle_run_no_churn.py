"""A run that changes nothing must leave the generated corpus files untouched.

Every sync used to rewrite the manifest (~19 MB), PROJECT_BRIEF.md, the Jira SUMMARY files and
the LLM instruction files, even when no source item had changed. A file-sync client such as
OneDrive then re-uploads them all, and a failed run (which the daemon retries) doubles that.
Several of these files embed a wall-clock "generated at" stamp, so the check has to ignore
exactly those stamp lines and nothing else.
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

import pytest

from workctx import corpus
from workctx.corpus import (
    generate_agents_md,
    generate_chatgpt_instructions,
    generate_claude_md,
    generate_context_md,
    generate_jira_summary,
    generate_manifest,
    generate_project_brief,
    generate_readme,
    write_corpus_file,
    write_text_if_changed,
)
from workctx.models import SourceObject, SourceType
from workctx.state import StateDB

STAMP_A = "01 Jan 2026 09:00 UTC"
STAMP_B = "02 Jan 2026 10:30 UTC"


def _age(path):
    """Push the mtime into the past so any rewrite is visible."""
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path.stat().st_mtime_ns


# ------------------------------------------------------------ write_text_if_changed


def test_identical_content_is_not_rewritten(tmp_path):
    path = tmp_path / "a.md"
    assert write_text_if_changed(path, "hello\n") is True
    mtime = _age(path)

    assert write_text_if_changed(path, "hello\n") is False
    assert path.stat().st_mtime_ns == mtime


def test_missing_file_is_written(tmp_path):
    path = tmp_path / "sub" / "a.md"
    assert write_text_if_changed(path, "x") is True
    assert path.read_text() == "x"


def test_changed_content_is_rewritten(tmp_path):
    path = tmp_path / "a.md"
    write_text_if_changed(path, "one")
    mtime = _age(path)

    assert write_text_if_changed(path, "two") is True
    assert path.read_text() == "two"
    assert path.stat().st_mtime_ns != mtime


@pytest.mark.parametrize(
    "template",
    [
        "# T\n\n**Last updated:** {s}\n\nbody\n",
        "# T\n\n*Auto-generated on {s} by Work Context Mirror.*\n\nbody\n",
        "# T\n\n*12 issues as of {s}*\n\n| a |\n",
    ],
)
def test_only_the_generated_at_stamp_is_ignored(tmp_path, template):
    path = tmp_path / "a.md"
    write_text_if_changed(path, template.format(s=STAMP_A))
    mtime = _age(path)

    assert write_text_if_changed(path, template.format(s=STAMP_B)) is False
    assert path.stat().st_mtime_ns == mtime
    # The stale stamp stays on disk until something real changes.
    assert STAMP_A in path.read_text()


def test_a_date_in_real_content_is_not_mistaken_for_a_stamp(tmp_path):
    """A Jira 'Updated' cell in the same format must still trigger a rewrite."""
    path = tmp_path / "a.md"
    write_text_if_changed(path, f"| K-1 | {STAMP_A} |\n")
    assert write_text_if_changed(path, f"| K-1 | {STAMP_B} |\n") is True
    assert STAMP_B in path.read_text()


def test_real_change_alongside_a_new_stamp_is_written_with_the_new_stamp(tmp_path):
    path = tmp_path / "a.md"
    write_text_if_changed(path, f"**Last updated:** {STAMP_A}\n\n12 issues\n")
    write_text_if_changed(path, f"**Last updated:** {STAMP_B}\n\n13 issues\n")
    text = path.read_text()
    assert "13 issues" in text
    assert STAMP_B in text


def test_unreadable_existing_file_falls_back_to_writing(tmp_path, monkeypatch):
    path = tmp_path / "a.md"
    path.write_text("same")

    def boom(self):
        raise OSError(60, "Operation timed out")

    monkeypatch.setattr(type(path), "read_bytes", boom)
    assert write_text_if_changed(path, "same") is True


def test_write_corpus_file_skip_flag_is_opt_in(tmp_path):
    """Item documents keep the old always-write behaviour unless asked."""
    write_corpus_file(tmp_path, "d.md", "x")
    mtime = _age(tmp_path / "d.md")
    write_corpus_file(tmp_path, "d.md", "x")
    assert (tmp_path / "d.md").stat().st_mtime_ns != mtime

    mtime = _age(tmp_path / "d.md")
    write_corpus_file(tmp_path, "d.md", "x", skip_if_unchanged=True)
    assert (tmp_path / "d.md").stat().st_mtime_ns == mtime


# ------------------------------------------------------------ whole generated set


@pytest.fixture
def env(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    db = StateDB(tmp_path / "state.sqlite")
    jira = MagicMock()
    jira.name = "jira-main"
    jira.projects = ["K"]
    cfg = MagicMock()
    cfg.project.name = "Proj"
    cfg.project.id = "proj"
    cfg.sources.jira = [jira]
    for kind in ("confluence", "sharepoint", "local_folders", "transcripts", "mail", "calendar"):
        setattr(cfg.sources, kind, [])
    cfg.sources.slack = []

    for n in (1, 2):
        rel = f"jira/jira-main/K/K-{n}.md"
        write_corpus_file(out, rel, f"## Details\n\n- **Status**: Open\n- **Type**: Bug\n#{n}\n")
        db.upsert_object(
            SourceObject(
                source_name="jira-main",
                source_type=SourceType.JIRA,
                source_id=str(n),
                source_key=f"K-{n}",
                title=f"Issue {n}",
                output_path=rel,
                content_sha256=f"sha{n}",
            )
        )
    yield out, db, cfg
    db.close()


def _generate_all(out, db, cfg):
    generate_manifest(db, out)
    generate_jira_summary(cfg, db, out)
    generate_context_md(cfg, out)
    generate_agents_md(cfg, out)
    generate_claude_md(cfg, out)
    generate_chatgpt_instructions(cfg, out)
    generate_project_brief(cfg, db, out)
    generate_readme(cfg, out)


def _generated_files(out):
    names = ["CONTEXT.md", "AGENTS.md", "CLAUDE.md", "CHATGPT_INSTRUCTIONS.md"]
    names += ["PROJECT_BRIEF.md", "README.md", "_meta/manifest.jsonl"]
    names += ["jira/jira-main/SUMMARY.csv", "jira/jira-main/SUMMARY.md"]
    return [out / n for n in names]


def test_second_identical_run_rewrites_no_generated_file(env, monkeypatch):
    out, db, cfg = env
    _generate_all(out, db, cfg)
    files = _generated_files(out)
    assert all(f.exists() for f in files)
    mtimes = {f: _age(f) for f in files}

    # A later minute: every embedded "generated at" stamp now differs.
    monkeypatch.setattr(corpus, "_now_stamp", lambda: STAMP_B)
    _generate_all(out, db, cfg)

    rewritten = [f.name for f in files if f.stat().st_mtime_ns != mtimes[f]]
    assert rewritten == []


def test_a_real_change_still_reaches_the_brief_summary_and_manifest(env, monkeypatch):
    out, db, cfg = env
    _generate_all(out, db, cfg)
    files = _generated_files(out)
    for f in files:
        _age(f)

    rel = "jira/jira-main/K/K-2.md"
    write_corpus_file(out, rel, "## Details\n\n- **Status**: Done\n- **Type**: Bug\n#2\n")
    db.upsert_object(
        SourceObject(
            source_name="jira-main",
            source_type=SourceType.JIRA,
            source_id="2",
            source_key="K-2",
            title="Issue 2",
            output_path=rel,
            content_sha256="sha2-new",
        )
    )
    monkeypatch.setattr(corpus, "_now_stamp", lambda: STAMP_B)
    _generate_all(out, db, cfg)

    assert "Done" in (out / "jira/jira-main/SUMMARY.csv").read_text()
    assert "Done" in (out / "PROJECT_BRIEF.md").read_text()
    assert "sha2-new" in (out / "_meta/manifest.jsonl").read_text()
    assert STAMP_B in (out / "PROJECT_BRIEF.md").read_text()
