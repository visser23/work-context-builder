"""SharePoint cookie capture via Playwright.

Supports two flows:
1. Interactive login: opens a visible browser, user authenticates, cookies extracted.
2. Headless keepalive: opens browser with persisted profile, navigates to SP site
   and WAITS (up to SSO_WAIT_SECONDS) for the silent SSO redirect chain to hand
   back fresh rtFa/FedAuth cookies that pass an HTTP check. SharePoint expires
   sessions server-side after a few hours even though the profile's cookies look
   alive; the identity provider's long-lived cookie re-issues them silently.
   Falls back to manual re-login only if genuine interaction (password/MFA) is needed.
"""

from __future__ import annotations

import json
import logging
import platform
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

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

# How long a headless refresh waits for the silent SSO redirect chain
# (SharePoint -> login.microsoftonline.com -> SharePoint) to hand back fresh cookies.
SSO_WAIT_SECONDS = 45.0

_BROWSER_LOCK = threading.Lock()


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
    last_url = ""

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            channel="chromium",
            user_agent=_CHROMIUM_UA,
            accept_downloads=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        try:
            page = context.new_page()
            try:
                page.goto(site_url, wait_until="networkidle", timeout=120_000)
            except Exception:
                logger.debug("Initial navigation did not go idle — polling anyway")

            logger.info("Browser opened at %s — polling for auth cookies", site_url)
            print(
                "\n  Authenticate in the browser window if prompted. "
                "Cookies will be captured automatically...\n"
            )

            cookies, last_url = _wait_for_valid_cookies(
                context,
                page,
                site_url,
                wait_seconds=timeout_seconds,
                poll_interval=poll_interval,
            )
        finally:
            context.close()

    if not cookies:
        raise RuntimeError(
            "No valid SharePoint session cookies found after login "
            f"(last page: {_safe_url(last_url)}). "
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
    sso_wait_seconds: float = SSO_WAIT_SECONDS,
    poll_interval: float = 1.0,
) -> dict[str, str]:
    """Open headless browser with persisted profile to refresh session cookies.

    The browser profile keeps *persistent* rtFa/FedAuth cookies (days of life)
    and a long-lived identity-provider cookie, but SharePoint invalidates the
    session server-side after a few hours. When that happens SharePoint
    bounces the browser to the login page, which silently re-authenticates
    (Entra ``ESTSAUTHPERSISTENT``) and redirects back with *new* cookies a few
    seconds later. This function therefore does NOT trust the first cookies or
    the first URL it sees: it polls for up to ``sso_wait_seconds`` until
    cookies that pass an HTTP check against the SharePoint REST API appear.

    Only cookies that pass that check are persisted, so stale profile cookies
    can never overwrite good credentials. Raises ``SessionExpiredError`` if the
    identity provider needs genuine interaction (password/MFA prompt).
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
            f"Run: {relogin_hint(source_name)}"
        )

    started = time.monotonic()
    cookies: dict[str, str] = {}
    last_url = ""

    # One browser per profile at a time (daemon keepalive vs. sync vs. Telegram /sync).
    with _BROWSER_LOCK, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=True,
            channel="chromium",
            user_agent=_CHROMIUM_UA,
            accept_downloads=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.new_page()
            try:
                page.goto(site_url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception:
                logger.debug("Navigation timeout for %s — polling anyway", source_name)

            cookies, last_url = _wait_for_valid_cookies(
                context,
                page,
                site_url,
                wait_seconds=sso_wait_seconds,
                poll_interval=poll_interval,
            )
        finally:
            context.close()

    if not cookies:
        logger.info(
            "Headless refresh for %s found no valid cookies after %.0fs (last page: %s)",
            source_name,
            time.monotonic() - started,
            _safe_url(last_url),
        )
        raise SessionExpiredError(
            f"Session expired for '{source_name}': the identity provider needs "
            f"interactive login (stuck at {_safe_url(last_url)} after "
            f"{sso_wait_seconds:.0f}s).\n"
            f"Run: {relogin_hint(source_name)}"
        )

    _persist_cookies(secret_ref, cookies, site_url)
    logger.info(
        "SharePoint cookies refreshed for %s in %.1fs",
        source_name,
        time.monotonic() - started,
    )
    return cookies


def _wait_for_valid_cookies(
    context: BrowserContext,
    page: Any,
    site_url: str,
    *,
    wait_seconds: float,
    poll_interval: float,
) -> tuple[dict[str, str], str]:
    """Poll the browser until rtFa/FedAuth cookies that SharePoint accepts appear.

    Each distinct cookie pair is HTTP-tested once (stale profile cookies are
    rejected immediately and not re-tested every poll). Returns
    ``(cookies, last_url)``; ``cookies`` is empty if nothing valid appeared
    before the deadline.
    """
    deadline = time.monotonic() + wait_seconds
    tested: dict[str, str] | None = None
    last_url = ""

    while True:
        try:
            last_url = page.url
            candidate = _extract_sp_cookies(context, site_url)
        except Exception as exc:  # browser/page closed by the user
            logger.info("Browser closed while waiting for cookies: %s", exc)
            return {}, last_url

        if "rtFa" in candidate and "FedAuth" in candidate and candidate != tested:
            tested = dict(candidate)
            if _http_test_cookies(site_url, candidate):
                logger.info("Valid SharePoint cookies detected")
                return candidate, last_url
            logger.info(
                "Browser holds rtFa/FedAuth but SharePoint rejected them — "
                "waiting for SSO to issue fresh ones (page: %s)",
                _safe_url(last_url),
            )

        if time.monotonic() >= deadline:
            return {}, last_url

        try:
            page.wait_for_timeout(int(poll_interval * 1000))
        except Exception as exc:
            logger.info("Browser closed while waiting for cookies: %s", exc)
            return {}, last_url


def _safe_url(url: str) -> str:
    """Host + path only — login URLs carry tokens in the query string."""
    if not url:
        return "unknown"
    parsed = urlparse(url)
    return f"{parsed.hostname or 'unknown'}{parsed.path}"


def relogin_hint(source_name: str) -> str:
    """Best command for a human to run to re-authenticate ``source_name``."""
    wrapper = Path.home() / ".local" / "bin" / "workctx-relogin"
    if wrapper.exists():
        return f"workctx-relogin --source {source_name}"
    return f"uv run workctx auth login-sharepoint --config workctx.yaml --source {source_name}"


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


def _http_test_cookies(site_url: str, cookies: dict[str, str]) -> bool:
    """Validate cookies with a lightweight HTTP request to SharePoint REST API.

    Returns True if the cookies authenticate successfully (HTTP 200).
    Used internally by keepalive_and_extract to validate before persisting.
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
        return resp.status_code == 200
    except Exception:
        return False


