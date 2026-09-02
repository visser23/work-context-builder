"""SharePoint cookie capture and keep-alive via Playwright.

Supports two flows:
1. Interactive login: opens a browser, user authenticates, cookies extracted.
2. Keep-alive: opens browser with persisted profile, navigates to SP site to
   refresh session cookies, extracts fresh rtFa/FedAuth.
"""

from __future__ import annotations

import json
import logging
import platform
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from workctx.secrets import get_secret, set_secret

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

logger = logging.getLogger(__name__)

_CHROMIUM_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

SP_COOKIE_NAMES = {"rtFa", "FedAuth"}


def _profiles_dir() -> Path:
    """Platform-specific browser profile storage."""
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "WorkContextMirror"
    elif system == "Windows":
        base = Path.home() / "AppData" / "Local" / "WorkContextMirror"
    else:
        base = Path.home() / ".local" / "share" / "workctx"
    return base / "browser-profiles"


def get_profile_dir(source_name: str) -> Path:
    d = _profiles_dir() / source_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def interactive_login(
    site_url: str,
    source_name: str,
    secret_ref: str,
    *,
    headless: bool = False,
    timeout_seconds: int = 300,
    poll_interval: float = 2.0,
) -> dict[str, str]:
    """Open browser for user to authenticate, then extract SP cookies.

    Polls for rtFa/FedAuth cookies every poll_interval seconds. Once
    they appear (meaning SSO completed), captures them automatically.
    Falls back to waiting for Enter if stdin is available.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright not installed. Run: uv pip install playwright && "
            "playwright install chromium"
        ) from exc

    profile_dir = get_profile_dir(source_name)
    cookies: dict[str, str] = {}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            channel="chromium",
            user_agent=_CHROMIUM_UA,
            accept_downloads=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = context.new_page()
        page.goto(site_url, wait_until="networkidle", timeout=120_000)

        logger.info("Browser opened at %s — polling for auth cookies", site_url)
        print(
            "\n  Authenticate in the browser window. "
            "Cookies will be captured automatically...\n"
        )

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            cookies = _extract_sp_cookies(context, site_url)
            if "rtFa" in cookies and "FedAuth" in cookies:
                logger.info("Authentication cookies detected")
                break

            final_url = page.url.lower()
            if not _is_login_redirect(final_url) and ".sharepoint.com" in final_url:
                cookies = _extract_sp_cookies(context, site_url)
                if cookies:
                    break

            time.sleep(poll_interval)

        if not cookies or "rtFa" not in cookies:
            cookies = _extract_sp_cookies(context, site_url)

        context.close()

    if not cookies:
        raise RuntimeError(
            "No SharePoint session cookies found after login. "
            "Ensure you completed authentication."
        )

    _persist_cookies(secret_ref, cookies, site_url)
    logger.info("SharePoint cookies captured and stored for %s", source_name)
    return cookies


def keepalive_and_extract(
    site_url: str,
    source_name: str,
    secret_ref: str,
    *,
    timeout_ms: int = 60_000,
) -> dict[str, str]:
    """Open browser with persisted profile to refresh session cookies.

    Uses the persistent profile from the last interactive login. Navigates
    to the SP site with ``domcontentloaded`` (not ``networkidle`` — SharePoint
    fires endless background requests that make ``networkidle`` unreliable).

    Extracts cookies even if navigation times out, since they're set in the
    initial HTTP response headers before the page finishes loading.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright not installed. Run: uv pip install playwright && "
            "playwright install chromium"
        ) from exc

    profile_dir = get_profile_dir(source_name)
    if not (profile_dir / "Default").exists() and not any(profile_dir.iterdir()):
        raise SessionExpiredError(
            f"No browser profile found for '{source_name}'. "
            f"Run: workctx auth login-sharepoint --source {source_name}"
        )

    cookies: dict[str, str] = {}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            channel="chromium",
            user_agent=_CHROMIUM_UA,
            accept_downloads=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        page = context.new_page()

        try:
            page.goto(site_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            logger.debug(
                "Navigation timeout for %s — extracting cookies anyway",
                source_name,
            )

        cookies = _extract_sp_cookies(context, site_url)

        if not cookies:
            final_url = page.url.lower()
            if _is_login_redirect(final_url):
                context.close()
                raise SessionExpiredError(
                    f"Session expired — redirected to login for '{source_name}'. "
                    f"Run: workctx auth login-sharepoint --source {source_name}"
                )

        context.close()

    if not cookies:
        raise SessionExpiredError(
            f"No session cookies after keep-alive for '{source_name}'. "
            f"Run: workctx auth login-sharepoint --source {source_name}"
        )

    _persist_cookies(secret_ref, cookies, site_url)
    logger.info("SharePoint cookies refreshed for %s", source_name)
    return cookies


def load_cookies(secret_ref: str) -> dict[str, str] | None:
    """Load previously captured cookies from the OS credential store."""
    raw = get_secret(secret_ref)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        cookies = data.get("cookies", {})
        if "rtFa" in cookies and "FedAuth" in cookies:
            return cookies
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def load_cookie_blob(secret_ref: str) -> dict[str, Any] | None:
    """Load the full cookie blob (cookies + site_url) from credential store."""
    raw = get_secret(secret_ref)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if data.get("cookies") and data.get("site_url"):
            return data
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def http_keepalive(site_url: str, cookies: dict[str, str]) -> bool:
    """Lightweight HTTP hit to SharePoint to keep the session alive.

    Makes a single GET to /_api/web/title. SharePoint refreshes the session
    internally on any authenticated request, so this prevents cookie expiry
    without needing a browser.

    Returns True if the session is still valid, False if expired.
    """
    import httpx

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    try:
        resp = httpx.get(
            f"{site_url.rstrip('/')}/_api/web/title",
            headers={
                "Cookie": cookie_header,
                "Accept": "application/json;odata=verbose",
                "User-Agent": _CHROMIUM_UA,
            },
            timeout=30,
            follow_redirects=False,
        )
        if resp.status_code == 200:
            logger.debug("Cookie keepalive OK for %s", site_url)
            return True
        logger.info("Cookie keepalive failed for %s: HTTP %d", site_url, resp.status_code)
        return False
    except Exception as e:
        logger.info("Cookie keepalive error for %s: %s", site_url, e)
        return False


def _extract_sp_cookies(context: BrowserContext, site_url: str) -> dict[str, str]:
    """Extract rtFa and FedAuth cookies from a Playwright browser context."""
    all_cookies = context.cookies([site_url])
    sp_cookies: dict[str, str] = {}
    for c in all_cookies:
        if c.get("name") in SP_COOKIE_NAMES:
            sp_cookies[c["name"]] = c["value"]
    return sp_cookies


def _persist_cookies(
    secret_ref: str, cookies: dict[str, str], site_url: str
) -> None:
    data = {
        "cookies": cookies,
        "site_url": site_url,
    }
    set_secret(secret_ref, json.dumps(data))


def _is_login_redirect(url: str) -> bool:
    """Detect common SSO/login redirect patterns."""
    login_indicators = [
        "login.microsoftonline.com",
        "adfs.",
        "/adfs/ls",
        "login.live.com",
        "accounts.accesscontrol.windows.net",
    ]
    return any(indicator in url for indicator in login_indicators)


class SessionExpiredError(Exception):
    """Raised when SharePoint session cookies are expired or missing."""
