"""Tests for progress display module."""

from __future__ import annotations

from workctx.progress import SyncProgress


def test_progress_quiet_mode() -> None:
    p = SyncProgress(quiet=True)
    with p.live():
        p.begin_discovery("test")
        p.end_discovery("test", total=10, skipped=2)
        p.advance("test", added=1)
        p.advance("test", updated=1)
        p.advance("test", failed=1)
        p.finish_source("test", "healthy")

    assert p._stats["test"]["total"] == 10
    assert p._stats["test"]["added"] == 1
    assert p._stats["test"]["updated"] == 1
    assert p._stats["test"]["failed"] == 1
    assert p._skipped["test"] == 2


def test_progress_multiple_sources() -> None:
    p = SyncProgress(quiet=True)
    with p.live():
        p.end_discovery("src1", total=5)
        p.end_discovery("src2", total=3)

        for _ in range(5):
            p.advance("src1", added=1)
        for _ in range(3):
            p.advance("src2", updated=1)

    assert p._stats["src1"]["done"] == 5
    assert p._stats["src2"]["done"] == 3
