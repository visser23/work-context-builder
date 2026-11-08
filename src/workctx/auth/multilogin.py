"""Sign in to several web apps in ONE browser window, one tab per lapsed login.

``workctx auth relogin`` first checks every login silently, then hands the ones that
really need a human to :func:`run_login_tabs`: a single visible browser (per browser
profile) with a tab for each. The terminal reports each login as it completes, finished
tabs are closed, and the whole browser closes when the last one is done (or the timeout
passes, or the user closes the window).

Each login is a :class:`LoginTask` whose ``step(page, context)`` returns a non-``None``
value once that tab holds a session the service accepts - the same step functions the
single-login flows use - and an optional ``on_success`` that stores the result.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# How often (seconds) the terminal repeats "still waiting for ...".
STATUS_EVERY_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 300.0


@dataclass
class LoginTask:
    """One login to complete in its own tab."""

    name: str
    kind: str  # "sharepoint" | "outlook" | "slack": for reporting and tests
    url: str
    step: Callable[[Any, Any], Any | None]
    on_success: Callable[[Any], None] | None = None


@contextlib.contextmanager
def _browser_session(profile: str) -> Iterator[Any]:
    """A visible persistent browser context on ``profile`` (tests replace this)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright not installed. Run: uv sync --extra playwright && "
            "uv run playwright install chromium"
        ) from exc
    from workctx.auth.sharepoint import _BROWSER_LOCK, get_profile_dir
    from workctx.auth.webtokens import _launch_context
    from workctx.browsers import configure_browsers_path, ensure_chromium

    configure_browsers_path()  # before the driver starts: never run from a cache dir
    with _BROWSER_LOCK, sync_playwright() as p:
        ensure_chromium(p)
        context = _launch_context(p, get_profile_dir(profile), False)
        try:
            yield context
        finally:
            context.close()


def _is_closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return True


def _bring_first_to_front(pages: dict[str, Any], pending: dict[str, LoginTask]) -> None:
    """Show the next login that still needs attention."""
    for name in pending:
        with contextlib.suppress(Exception):
            pages[name].bring_to_front()
        return


def run_login_tabs(
    profile: str,
    tasks: list[LoginTask],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_seconds: float = 2.0,
    report: Callable[[str], None] = print,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, bool]:
    """Open ``tasks`` as tabs of one browser on ``profile`` and wait for all to complete.

    Returns ``{task name: signed in?}``. The browser is always closed on the way out.
    """
    results = {t.name: False for t in tasks}
    if not tasks:
        return results
    total = len(tasks)
    done = 0

    with _browser_session(profile) as context:
        pages: dict[str, Any] = {}
        for t in tasks:
            page = context.new_page()
            with contextlib.suppress(Exception):  # a slow page is fine: we poll anyway
                page.goto(t.url, wait_until="domcontentloaded", timeout=60_000)
            pages[t.name] = page
        pending = {t.name: t for t in tasks}
        report(f"Opened {total} tab{'s' if total != 1 else ''}; sign in to each. Waiting...")
        _bring_first_to_front(pages, pending)

        deadline = clock() + timeout_seconds
        last_status = clock()
        while pending:
            for name in list(pending):
                task, page = pending[name], pages[name]
                if _is_closed(page):
                    report(f"  ✘ {name}: tab closed before sign-in finished")
                    del pending[name]
                    continue
                try:
                    result = task.step(page, context)
                except Exception as exc:  # page navigating, element not there yet ...
                    logger.debug("Login step for %s failed: %s", name, exc)
                    result = None
                if result is None:
                    continue
                del pending[name]
                try:
                    if task.on_success:
                        task.on_success(result)
                except Exception as exc:
                    report(f"  ✘ {name}: signed in but saving the login failed: {exc}")
                else:
                    done += 1
                    results[name] = True
                    report(f"  ✔ {name} signed in ({done}/{total})")
                with contextlib.suppress(Exception):
                    page.close()
                _bring_first_to_front(pages, pending)
            if not pending:
                break
            if clock() >= deadline:
                left = ", ".join(pending)
                report(f"  ✘ Timed out waiting for: {left}")
                break
            if clock() - last_status >= STATUS_EVERY_SECONDS:
                last_status = clock()
                left = ", ".join(pending)
                report(f"  ... Waiting for: {left} ({max(0, int(deadline - clock()))}s left)")
            try:
                pages[next(iter(pending))].wait_for_timeout(int(poll_seconds * 1000))
            except Exception as exc:  # the user closed the browser
                logger.debug("Browser went away: %s", exc)
                report(f"  ✘ Browser closed before: {', '.join(pending)}")
                break
    return results
