"""Session credentials for web apps (Outlook, Slack) from a logged-in browser profile.

The browser profile is the same persistent Playwright profile used for SharePoint
(see :mod:`workctx.auth.sharepoint`). Single sign-on cookies in that profile let a
*headless* browser silently sign in to Outlook on the web and Slack, after which
we read the credentials the web apps themselves use:

* **Outlook on the web** keeps MSAL access tokens (audience
  ``https://outlook.office.com``) in browser storage. We read one and call the
  Outlook REST API with it. No app registration or admin consent is involved.
* **Slack** keeps a per-workspace ``xoxc-`` client token in ``localStorage``
  (``localConfig_v2``) and a ``d`` session cookie. Together they authorise the
  same Web API calls the Slack web client makes.

Credentials are held **in memory only** (never written to disk or the keychain by
this module) and expire with the process. Every function that touches the browser
goes through :func:`_run_in_browser`, which tests replace with a fake.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from workctx.auth.sharepoint import _BROWSER_LOCK, _CHROMIUM_UA, get_profile_dir, relogin_hint

logger = logging.getLogger(__name__)

OUTLOOK_RESOURCE = "https://outlook.office.com"
# Fresh-enough: refuse tokens that expire within this many seconds.
MIN_TOKEN_TTL_SECONDS = 300
# Seconds a headless run waits for silent single sign-on to hand back a session.
SSO_WAIT_SECONDS = 60.0
# A page that hands us nothing for this long while sitting on a login page is stuck.
_LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com")


class WebSessionExpiredError(Exception):
    """The browser profile has no usable session for the app (needs a human login)."""


@dataclass(frozen=True)
class WebToken:
    """A bearer token and the epoch second at which it stops being valid."""

    value: str = field(repr=False)
    expires_at: float

    def valid_for(self, seconds: float, now: float | None = None) -> bool:
        return self.expires_at - (now if now is not None else time.time()) >= seconds


@dataclass(frozen=True)
class SlackTeam:
    """One Slack workspace (or the Enterprise Grid org itself) in the browser session."""

    id: str
    name: str
    domain: str
    url: str
    token: str = field(repr=False)
    enterprise_id: str | None = None

    @property
    def is_enterprise_org(self) -> bool:
        return self.id.startswith("E")


@dataclass(frozen=True)
class SlackSession:
    """All workspace tokens plus the ``d`` cookie that authorises them."""

    teams: list[SlackTeam]
    d_cookie: str = field(repr=False)

    def cookie_header(self) -> str:
        """``Cookie`` header value (the ``d`` cookie must be URL-encoded)."""
        value = (
            self.d_cookie if "%" in self.d_cookie else urllib.parse.quote(self.d_cookie, safe="")
        )
        return f"d={value}"


# ---------------------------------------------------------------------- parsing


def pick_outlook_token(
    entries: list[dict[str, Any]],
    *,
    now: float | None = None,
    resource: str = OUTLOOK_RESOURCE,
    min_ttl: float = MIN_TOKEN_TTL_SECONDS,
) -> WebToken | None:
    """Choose the best usable Outlook access token from MSAL cache ``entries``.

    ``entries`` are the parsed MSAL access-token records (``secret``, ``target`` and
    ``expiresOn`` in epoch seconds). The token must be issued for ``resource`` and
    carry mail *and* calendar read scopes; the longest-lived one wins.
    """
    current = now if now is not None else time.time()
    best: WebToken | None = None
    for entry in entries:
        secret = entry.get("secret")
        target = entry.get("target") or ""
        try:
            expires = float(entry.get("expiresOn") or "nan")
        except (TypeError, ValueError):
            continue
        scopes = target.split()
        has_mail = any(s.startswith(f"{resource}/Mail.Read") for s in scopes)
        has_calendar = any(s.startswith(f"{resource}/Calendars.Read") for s in scopes)
        if not (secret and has_mail and has_calendar):
            continue
        candidate = WebToken(str(secret), expires)
        if not candidate.valid_for(min_ttl, current):
            continue
        if best is None or candidate.expires_at > best.expires_at:
            best = candidate
    return best


def parse_slack_local_config(raw: str | None) -> list[SlackTeam]:
    """Parse Slack's ``localStorage.localConfig_v2`` JSON into workspaces with tokens."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    teams_raw = data.get("teams") if isinstance(data, dict) else None
    if not isinstance(teams_raw, dict):
        return []
    teams: list[SlackTeam] = []
    for team_id, info in teams_raw.items():
        if not isinstance(info, dict):
            continue
        token = info.get("token")
        url = str(info.get("url") or "")
        if (
            not isinstance(token, str)
            or not token.startswith("xoxc-")
            or not url.startswith("https://")
        ):
            continue
        teams.append(
            SlackTeam(
                id=str(info.get("id") or team_id),
                name=str(info.get("name") or team_id),
                domain=str(info.get("domain") or ""),
                url=url.rstrip("/"),
                token=token,
                enterprise_id=info.get("enterprise_id") or None,
            )
        )
    return teams


