"""An item whose content has not changed must not be written again.

Sources such as calendar re-emit their whole window on every run. The sync must still touch
only real deltas, otherwise every run rewrites thousands of files and a file-sync client
(OneDrive, Dropbox) uploads them all again. ``synced_at`` is bookkeeping, not content, so it
must not make an identical item look changed.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from workctx.indexing import SearchIndex
from workctx.models import ChangeAction, DiscoveredChange, SourceType
from workctx.state import StateDB
from workctx.sync import _write_and_index


@pytest.fixture
def env(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    db = StateDB(tmp_path / "state" / "state.sqlite")
    idx = SearchIndex(tmp_path / "state" / "state.sqlite")
    source = SimpleNamespace(source_type=SourceType.CALENDAR, name="cal")
    config = SimpleNamespace(sync=SimpleNamespace(large_document_chars=300_000))
    yield SimpleNamespace(out=out, db=db, idx=idx, source=source, config=config)
    db.close()
    idx.close()


def change(title="Standup", version="v1", **meta):
    return DiscoveredChange(
        source_id="evt-1",
        title=title,
        source_version=version,
        source_updated_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
        action=ChangeAction.UPDATE,
        metadata={"occurred_on": "2026-10-20", **meta},
    )


def write(env, body, **kw):
    _write_and_index(env.source, change(**kw), env.config, env.db, env.idx, env.out, body, False)


def the_file(env):
    files = list(env.out.rglob("*.md"))
    assert len(files) == 1, files
    return files[0]


def age(path):
    """Push the mtime into the past so any rewrite is visible."""
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path.stat().st_mtime_ns


def test_identical_item_is_not_rewritten(env):
    write(env, "Daily standup at 9")
    path = the_file(env)
    first_text = path.read_text()
    mtime = age(path)

    time.sleep(0.01)  # synced_at would differ if it were regenerated and written
    write(env, "Daily standup at 9")

    assert path.stat().st_mtime_ns == mtime
    assert path.read_text() == first_text


def test_unchanged_item_does_not_touch_other_files_or_the_index(env):
    write(env, "Daily standup at 9")
    obj = env.db.get_object("cal", "evt-1")
    write(env, "Daily standup at 9")
    again = env.db.get_object("cal", "evt-1")
    assert again.content_sha256 == obj.content_sha256


def test_changed_body_is_rewritten_with_a_fresh_synced_at(env):
    write(env, "Daily standup at 9")
    path = the_file(env)
    before = path.read_text()
    mtime = age(path)

    time.sleep(0.01)
    write(env, "Daily standup moved to 10", version="v2")

    assert path.stat().st_mtime_ns != mtime
    text = path.read_text()
    assert "moved to 10" in text
    assert text != before
    assert "synced_at" in text


def test_changed_front_matter_is_rewritten(env):
    write(env, "Daily standup at 9")
    path = the_file(env)
    mtime = age(path)

    write(env, "Daily standup at 9", title="Standup (renamed)")

    renamed = list(env.out.rglob("*.md"))
    assert len(renamed) == 1
    assert "Standup (renamed)" in renamed[0].read_text()
    assert renamed[0] != path or renamed[0].stat().st_mtime_ns != mtime


def test_stored_file_still_carries_a_synced_at(env):
    write(env, "Daily standup at 9")
    assert "synced_at" in the_file(env).read_text()


def test_identical_stub_is_not_rewritten_but_still_counts_a_retry(env):
    def stub():
        _write_and_index(env.source, change(), env.config, env.db, env.idx, env.out, None, False)

    stub()
    path = the_file(env)
    first = env.db.get_object("cal", "evt-1").retry_count
    mtime = age(path)

    stub()
    stub()

    assert path.stat().st_mtime_ns == mtime
    assert env.db.get_object("cal", "evt-1").retry_count == first + 2
