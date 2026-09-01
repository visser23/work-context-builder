"""File-based execution lock with stale lock detection."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STALE_LOCK_SECONDS = 3600  # 1 hour


class LockError(Exception):
    """Raised when a lock cannot be acquired."""


class ExecutionLock:
    """PID-based file lock to prevent overlapping sync executions."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._held = False

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        if self.lock_path.exists():
            lock_info = self._read_lock()
            if lock_info and self._is_stale(lock_info):
                logger.warning(
                    "Removing stale lock (pid=%s, started=%s)",
                    lock_info.get("pid"),
                    lock_info.get("started_at"),
                )
                self._remove()
            elif lock_info:
                raise LockError(
                    f"Another sync is running (pid={lock_info.get('pid')}, "
                    f"started={lock_info.get('started_at')}). "
                    f"If this is incorrect, delete {self.lock_path}"
                )

        self._write_lock()
        self._held = True

    def release(self) -> None:
        if self._held:
            self._remove()
            self._held = False

    def _write_lock(self) -> None:
        info = {
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
        }
        self.lock_path.write_text(json.dumps(info))

    def _read_lock(self) -> dict | None:
        try:
            return json.loads(self.lock_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _is_stale(self, lock_info: dict) -> bool:
        pid = lock_info.get("pid")
        if pid and not _pid_exists(pid):
            return True

        started_str = lock_info.get("started_at")
        if started_str:
            try:
                started = datetime.fromisoformat(started_str)
                age = (datetime.now(UTC) - started).total_seconds()
                if age > STALE_LOCK_SECONDS:
                    return True
            except ValueError:
                return True
        return False

    def _remove(self) -> None:
        with contextlib.suppress(OSError):
            self.lock_path.unlink(missing_ok=True)

    def __enter__(self) -> ExecutionLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
