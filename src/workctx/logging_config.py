"""Logging configuration with rotating file handler."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SecretFilter(logging.Filter):
    """Filter to prevent secrets from appearing in logs."""

    REDACT_PATTERNS = (
        "authorization",
        "token",
        "password",
        "secret",
        "cookie",
        "session",
        "api_key",
        "apikey",
        "refresh_token",
        "bot_token",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        msg_lower = msg.lower()
        for pattern in self.REDACT_PATTERNS:
            if pattern in msg_lower and ("=" in msg or "/bot" in msg_lower):
                record.msg = f"[REDACTED - contained {pattern}]"
                record.args = None
                break
        return True


def setup_logging(
    state_dir: Path,
    run_id: str,
    *,
    verbose: bool = False,
) -> Path:
    """Configure logging for a sync run.

    Returns the log file path.
    """
    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"{run_id}.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=30,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    fh.addFilter(SecretFilter())
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.DEBUG if verbose else logging.WARNING)
    ch.setFormatter(fmt)
    ch.addFilter(SecretFilter())
    root.addHandler(ch)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return log_file


def generate_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
