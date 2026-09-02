"""Notification dispatch: Telegram Bot API and macOS Notification Center."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime

import httpx

from workctx.config import ProjectConfig
from workctx.secrets import get_secret

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
SUPPRESSION_FILE = "notification_state.json"
REMINDER_HOURS = 24


class NotificationDispatcher:
    """Manages alert delivery with suppression for repeated failures."""

    def __init__(self, config: ProjectConfig) -> None:
        self.config = config
        self._state_path = config.state_dir / SUPPRESSION_FILE
        self._state = self._load_state()

    def send_failure(
        self,
        source_name: str,
        stage: str,
        error: str,
        run_id: str,
        last_success: str | None = None,
    ) -> None:
        """Send a failure notification (with suppression for repeats)."""
        key = f"fail:{source_name}:{error[:100]}"
        if self._is_suppressed(key):
            logger.debug("Notification suppressed: %s", key)
            return

        log_dir = self.config.state_dir / "logs"
        log_file = log_dir / f"{run_id}.log"

        message = (
            f"⚠️ Work Context Mirror\n\n"
            f"Project: {self.config.project.name}\n"
            f"Source: {source_name}\n"
            f"Stage: {stage}\n\n"
            f"Error:\n{error[:500]}\n\n"
            f"Last successful sync:\n{last_success or 'Never'}\n\n"
            f"Run: {run_id}\n"
            f"Log: {log_file}"
        )

        self._dispatch(message)
        self._record_sent(key)

    def send_recovery(self, source_name: str, previous_error: str | None = None) -> None:
        """Send a recovery notification."""
        now = datetime.now(UTC).strftime("%d %b %Y %H:%M")
        message = (
            f"✅ Work Context Mirror recovered\n\n"
            f"Project: {self.config.project.name}\n"
            f"Source: {source_name}\n\n"
            f"Sync completed successfully.\n"
        )
        if previous_error:
            message += f"\nPrevious failure:\n{previous_error[:200]}\n"
        message += f"\nRecovered: {now}"

        self._dispatch(message)

        for key in list(self._state.keys()):
            if key.startswith(f"fail:{source_name}:"):
                del self._state[key]
        self._save_state()

    def send_degraded(self, summary: str, run_id: str) -> None:
        """Send a degraded-run notification."""
        message = (
            f"⚠️ Work Context Mirror — Degraded\n\n"
            f"Project: {self.config.project.name}\n"
            f"Run: {run_id}\n\n"
            f"{summary}"
        )
        self._dispatch(message)

    def _dispatch(self, message: str) -> None:
        """Send via all configured channels."""
        if self.config.notifications.telegram.enabled:
            self._send_telegram(message)
        if self.config.notifications.macos.enabled:
            self._send_macos(message)

    def _send_telegram(self, message: str) -> None:
        tg = self.config.notifications.telegram
        bot_token = get_secret(tg.bot_token_ref or "")
        chat_id = get_secret(tg.chat_id_ref or "")

        if not bot_token or not chat_id:
            logger.warning("Telegram not configured (missing bot token or chat ID)")
            return

        try:
            resp = httpx.post(
                f"{TELEGRAM_API}/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": ""},
                timeout=15.0,
            )
            if resp.status_code != 200:
                logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
            else:
                logger.info("Telegram notification sent")
        except Exception:
            logger.error("Telegram send failed", exc_info=True)

    def _send_macos(self, message: str) -> None:
        """Send via macOS Notification Center using osascript."""
        title = "Work Context Mirror"
        short_msg = message[:200].replace('"', "'").replace("\n", " ")
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{short_msg}" with title "{title}"',
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            logger.debug("macOS notification failed", exc_info=True)

    def _is_suppressed(self, key: str) -> bool:
        if key not in self._state:
            return False
        last_sent = self._state[key].get("last_sent")
        if not last_sent:
            return False
        try:
            dt = datetime.fromisoformat(last_sent)
            hours = (datetime.now(UTC) - dt).total_seconds() / 3600
            return hours < REMINDER_HOURS
        except ValueError:
            return False

    def _record_sent(self, key: str) -> None:
        self._state[key] = {
            "last_sent": datetime.now(UTC).isoformat(),
        }
        self._save_state()

    def _load_state(self) -> dict:
        if self._state_path.exists():
            try:
                return json.loads(self._state_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(self._state, indent=2))
