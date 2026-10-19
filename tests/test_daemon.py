"""Tests for daemon notification deduplication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from workctx.daemon import NOTIFICATION_DEDUP_HOURS, Daemon

PATCH_TARGET = "workctx.notifications.NotificationDispatcher"


def _make_daemon() -> Daemon:
    """Create a Daemon with a minimal mock config."""
    config = MagicMock()
    config.project.name = "test-project"
    config.project.id = "test"
    config.state_dir.mkdir = MagicMock()
    config.sources.sharepoint = []
    config.notifications.telegram.enabled = False
    return Daemon(config, "/tmp/test.yaml")


class TestNotificationDedup:
    def test_first_notification_sent(self):
        daemon = _make_daemon()
        with patch(PATCH_TARGET) as mock_cls:
            mock_dispatcher = MagicMock()
            mock_cls.return_value = mock_dispatcher
            daemon._notify("Sync failed\nDetails here")
            mock_dispatcher._dispatch.assert_called_once_with("Sync failed\nDetails here")

    def test_duplicate_suppressed(self):
        daemon = _make_daemon()
        with patch(PATCH_TARGET) as mock_cls:
            mock_dispatcher = MagicMock()
            mock_cls.return_value = mock_dispatcher
            daemon._notify("Sync failed\nRun: abc123")
            daemon._notify("Sync failed\nRun: def456")
            assert mock_dispatcher._dispatch.call_count == 1

    def test_different_messages_both_sent(self):
        daemon = _make_daemon()
        with patch(PATCH_TARGET) as mock_cls:
            mock_dispatcher = MagicMock()
            mock_cls.return_value = mock_dispatcher
            daemon._notify("Sync failed\nDetails")
            daemon._notify("Cookie expired\nDetails")
            assert mock_dispatcher._dispatch.call_count == 2

    def test_duplicate_sent_after_dedup_window(self):
        daemon = _make_daemon()
        with patch(PATCH_TARGET) as mock_cls:
            mock_dispatcher = MagicMock()
            mock_cls.return_value = mock_dispatcher

            daemon._notify("Sync failed\nRun: abc123")
            assert mock_dispatcher._dispatch.call_count == 1

            # Simulate the dedup window expiring
            past = datetime.now(UTC) - timedelta(hours=NOTIFICATION_DEDUP_HOURS + 1)
            daemon._notified_messages["Sync failed"] = past

            daemon._notify("Sync failed\nRun: def456")
            assert mock_dispatcher._dispatch.call_count == 2

    def test_force_bypasses_dedup(self):
        daemon = _make_daemon()
        with patch(PATCH_TARGET) as mock_cls:
            mock_dispatcher = MagicMock()
            mock_cls.return_value = mock_dispatcher
            daemon._notify("Daemon started\nProject: test", force=True)
            daemon._notify("Daemon started\nProject: test", force=True)
            assert mock_dispatcher._dispatch.call_count == 2


class TestRetryBackoff:
    """A failed run must not be retried every 30 minutes forever."""

    def test_delay_grows_and_is_capped(self):
        from workctx.daemon import MAX_RETRY_DELAY_MINUTES, MIN_RETRY_DELAY_MINUTES, retry_delay

        assert retry_delay(0) == MIN_RETRY_DELAY_MINUTES
        assert retry_delay(1) == MIN_RETRY_DELAY_MINUTES
        assert retry_delay(2) == 2 * MIN_RETRY_DELAY_MINUTES
        assert retry_delay(3) == 4 * MIN_RETRY_DELAY_MINUTES
        assert retry_delay(50) == MAX_RETRY_DELAY_MINUTES

    @staticmethod
    def _due_daemon(failures: int, minutes_since_attempt: float):
        daemon = _make_daemon()
        daemon.config.all_source_names.return_value = ["src"]
        daemon._consecutive_failures = failures
        daemon._last_sync_attempt = datetime.now(UTC) - timedelta(minutes=minutes_since_attempt)
        daemon.trigger_sync = MagicMock()
        stale = MagicMock()
        stale.last_success = datetime.now(UTC) - timedelta(hours=30)
        return daemon, stale

    def _run_check(self, failures: int, minutes_since_attempt: float) -> bool:
        daemon, stale = self._due_daemon(failures, minutes_since_attempt)
        with patch("workctx.daemon.StateDB") as db_cls:
            db_cls.return_value.get_checkpoint.return_value = stale
            daemon._check_and_sync()
        return daemon.trigger_sync.called

    def test_first_retry_after_the_minimum_delay(self):
        assert self._run_check(failures=1, minutes_since_attempt=29) is False
        assert self._run_check(failures=1, minutes_since_attempt=31) is True

    def test_repeated_failures_wait_longer(self):
        assert self._run_check(failures=3, minutes_since_attempt=61) is False
        assert self._run_check(failures=3, minutes_since_attempt=121) is True

    def test_failed_run_counts_and_healthy_run_resets(self):
        daemon = _make_daemon()
        daemon.config.state_dir = MagicMock()
        failed = MagicMock()
        failed.aggregate_status.return_value.value = "failed"
        failed.source_results = []
        healthy = MagicMock()
        healthy.aggregate_status.return_value.value = "healthy"
        healthy.source_results = []

        with (
            patch("workctx.daemon.setup_logging"),
            patch("workctx.sync.run_sync", side_effect=[failed, failed, healthy]),
            patch.object(daemon, "_notify"),
        ):
            daemon._do_sync()
            daemon._do_sync()
            assert daemon._consecutive_failures == 2
            daemon._do_sync()
            assert daemon._consecutive_failures == 0

    def test_crashed_run_counts_as_a_failure(self):
        daemon = _make_daemon()
        with (
            patch("workctx.daemon.setup_logging"),
            patch("workctx.sync.run_sync", side_effect=RuntimeError("boom")),
            patch.object(daemon, "_notify"),
        ):
            daemon._do_sync()
        assert daemon._consecutive_failures == 1
