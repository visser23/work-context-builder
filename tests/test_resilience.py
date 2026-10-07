"""Regression tests: failures must not be masked, and cloud-drive writes must retry.

Found while dogfooding the daemon logs:

* ``run_sync`` caught an unhandled exception (OneDrive ``EDEADLK`` while writing
  ``_meta/manifest.jsonl``) and set ``result.status = FAILED`` - but the daemon
  recomputed status from per-source results, logged "Sync healthy" and sent no
  alert.
* OneDrive's macOS File Provider intermittently fails opens/writes with
  ``EDEADLK`` (errno 11) / ``ETIMEDOUT`` (errno 60). These are transient.
"""

from __future__ import annotations

import errno
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from workctx import corpus
from workctx.models import RunStatus, SourceResult, SourceType, SyncResult


def _src(status: RunStatus) -> SourceResult:
    return SourceResult(source_name="a", source_type=SourceType.SHAREPOINT, status=status)


def _result(**kw) -> SyncResult:
    return SyncResult(run_id="r", started_at=datetime.now(UTC), **kw)


class TestAggregateStatusHonoursRunFailure:
    def test_run_level_failure_not_masked_by_healthy_sources(self):
        r = _result(
            status=RunStatus.FAILED,
            source_results=[_src(RunStatus.HEALTHY)],
        )
        assert r.aggregate_status() == RunStatus.FAILED

    def test_run_level_failure_with_no_source_results(self):
        assert _result(status=RunStatus.FAILED).aggregate_status() == RunStatus.FAILED

    def test_healthy_when_everything_healthy(self):
        r = _result(source_results=[_src(RunStatus.HEALTHY)])
        assert r.aggregate_status() == RunStatus.HEALTHY

    def test_degraded_source_still_degraded(self):
        r = _result(source_results=[_src(RunStatus.DEGRADED)])
        assert r.aggregate_status() == RunStatus.DEGRADED

    def test_failed_source_still_failed(self):
        r = _result(source_results=[_src(RunStatus.FAILED)])
        assert r.aggregate_status() == RunStatus.FAILED


class TestWriteTextAtomic:
    def test_writes_content(self, tmp_path: Path):
        target = tmp_path / "_meta" / "x.txt"
        corpus.write_text_atomic(target, "hello")
        assert target.read_text() == "hello"

    def test_overwrites_existing(self, tmp_path: Path):
        target = tmp_path / "x.txt"
        target.write_text("old")
        corpus.write_text_atomic(target, "new")
        assert target.read_text() == "new"

    @pytest.mark.parametrize("code", [errno.EDEADLK, errno.ETIMEDOUT, errno.EAGAIN])
    def test_retries_transient_cloud_errors_then_succeeds(self, tmp_path: Path, code: int):
        target = tmp_path / "x.txt"
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError(code, os.strerror(code))
            return real_replace(src, dst)

        with patch("workctx.corpus.os.replace", side_effect=flaky):
            corpus.write_text_atomic(target, "ok", retry_delay=0)
        assert target.read_text() == "ok"
        assert calls["n"] == 3

    def test_gives_up_after_attempts_and_raises(self, tmp_path: Path):
        target = tmp_path / "x.txt"
        err = OSError(errno.EDEADLK, "Resource deadlock avoided")
        with (
            patch("workctx.corpus.os.replace", side_effect=err) as rep,
            pytest.raises(OSError) as exc,
        ):
            corpus.write_text_atomic(target, "x", attempts=3, retry_delay=0)
        assert exc.value.errno == errno.EDEADLK
        assert rep.call_count == 3

    def test_does_not_retry_permanent_errors(self, tmp_path: Path):
        target = tmp_path / "x.txt"
        err = OSError(errno.ENOSPC, "No space left on device")
        with (
            patch("workctx.corpus.os.replace", side_effect=err) as rep,
            pytest.raises(OSError),
        ):
            corpus.write_text_atomic(target, "x", retry_delay=0)
        assert rep.call_count == 1

    def test_leaves_no_temp_files_after_failure(self, tmp_path: Path):
        target = tmp_path / "x.txt"
        with (
            patch("workctx.corpus.os.replace", side_effect=OSError(errno.EDEADLK, "d")),
            pytest.raises(OSError),
        ):
            corpus.write_text_atomic(target, "x", attempts=2, retry_delay=0)
        assert [p.name for p in tmp_path.iterdir()] == []


class TestManifestUsesResilientWrite:
    def test_manifest_written_despite_transient_error(self, tmp_path: Path):
        from workctx.state import StateDB

        db = StateDB(tmp_path / "state.sqlite")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(errno.EDEADLK, "Resource deadlock avoided")
            return real_replace(src, dst)

        out = tmp_path / "corpus"
        with (
            patch("workctx.corpus.os.replace", side_effect=flaky),
            patch("workctx.corpus.time.sleep"),
        ):
            corpus.generate_manifest(db, out)
        db.close()
        assert (out / "_meta" / "manifest.jsonl").exists()
        assert calls["n"] == 2

    def test_health_json_is_valid_after_write(self, tmp_path: Path):
        from workctx.state import StateDB

        db = StateDB(tmp_path / "state.sqlite")
        out = tmp_path / "corpus"
        corpus.generate_health(db, out, "healthy")
        db.close()
        data = json.loads((out / "_meta" / "health.json").read_text())
        assert data["status"] == "healthy"
