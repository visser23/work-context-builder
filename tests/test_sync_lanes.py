"""Worker lanes: slow, CPU-heavy sources must not starve fast ones.

Regression (Nov 2026): every source shared one FIFO pool of 4 workers. SharePoint queued
~4,000 slow PDF/Office conversions first (one PDF can take 2+ minutes), so ~26,000 quick
Jira/mail/Slack/calendar/Teams writes sat behind them for hours although the machine had
nothing else to do for them.
"""

from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from workctx import sync as sync_mod
from workctx.models import SourceType
from workctx.sources.base import Source
from workctx.sources.confluence import ConfluenceAdapter
from workctx.sources.jira import JiraAdapter
from workctx.sources.local_folder import LocalFolderAdapter
from workctx.sources.outlook import CalendarAdapter, MailAdapter
from workctx.sources.sharepoint import SharePointLocalSource
from workctx.sources.sharepoint_web import SharePointWebSource
from workctx.sources.slack import SlackAdapter
from workctx.sources.teams_transcripts import TeamsTranscriptSource


class Stub:
    def __init__(self, name: str, heavy: bool) -> None:
        self.name = name
        self.heavy_conversion = heavy
        self.source_type = SourceType.JIRA

    def close(self) -> None:
        pass


class TestWhoIsHeavy:
    @pytest.mark.parametrize("cls", [SharePointWebSource, SharePointLocalSource])
    def test_document_converting_sources_get_the_lane(self, cls):
        assert cls.heavy_conversion is True

    @pytest.mark.parametrize(
        "cls",
        [
            JiraAdapter,
            ConfluenceAdapter,
            SlackAdapter,
            MailAdapter,
            CalendarAdapter,
            TeamsTranscriptSource,
            LocalFolderAdapter,
        ],
    )
    def test_text_sources_stay_on_the_shared_pool(self, cls):
        assert cls.heavy_conversion is False

    def test_default_is_not_heavy(self):
        assert Source.heavy_conversion is False


class TestPickPool:
    def setup_method(self):
        self.shared = ThreadPoolExecutor(1, thread_name_prefix="shared")
        self.heavy = ThreadPoolExecutor(1, thread_name_prefix="heavy")

    def teardown_method(self):
        self.shared.shutdown()
        self.heavy.shutdown()

    def test_heavy_source_uses_its_own_lane_next_to_fast_ones(self):
        sources = [Stub("sp", True), Stub("jira", False)]
        pool = sync_mod._pick_pool(sources[0], sources, self.shared, self.heavy)
        assert pool is self.heavy

    def test_fast_source_always_uses_the_shared_pool(self):
        sources = [Stub("sp", True), Stub("jira", False)]
        assert sync_mod._pick_pool(sources[1], sources, self.shared, self.heavy) is self.shared

    def test_a_lone_heavy_source_keeps_every_worker(self):
        sources = [Stub("sp", True), Stub("sp2", True)]
        assert sync_mod._pick_pool(sources[0], sources, self.shared, self.heavy) is self.shared

    def test_sources_without_the_flag_count_as_fast(self):
        class Plain:
            name = "x"

        assert sync_mod._pick_pool(Plain(), [Plain()], self.shared, self.heavy) is self.shared

    @pytest.mark.parametrize(("workers", "lane"), [(1, 1), (2, 1), (4, 2), (8, 2), (16, 2)])
    def test_lane_is_half_the_workers_capped_at_two(self, workers, lane):
        assert sync_mod._lane_size(workers) == lane


class TestRunSyncUsesTheLanes:
    def test_each_source_is_handed_its_pool(self, sp_project, monkeypatch):
        sources = [Stub("sp", True), Stub("jira", False)]
        monkeypatch.setattr(sync_mod, "_build_sources", lambda cfg: sources)
        seen: dict[str, str] = {}

        def fake_sync_source(source, config, db, idx, output_root, **kw):
            seen[source.name] = kw["worker_pool"]._thread_name_prefix
            from workctx.models import SourceResult

            return SourceResult(source_name=source.name, source_type=SourceType.JIRA)

        monkeypatch.setattr(sync_mod, "_sync_source", fake_sync_source)

        with contextlib.suppress(Exception):  # post-run reporting is not under test
            sync_mod.run_sync(sp_project, run_id="lanes")

        assert seen == {"sp": "heavy", "jira": "worker"}
