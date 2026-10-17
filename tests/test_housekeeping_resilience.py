"""Housekeeping before a sync must not abort the sync on a transient filesystem error.

A cloud-synced folder (OneDrive File Provider) answers some ``stat`` calls with
``EDEADLK`` / ``EAGAIN`` while it is busy. Pruning is idempotent and runs again next time,
so an unreadable path is skipped, not fatal.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from workctx.indexing import SearchIndex
from workctx.models import SourceObject, SourceType
from workctx.state import StateDB
from workctx.sync import _prune_stale_twins


@pytest.fixture
def env(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    db = StateDB(tmp_path / "state" / "state.sqlite")
    idx = SearchIndex(tmp_path / "state" / "state.sqlite")
    yield out, db, idx
    db.close()
    idx.close()


def add_split_doc(out: Path, db: StateDB, name: str, *, with_twin: bool) -> str:
    folder = out / "docs"
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.part-001.md").write_text("p1")
    twin = folder / f"{name}.md"
    if with_twin:
        twin.write_text("old single file")
    db.upsert_object(
        SourceObject(
            source_name="sp",
            source_type=SourceType.SHAREPOINT,
            source_id=name,
            output_path=f"docs/{name}.part-001.md",
        )
    )
    return f"docs/{name}.md"


def test_stale_twin_is_removed(env):
    out, db, idx = env
    twin = add_split_doc(out, db, "big", with_twin=True)
    assert _prune_stale_twins(db, idx, out) == 1
    assert not (out / twin).exists()


def test_transient_oserror_skips_that_path_and_carries_on(env, monkeypatch):
    out, db, idx = env
    bad = add_split_doc(out, db, "busy", with_twin=True)
    good = add_split_doc(out, db, "other", with_twin=True)

    real_is_file = Path.is_file

    def flaky(self, *a, **kw):
        if self.name == Path(bad).name:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        return real_is_file(self, *a, **kw)

    monkeypatch.setattr(Path, "is_file", flaky)

    removed = _prune_stale_twins(db, idx, out)  # must not raise

    assert removed == 1
    assert not (out / good).exists()  # the readable one was still pruned
    assert (out / bad).exists()  # the unreadable one is left for the next run

    monkeypatch.setattr(Path, "is_file", real_is_file)
    assert _prune_stale_twins(db, idx, out) == 1  # next run finishes the job
    assert not (out / bad).exists()