def select_slack_teams(teams: list[SlackTeam], patterns: list[str]) -> list[SlackTeam]:
    """Workspaces to read: ``patterns`` (globs on id/domain/name) or, by default,
    every workspace - excluding the Enterprise Grid org shell when real workspaces exist.
    """
    import fnmatch

    workspaces = [t for t in teams if not t.is_enterprise_org] or list(teams)
    if not patterns:
        return workspaces
    lowered = [p.lower() for p in patterns]
    return [
        t
        for t in workspaces
        if any(
            fnmatch.fnmatch(value.lower(), pat)
            for pat in lowered
            for value in (t.id, t.domain, t.name)
        )
    ]


# ---------------------------------------------------------------------- browser

_MSAL_JS = """() => {
  const out = [];
  for (const store of [window.localStorage, window.sessionStorage]) {
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i);
      if (!/accesstoken/i.test(key)) continue;
      try {
        const v = JSON.parse(store.getItem(key));
        if (v && v.secret) out.push({secret: v.secret, target: v.target || '',
                                     expiresOn: v.expiresOn});
      } catch (e) {}
    }
  }
  return out;
}"""

# Serialises credential acquisition so concurrent sources (mail + calendar) share one
# browser launch instead of each starting their own.
_lock = threading.RLock()
_outlook_cache: dict[str, WebToken] = {}
_slack_cache: dict[tuple[str, str], SlackSession] = {}


def clear_caches() -> None:
    """Forget every in-memory credential (used after a 401 and by tests)."""
    with _lock:
        _outlook_cache.clear()
        _slack_cache.clear()


def _is_login_page(url: str) -> bool:
    return any(host in url for host in _LOGIN_HOSTS)


def _launch_context(p: Any, profile_dir: Any, headless: bool) -> Any:
    """Launch the persistent profile. Headless Chromium advertises ``HeadlessChrome`` and
    Slack rejects that, so a headless run first reads the real Chromium user agent and
    re-launches presenting it as plain Chrome (version always current).
    """
    options: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": headless,
        "channel": "chromium",
        "accept_downloads": False,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if headless:
        user_agent = _CHROMIUM_UA
        probe = p.chromium.launch_persistent_context(**options)
        try:
            user_agent = str(probe.new_page().evaluate("navigator.userAgent")).replace(
                "HeadlessChrome", "Chrome"
            )
        except Exception:
            logger.debug("Could not read the Chromium user agent; using the default")
        finally:
            probe.close()
        options["user_agent"] = user_agent
    return p.chromium.launch_persistent_context(**options)