def http_keepalive(site_url: str, cookies: dict[str, str]) -> bool:
    """Check whether SharePoint session cookies are still valid.

    Makes a single GET to /_api/web/title. Note: this does NOT extend
    cookie lifetime — SharePoint session cookies (rtFa/FedAuth) expire
    on a schedule set by the identity provider (ADFS/Entra ID). This
    function only detects expiry so the daemon can notify the user.

    Returns True if the session is still valid, False if expired.
    """
    result = _http_test_cookies(site_url, cookies)
    if result:
        logger.debug("Cookie keepalive OK for %s", site_url)
    else:
        logger.info("Cookie keepalive failed for %s", site_url)
    return result


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


def tenant_hosts(url: str) -> tuple[str, str]:
    """Return ``(team_sites_root, onedrive_root)`` URLs for the tenant behind ``url``.

    SharePoint Online serves team sites from ``https://<tenant>.sharepoint.com`` and
    personal OneDrives from ``https://<tenant>-my.sharepoint.com``. Session cookies
    (``FedAuth``) are per host, so each needs its own login capture.
    Raises ``ValueError`` if ``url`` is not a SharePoint Online URL.
    """
    host = (urlparse(url).hostname or "").lower()
    first, _, rest = host.partition(".")
    if not first or "sharepoint" not in rest:
        raise ValueError(f"Not a SharePoint Online URL: {_safe_url(url)}")
    tenant = first[: -len("-my")] if first.endswith("-my") else first
    return f"https://{tenant}.{rest}", f"https://{tenant}-my.{rest}"


def onedrive_secret_ref(secret_ref: str) -> str:
    """Credential-store key for the OneDrive-host cookies derived from ``secret_ref``."""
    return f"{secret_ref}-my"


def get_valid_cookies(host_url: str, profile_name: str, secret_ref: str) -> dict[str, str]:
    """Return cookies for ``host_url`` that SharePoint currently accepts.

    Tries the credential-store copy first (one cheap HTTP check); on rejection
    falls back to a headless refresh using the persistent browser profile
    ``profile_name`` (silent SSO). Raises ``SessionExpiredError`` if a human
    login is genuinely required.
    """
    cached = load_cookies(secret_ref)
    if cached and _http_test_cookies(host_url, cached):
        return cached
    if cached:
        logger.info("Cached cookies for %s rejected, refreshing via browser", _safe_url(host_url))
    return keepalive_and_extract(host_url, profile_name, secret_ref)


class SessionExpiredError(Exception):
    """Raised when SharePoint session cookies are expired or missing."""
