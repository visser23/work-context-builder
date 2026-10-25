"""Where Playwright's Chromium lives, and making sure it is actually there.

Playwright downloads its browser to an OS *cache* directory by default
(``~/Library/Caches/ms-playwright`` on macOS, ``~/.cache/ms-playwright`` on Linux).
Cache cleaners and "free up space" tools delete those, which silently breaks every
SharePoint / Outlook / Slack session refresh ("Executable doesn't exist ...").

workctx therefore pins ``PLAYWRIGHT_BROWSERS_PATH`` to a persistent directory next to
the browser profiles and, if the browser is missing at launch time, re-installs it.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENV_VAR = "PLAYWRIGHT_BROWSERS_PATH"

# Path components that mark a directory as disposable cache storage.
_CACHE_PARTS = frozenset({"Caches", ".cache"})

INSTALL_TIMEOUT_SECONDS = 900


def app_data_dir() -> Path:
    """Platform-specific, persistent (non-cache) application data directory."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "WorkContextMirror"
    if system == "Windows":
        return Path.home() / "AppData" / "Local" / "WorkContextMirror"
    return Path.home() / ".local" / "share" / "workctx"


def browsers_dir() -> Path:
    """Persistent directory that holds Playwright's downloaded browsers."""
    return app_data_dir() / "ms-playwright"


def is_cache_path(path: Path) -> bool:
    """True if ``path`` sits inside an OS cache directory (``Caches`` / ``.cache``)."""
    return any(part in _CACHE_PARTS for part in path.parts)


def configure_browsers_path() -> Path:
    """Point Playwright at a persistent browser directory; return the effective path.

    Must run before ``sync_playwright()`` starts (the driver copies ``os.environ`` then).
    An explicit ``PLAYWRIGHT_BROWSERS_PATH`` is respected unless it points into a cache
    directory, in which case it is replaced (with a warning). ``0`` (browsers inside the
    Playwright package, i.e. the venv) is also respected.
    """
    current = os.environ.get(ENV_VAR)
    if current == "0":
        return Path(current)
    if current:
        candidate = Path(current).expanduser()
        if not is_cache_path(candidate):
            return candidate
        logger.warning(
            "%s=%s is a cache directory (cleaners delete it) — using %s instead",
            ENV_VAR,
            current,
            browsers_dir(),
        )
    target = browsers_dir()
    os.environ[ENV_VAR] = str(target)
    return target


def playwright_env() -> dict[str, str]:
    """Environment for a ``playwright`` subprocess, with the persistent browser path."""
    configure_browsers_path()
    return dict(os.environ)


def install_chromium() -> None:
    """Download Chromium into the persistent browser directory."""
    target = configure_browsers_path()
    logger.info("Installing Playwright chromium into %s", target)
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
        env=playwright_env(),
        timeout=INSTALL_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise RuntimeError(f"playwright install chromium failed: {detail}")
    logger.info("Playwright chromium installed")


def chromium_executable() -> Path | None:
    """Path of the Chromium Playwright would launch (it may not exist), or ``None``."""
    configure_browsers_path()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = p.chromium.executable_path
    except Exception:
        logger.debug("Could not determine the Playwright chromium path", exc_info=True)
        return None
    return Path(exe) if isinstance(exe, str) and exe else None


def ensure_chromium(pw: Any) -> None:
    """Make sure the Chromium Playwright is about to launch exists; re-install if not.

    ``pw`` is the object yielded by ``sync_playwright()``. Anything that does not expose
    a real ``chromium.executable_path`` string (test doubles, other versions) is skipped.
    """
    exe = getattr(getattr(pw, "chromium", None), "executable_path", None)
    if not isinstance(exe, str) or not exe or Path(exe).exists():
        return
    logger.warning("Chromium missing at %s — reinstalling", exe)
    install_chromium()
    if not Path(exe).exists():
        raise RuntimeError(
            f"Chromium still missing at {exe} after install. "
            "Run: playwright install chromium"
        )