def _run_in_browser[T](
    profile: str,
    url: str,
    step: Callable[[Any, Any], T | None],
    *,
    headless: bool = True,
    wait_seconds: float = SSO_WAIT_SECONDS,
    poll_seconds: float = 1.0,
) -> T | None:
    """Open ``url`` in the persistent profile and poll ``step(page, context)`` until it
    returns a non-``None`` value or ``wait_seconds`` elapse (``None`` then).

    Headed runs (interactive login) may start from an empty profile; headless runs
    require an existing, logged-in one.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright not installed. Run: uv sync --extra playwright && "
            "uv run playwright install chromium"
        ) from exc

    profile_dir = get_profile_dir(profile)
    if headless and not any(profile_dir.iterdir()):
        raise WebSessionExpiredError(
            f"No browser profile found for '{profile}'. Run: {relogin_hint(profile)}"
        )

    with _BROWSER_LOCK, sync_playwright() as p:
        context = _launch_context(p, profile_dir, headless)
        try:
            page = context.new_page()
            with contextlib.suppress(Exception):
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    result = step(page, context)
                except Exception as exc:  # page navigating / closed by the user
                    logger.debug("Browser step failed: %s", exc)
                    result = None
                if result is not None:
                    return result
                if time.monotonic() >= deadline:
                    return None
                try:
                    page.wait_for_timeout(int(poll_seconds * 1000))
                except Exception:
                    return None
        finally:
            context.close()


def get_outlook_token(
    profile: str,
    mailbox_url: str,
    *,
    force: bool = False,
    headless: bool = True,
    wait_seconds: float = SSO_WAIT_SECONDS,
) -> WebToken:
    """Return an Outlook REST access token for the profile's signed-in user.

    Cached in memory per profile until five minutes before expiry. Raises
    :class:`WebSessionExpiredError` if the profile needs a human login.
    """
    with _lock:
        cached = _outlook_cache.get(profile)
        if cached and not force and cached.valid_for(MIN_TOKEN_TTL_SECONDS):
            return cached

        def step(page: Any, context: Any) -> WebToken | None:
            return pick_outlook_token(page.evaluate(_MSAL_JS))

        started = time.monotonic()
        token = _run_in_browser(
            profile, mailbox_url, step, headless=headless, wait_seconds=wait_seconds
        )
        if token is None:
            raise WebSessionExpiredError(
                f"No Outlook session for browser profile '{profile}' after "
                f"{time.monotonic() - started:.0f}s (single sign-on needs a human login). "
                f"Run: uv run workctx auth login-web --source <name>"
            )
        _outlook_cache[profile] = token
        logger.info("Outlook token acquired for profile %s", profile)
        return token


def _slack_step(sso_button_text: str) -> Callable[[Any, Any], SlackSession | None]:
    clicked = {"done": False}

    def step(page: Any, context: Any) -> SlackSession | None:
        raw = page.evaluate("window.localStorage.getItem('localConfig_v2')")
        teams = parse_slack_local_config(raw)
        d_cookie = next(
            (
                c["value"]
                for c in context.cookies()
                if c.get("name") == "d" and str(c.get("domain", "")).endswith("slack.com")
            ),
            None,
        )
        if teams and d_cookie:
            return SlackSession(teams, d_cookie)
        # Signed out: Slack shows an SSO sign-in page. Press the SSO button once;
        # an existing identity-provider session completes the sign-in silently.
        if not clicked["done"] and sso_button_text:
            with contextlib.suppress(Exception):
                page.get_by_text(sso_button_text).first.click(timeout=2_000)
                clicked["done"] = True
        return None

    return step


def get_slack_session(
    profile: str,
    client_url: str,
    *,
    signin_url: str | None = None,
    sso_button_text: str = "Sign in with",
    force: bool = False,
    headless: bool = True,
    wait_seconds: float = SSO_WAIT_SECONDS,
) -> SlackSession:
    """Return the Slack workspaces/tokens and ``d`` cookie for the profile's user."""
    key = (profile, client_url)
    with _lock:
        cached = _slack_cache.get(key)
        if cached and not force:
            return cached

        session = _run_in_browser(
            profile,
            client_url,
            _slack_step(sso_button_text),
            headless=headless,
            wait_seconds=wait_seconds,
        )
        if session is None and signin_url:
            # Logged out: Slack's /workspace-signin needs the workspace's own sign-in page.
            session = _run_in_browser(
                profile,
                signin_url,
                _slack_step(sso_button_text),
                headless=headless,
                wait_seconds=wait_seconds,
            )
        if session is None:
            raise WebSessionExpiredError(
                f"No Slack session for browser profile '{profile}' "
                "(set 'signin_url' for silent SSO, or log in once). "
                "Run: uv run workctx auth login-web --source <name>"
            )
        _slack_cache[key] = session
        logger.info("Slack session acquired: %d workspace(s)", len(session.teams))
        return session


def interactive_web_login(
    profile: str,
    url: str,
    kind: str,
    *,
    sso_button_text: str = "Sign in with",
    timeout_seconds: float = 300.0,
) -> bool:
    """Open a visible browser at ``url`` and wait until the app session is usable.

    ``kind`` is ``"outlook"`` or ``"slack"``. Returns ``True`` once credentials can
    be read from the profile.
    """
    step: Callable[[Any, Any], Any]
    if kind == "outlook":

        def step(page: Any, context: Any) -> Any:
            return pick_outlook_token(page.evaluate(_MSAL_JS))

    elif kind == "slack":
        step = _slack_step(sso_button_text)
    else:
        raise ValueError(f"unknown login kind: {kind}")
    return (
        _run_in_browser(
            profile,
            url,
            step,
            headless=False,
            wait_seconds=timeout_seconds,
            poll_seconds=2.0,
        )
        is not None
    )
