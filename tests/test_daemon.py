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
