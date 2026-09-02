"""Background daemon: daily sync + Telegram command polling.

Designed to run as a persistent background service managed by
launchd (macOS), systemd (Linux), or Task Scheduler (Windows).

On each loop iteration:
  1. Check if a daily sync is due (last success > 24h ago)
  2. Poll Telegram for inbound commands (/sync, /status, /help)
  3. Sleep and repeat
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import UTC, datetime

from workctx.config import ProjectConfig, load_config
from workctx.logging_config import generate_run_id, setup_logging
from workctx.state import StateDB

logger = logging.getLogger(__name__)

SYNC_INTERVAL_HOURS = 24
POLL_INTERVAL_SECONDS = 10
LOOP_SLEEP_SECONDS = 30
COOKIE_KEEPALIVE_HOURS = 4


class Daemon:
    """Main daemon loop for Work Context Mirror."""

    def __init__(self, config: ProjectConfig, config_path: str) -> None:
        self.config = config
        self.config_path = config_path
        self._running = False
        self._syncing = False
        self._sync_lock = threading.Lock()
        self._last_sync_attempt: datetime | None = None
        self._last_cookie_keepalive: datetime | None = None

    def run(self) -> None:
        """Run the daemon loop until SIGTERM/SIGINT."""
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        logger.info("Daemon started for project '%s'", self.config.project.name)
        self._notify(f"Work Context Mirror daemon started\nProject: {self.config.project.name}")

        tg_poller = None
        if self.config.notifications.telegram.enabled:
            tg_poller = TelegramPoller(self.config, self)
            tg_poller.start()

        try:
            while self._running:
                self._check_and_sync()
                self._check_cookie_keepalive()
                time.sleep(LOOP_SLEEP_SECONDS)
        finally:
            if tg_poller:
                tg_poller.stop()
            logger.info("Daemon stopped")

    def trigger_sync(self, *, full: bool = False, source: str = "schedule") -> str:
        """Trigger a sync run. Returns a status message."""
        with self._sync_lock:
            if self._syncing:
                return "Sync already in progress."
            self._syncing = True

        try:
            return self._do_sync(full=full, source=source)
        finally:
            self._syncing = False

    def get_status(self) -> str:
        """Return a human-readable status string."""
        lines = [
            f"Project: {self.config.project.name}",
            f"Syncing: {'yes' if self._syncing else 'no'}",
        ]

        try:
            db = StateDB(self.config.state_dir / "state.sqlite")
            for name in self.config.all_source_names():
                cp = db.get_checkpoint(name)
                count = db.count_objects(name)
                last = (
                    cp.last_success.strftime("%d %b %H:%M") if cp and cp.last_success else "never"
                )
                lines.append(f"  {name}: {count:,} objects, last sync {last}")
            db.close()
        except Exception as e:
            lines.append(f"  DB error: {e}")

        return "\n".join(lines)

    def _check_and_sync(self) -> None:
        """Check if daily sync is due and run if needed."""
        if self._syncing:
            return

        try:
            db = StateDB(self.config.state_dir / "state.sqlite")
            latest_success: datetime | None = None

            for name in self.config.all_source_names():
                cp = db.get_checkpoint(name)
                if (
                    cp
                    and cp.last_success
                    and (latest_success is None or cp.last_success > latest_success)
                ):
                    latest_success = cp.last_success
            db.close()
        except Exception:
            latest_success = None

        now = datetime.now(UTC)

        if latest_success:
            hours_since = (now - latest_success).total_seconds() / 3600
            if hours_since < SYNC_INTERVAL_HOURS:
                return

        if self._last_sync_attempt:
            mins_since_attempt = (now - self._last_sync_attempt).total_seconds() / 60
            if mins_since_attempt < 30:
                return

        logger.info("Daily sync is due — triggering")
        self.trigger_sync(source="daily-check")

    def _check_cookie_keepalive(self) -> None:
        """Ping SharePoint periodically to keep session cookies alive."""
        browser_sources = [sp for sp in self.config.sources.sharepoint if sp.mode == "browser"]
        if not browser_sources:
            return

        now = datetime.now(UTC)
        if self._last_cookie_keepalive:
            hours = (now - self._last_cookie_keepalive).total_seconds() / 3600
            if hours < COOKIE_KEEPALIVE_HOURS:
                return

        from workctx.auth.sharepoint import http_keepalive, load_cookie_blob

        self._last_cookie_keepalive = now

        for sp in browser_sources:
            secret_ref = sp.auth.secret_ref if sp.auth else None
            if not secret_ref:
                continue

            blob = load_cookie_blob(secret_ref)
            if not blob:
                logger.warning("No stored cookies for %s — skipping keepalive", sp.name)
                continue

            site_url = blob["site_url"]
            cookies = blob["cookies"]

            if http_keepalive(site_url, cookies):
                logger.debug("Cookie keepalive OK for %s", sp.name)
            else:
                msg = (
                    f"SharePoint session expired for '{sp.name}'.\n"
                    f"Run: workctx auth login-sharepoint --source {sp.name}"
                )
                logger.warning(msg)
                self._notify(msg)

    def _do_sync(self, *, full: bool = False, source: str = "schedule") -> str:
        """Execute a sync and return a result summary."""
        from workctx.sync import run_sync

        self._last_sync_attempt = datetime.now(UTC)
        run_id = generate_run_id()
        setup_logging(self.config.state_dir, run_id, verbose=False)

        logger.info("Sync started (run=%s, source=%s, full=%s)", run_id, source, full)

        try:
            result = run_sync(self.config, run_id=run_id, full=full, quiet=True)
        except Exception as e:
            msg = f"Sync failed: {e}"
            logger.error(msg, exc_info=True)
            self._notify(f"Sync FAILED\nRun: {run_id}\nError: {e}")
            return msg

        overall = result.aggregate_status()
        parts = []
        for sr in result.source_results:
            parts.append(
                f"  {sr.source_name}: {sr.status.value} "
                f"(+{sr.objects_added} ~{sr.objects_updated} "
                f"-{sr.objects_deleted} !{sr.objects_failed})"
            )

        summary = "\n".join(parts)
        msg = f"Sync {overall.value}\nRun: {run_id}\n{summary}"
        logger.info("Sync completed: %s", overall.value)

        if overall.value != "healthy":
            self._notify(msg)
        else:
            logger.info("Sync healthy — no notification needed")

        return msg

    def _notify(self, message: str) -> None:
        """Send a notification via configured channels."""
        from workctx.notifications import NotificationDispatcher

        try:
            dispatcher = NotificationDispatcher(self.config)
            dispatcher._dispatch(message)
        except Exception:
            logger.error("Notification failed", exc_info=True)

    def _handle_signal(self, signum: int, _frame: object) -> None:
        logger.info("Received signal %d, shutting down", signum)
        self._running = False


class TelegramPoller:
    """Polls Telegram getUpdates for inbound commands in a background thread."""

    def __init__(self, config: ProjectConfig, daemon: Daemon) -> None:
        self.config = config
        self.daemon = daemon
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_update_id = 0

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Telegram command poller started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=15)

    def _poll_loop(self) -> None:
        import httpx

        from workctx.secrets import get_secret

        tg = self.config.notifications.telegram
        bot_token = get_secret(tg.bot_token_ref or "")
        chat_id = get_secret(tg.chat_id_ref or "")

        if not bot_token or not chat_id:
            logger.warning("Telegram not configured — poller exiting")
            return

        while self._running:
            try:
                resp = httpx.get(
                    f"https://api.telegram.org/bot{bot_token}/getUpdates",
                    params={
                        "offset": self._last_update_id + 1,
                        "timeout": POLL_INTERVAL_SECONDS,
                        "allowed_updates": '["message"]',
                    },
                    timeout=POLL_INTERVAL_SECONDS + 5,
                )
                if resp.status_code != 200:
                    logger.warning("Telegram getUpdates: HTTP %d", resp.status_code)
                    time.sleep(POLL_INTERVAL_SECONDS)
                    continue

                data = resp.json()
                for update in data.get("result", []):
                    self._last_update_id = update["update_id"]
                    self._handle_update(update, bot_token, chat_id)

            except Exception:
                logger.debug("Telegram poll error", exc_info=True)
                time.sleep(POLL_INTERVAL_SECONDS)

    def _handle_update(self, update: dict, bot_token: str, chat_id: str) -> None:
        msg = update.get("message", {})
        text = (msg.get("text") or "").strip().lower()
        from_chat = str(msg.get("chat", {}).get("id", ""))

        if from_chat != chat_id:
            return

        if text == "/sync":
            self._reply(bot_token, chat_id, "Starting sync...")
            result = self.daemon.trigger_sync(source="telegram")
            self._reply(bot_token, chat_id, result)

        elif text == "/syncfull":
            self._reply(bot_token, chat_id, "Starting full sync...")
            result = self.daemon.trigger_sync(full=True, source="telegram")
            self._reply(bot_token, chat_id, result)

        elif text == "/status":
            status = self.daemon.get_status()
            self._reply(bot_token, chat_id, status)

        elif text in ("/help", "/start"):
            self._reply(
                bot_token,
                chat_id,
                "Work Context Mirror commands:\n"
                "/sync — trigger incremental sync\n"
                "/syncfull — trigger full sync\n"
                "/status — show sync status\n"
                "/help — this message",
            )

    def _reply(self, bot_token: str, chat_id: str, text: str) -> None:
        import httpx

        try:
            httpx.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=15.0,
            )
        except Exception:
            logger.debug("Telegram reply failed", exc_info=True)


def run_daemon(config_path: str) -> None:
    """Entry point: load config and start the daemon loop."""
    config = load_config(config_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)

    run_id = generate_run_id()
    setup_logging(config.state_dir, run_id, verbose=False)

    daemon = Daemon(config, config_path)
    daemon.run()
