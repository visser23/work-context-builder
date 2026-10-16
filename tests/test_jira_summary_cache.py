"""The Jira SUMMARY must not re-read every issue file on every sync."""

from __future__ import annotations

import csv
import io
from unittest.mock import MagicMock

import pytest

from workctx.corpus import generate_jira_summary, write_corpus_file
from workctx.models import SourceObject, SourceType
from workctx.state import StateDB

SRC = "jira-main"


def _issue_md(status: str) -> str:
    return (
        "---\ntitle: T\n---\n\n# K-1: T\n\n## Details\n\n"
        f"- **Type**: Bug\n- **Status**: {status}\n- **Priority**: High\n"
    )


@pytest.fixture
def env(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    db = StateDB(tmp_path / "state.sqlite")
    cfg = MagicMock()
    jira = MagicMock()
    jira.name = SRC
    cfg.sources.jira = [jira]
    return out, db, cfg


def _add(db, out, n: int, status: str, sha: str) -> SourceObject:
    rel = f"jira/{SRC}/K/K-{n}.md"
    write_corpus_file(out, rel, _issue_md(status))
    obj = SourceObject(
        source_name=SRC,
        source_type=SourceType.JIRA,
        source_id=str(n),
        source_key=f"K-{n}",
        title=f"Issue {n}",
        output_path=rel,
        content_sha256=sha,
    )
    db.upsert_object(obj)
    return obj


def _rows(out):
    text = (out / "jira" / SRC / "SUMMARY.csv").read_text()
    return {r["Key"]: r for r in csv.DictReader(io.StringIO(text))}


def test_second_run_reads_no_issue_files(env, monkeypatch):
    out, db, cfg = env
    for n in (1, 2, 3):
        _add(db, out, n, "Open", f"sha{n}")

    generate_jira_summary(cfg, db, out)
    assert _rows(out)["K-2"]["Status"] == "Open"

    calls: list[str] = []
    import workctx.corpus as corpus

    real = corpus._extract_jira_details
    monkeypatch.setattr(
        corpus, "_extract_jira_details", lambda *a, **k: calls.append(a[1]) or real(*a, **k)
    )
    generate_jira_summary(cfg, db, out)

    assert calls == [], "unchanged issues must come from the cache, not disk"
    assert _rows(out)["K-2"]["Status"] == "Open"


def test_changed_issue_is_reparsed_others_stay_cached(env, monkeypatch):
    out, db, cfg = env
    for n in (1, 2):
        _add(db, out, n, "Open", f"sha{n}")
    generate_jira_summary(cfg, db, out)

    _add(db, out, 2, "Done", "sha2-new")  # issue 2 edited upstream

    import workctx.corpus as corpus

    calls: list[str] = []
    real = corpus._extract_jira_details
    monkeypatch.setattr(
        corpus, "_extract_jira_details", lambda *a, **k: calls.append(a[1]) or real(*a, **k)
    )
    generate_jira_summary(cfg, db, out)

    assert calls == [f"jira/{SRC}/K/K-2.md"]
    rows = _rows(out)
    assert rows["K-1"]["Status"] == "Open"
    assert rows["K-2"]["Status"] == "Done"


def test_objects_without_hash_are_always_read(env):
    out, db, cfg = env
    _add(db, out, 1, "Open", None)
    generate_jira_summary(cfg, db, out)
    assert db.get_jira_details(SRC) == {}
    assert _rows(out)["K-1"]["Status"] == "Open"


def test_corrupt_cache_row_is_a_miss(env):
    out, db, cfg = env
    _add(db, out, 1, "Open", "sha1")
    db.conn.execute("INSERT INTO jira_details VALUES (?, '1', 'sha1', 'not-json{')", (SRC,))
    db.conn.commit()
    generate_jira_summary(cfg, db, out)
    assert _rows(out)["K-1"]["Status"] == "Open"
    assert db.get_jira_details(SRC)["1"][1]["Status"] == "Open"  # healed
