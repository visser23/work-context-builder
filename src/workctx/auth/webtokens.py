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
from urllib.parse import urlparse

import httpx

from workctx.auth.sharepoint import (
    _BROWSER_LOCK,
    _CHROMIUM_UA,
    get_profile_dir,
    relogin_command,
    relogin_hint,
)
from workctx.browsers import configure_browsers_path, ensure_chromium

logger = logging.getLogger(__name__)

OUTLOOK_RESOURCE = "https://outlook.office.com"
OUTLOOK_API_BASE = "https://outlook.office.com/api/v2.0"
# Cheapest authenticated call: used only to learn whether a token is still accepted.
_PROBE_PATH = "/me/mailfolders/inbox?$select=Id"
_PROBE_TIMEOUT_SECONDS = 20.0
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
# Token secrets the server has refused (401 / redirect to login). A token can look valid
# locally (``expiresOn`` hours away) yet be revoked server-side by continuous access
# evaluation; re-reading it from the browser cache must never hand it back again.
_outlook_rejected: set[str] = set()
_slack_cache: dict[tuple[str, str], SlackSession] = {}


def clear_caches() -> None:
    """Forget every in-memory credential (used after a 401 and by tests)."""
    with _lock:
        _outlook_cache.clear()
        _outlook_rejected.clear()
        _slack_cache.clear()


def clear_slack_cache() -> None:
    """Forget cached Slack sessions only (leaves Outlook state, e.g. rejected tokens)."""
    with _lock:
        _slack_cache.clear()


def mark_outlook_token_rejected(value: str) -> None:
    """Record that the server refused this token so it is never reused or re-read."""
    with _lock:
        _outlook_rejected.add(value)
        for profile, cached in list(_outlook_cache.items()):
            if cached.value == value:
                del _outlook_cache[profile]


def probe_outlook_token(value: str, api_base: str = OUTLOOK_API_BASE) -> bool | None:
    """Ask Outlook whether it still accepts the token.

    ``True`` = accepted, ``False`` = refused (401 or a redirect to sign-in, e.g. a
    continuous-access-evaluation ``insufficient_claims`` challenge), ``None`` =
    inconclusive (throttling, server error, network). Inconclusive must never cost a
    working token, so callers treat it as acceptable. The token is only ever sent to an
    ``https`` base and is never logged.
    """
    parsed = urlparse(api_base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    try:
        resp = httpx.get(
            api_base.rstrip("/") + _PROBE_PATH,
            headers={"Authorization": f"Bearer {value}", "Accept": "application/json"},
            follow_redirects=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.debug("Outlook token probe inconclusive: %s", type(exc).__name__)
        return None
    status = resp.status_code
    if status == 200:
        return True
    if status in (401, 301, 302, 303, 307, 308):
        challenge = str(resp.headers.get("www-authenticate", ""))
        reason = "insufficient_claims" if "insufficient_claims" in challenge else f"HTTP {status}"
        logger.info("Outlook rejected a browser-cached token (%s)", reason)
        return False
    return None


def _outlook_step(api_base: str, state: dict[str, bool]) -> Callable[[Any, Any], WebToken | None]:
    """Poll step: the best cached token that Outlook actually accepts.

    Tokens already refused are skipped, so a revoked cache entry is passed over until the
    web app has renewed it (OWA does this by itself within seconds of loading).
    """

    def step(page: Any, context: Any) -> WebToken | None:
        entries = [
            e
            for e in page.evaluate(_MSAL_JS)
            if str(e.get("secret") or "") not in _outlook_rejected
        ]
        token = pick_outlook_token(entries)
        if token is None:
            return None
        if probe_outlook_token(token.value, api_base) is False:
            _outlook_rejected.add(token.value)
            state["rejected"] = True
            return None
        return token

    return step


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

    configure_browsers_path()  # before the driver starts: never run from a cache dir
    with _BROWSER_LOCK, sync_playwright() as p:
        ensure_chromium(p)
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
    api_base: str = OUTLOOK_API_BASE,
) -> WebToken:
    """Return an Outlook REST access token for the profile's signed-in user.

    Cached in memory per profile until five minutes before expiry. The token handed back
    has been accepted by Outlook (or the check was inconclusive); tokens the server
    refused are skipped while the browser renews them. Raises
    :class:`WebSessionExpiredError` if the profile needs a human login.
    """
    with _lock:
        cached = _outlook_cache.get(profile)
        if cached and not force and cached.valid_for(MIN_TOKEN_TTL_SECONDS):
            return cached

        state = {"rejected": False}
        started = time.monotonic()
        token = _run_in_browser(
            profile,
            mailbox_url,
            _outlook_step(api_base, state),
            headless=headless,
            wait_seconds=wait_seconds,
        )
        if token is None:
            waited = time.monotonic() - started
            if state["rejected"]:
                raise WebSessionExpiredError(
                    f"Outlook rejected every token in browser profile '{profile}' and the "
                    f"browser could not renew one in {waited:.0f}s (access was revoked, e.g. "
                    f"by conditional access, and needs a fresh sign-in). "
                    f"Run: {relogin_command()}"
                )
            raise WebSessionExpiredError(
                f"No Outlook session for browser profile '{profile}' after "
                f"{waited:.0f}s (single sign-on needs a human login). "
                f"Run: {relogin_command()}"
            )
        _outlook_cache[profile] = token
        logger.info("Outlook token acquired for profile %s", profile)
        return token


# Errors that mean "this login is dead" (anything else, e.g. ratelimited, is inconclusive).
_SLACK_AUTH_ERRORS = frozenset(
    {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive"}
)
# A lapsed Slack client bounces to its sign-in page within seconds; waiting a full minute
# on it before trying the workspace sign-in URL would only slow every failed run.
_SLACK_CLIENT_PHASE_SECONDS = 25.0


def probe_slack_session(session: SlackSession) -> bool | None:
    """Ask Slack (``auth.test``) whether the session's login is still accepted.

    ``True`` = accepted, ``False`` = refused (``invalid_auth`` and friends, or a redirect),
    ``None`` = inconclusive (rate limiting, server error, network, nothing to test).
    Credentials are only ever sent to an ``https://*.slack.com`` workspace and never logged.
    """
    team = next((t for t in session.teams if t.is_enterprise_org), None) or next(
        iter(session.teams), None
    )
    if team is None:
        return None
    parsed = urlparse(team.url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host.endswith(".slack.com") or parsed.username:
        return None
    try:
        resp = httpx.post(
            f"https://{host}/api/auth.test",
            headers={"Authorization": f"Bearer {team.token}", "Cookie": session.cookie_header()},
            follow_redirects=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.debug("Slack session probe inconclusive: %s", type(exc).__name__)
        return None
    if resp.status_code in (301, 302, 303, 307, 308, 401, 403):
        return False
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("ok"):
        return True
    error = str(data.get("error") or "")
    if error in _SLACK_AUTH_ERRORS:
        logger.info("Slack rejected the browser-cached login (%s)", error)
        return False
    return None


def _slack_step(
    sso_button_text: str, state: dict[str, bool] | None = None
) -> Callable[[Any, Any], SlackSession | None]:
    clicked = {"done": False}
    state = state if state is not None else {"rejected": False}

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
            session = SlackSession(teams, d_cookie)
            # Slack keeps stale tokens in localStorage until the page itself notices they
            # are dead and bounces to sign-in; only a live check tells them from a login.
            if probe_slack_session(session) is not False:
                return session
            state["rejected"] = True
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
    """Return the Slack workspaces/tokens and ``d`` cookie for the profile's user.

    The session handed back has been accepted by Slack (or the check was inconclusive).
    """
    key = (profile, client_url)
    with _lock:
        cached = _slack_cache.get(key)
        if cached and not force:
            return cached

        state = {"rejected": False}
        session = _run_in_browser(
            profile,
            client_url,
            _slack_step(sso_button_text, state),
            headless=headless,
            wait_seconds=min(wait_seconds, _SLACK_CLIENT_PHASE_SECONDS)
            if signin_url
            else wait_seconds,
        )
        if session is None and signin_url:
            # Logged out: Slack's /workspace-signin needs the workspace's own sign-in page.
            session = _run_in_browser(
                profile,
                signin_url,
                _slack_step(sso_button_text, state),
                headless=headless,
                wait_seconds=wait_seconds,
            )
        if session is None:
            why = (
                "Slack rejected the saved login (invalid_auth) and single sign-on could not "
                "renew it"
                if state["rejected"]
                else "No Slack session"
            )
            raise WebSessionExpiredError(
                f"{why} for browser profile '{profile}' "
                "(set 'signin_url' for silent SSO, or log in once). "
                f"Run: {relogin_command()}"
            )
        _slack_cache[key] = session
        logger.info("Slack session acquired: %d workspace(s)", len(session.teams))
        return session


def _slack_interactive_step(
    client_url: str,
    signin_url: str,
    sso_button_text: str,
    state: dict[str, bool],
) -> Callable[[Any, Any], SlackSession | None]:
    """Step for the visible sign-in window, which starts on the Slack *client*.

    Slack only writes its tokens (``localConfig_v2``) on ``app.slack.com``, but after SSO the
    window stays on the workspace's own ``*.slack.com`` landing page, so the login was never
    seen. This step therefore: accepts a live client session straight away; sends a stale one
    to the sign-in page (once); and, when Slack issues a new ``d`` cookie after the user signs
    in, carries the window back to the client so the tokens appear. It never navigates while
    the user is on the identity provider's pages.
    """
    clicked = {"done": False}
    nav: dict[str, Any] = {"signin": False, "d": None}

    def d_cookie(context: Any) -> str | None:
        return next(
            (
                c["value"]
                for c in context.cookies()
                if c.get("name") == "d" and str(c.get("domain", "")).endswith("slack.com")
            ),
            None,
        )

    def step(page: Any, context: Any) -> SlackSession | None:
        parsed = urlparse(str(getattr(page, "url", "") or ""))
        host = (parsed.hostname or "").lower()
        d = d_cookie(context)
        stale = False
        if host == "app.slack.com" and "/client/" in parsed.path:
            teams = parse_slack_local_config(
                page.evaluate("window.localStorage.getItem('localConfig_v2')")
            )
            if teams and d:
                session = SlackSession(teams, d)
                if probe_slack_session(session) is not False:
                    return session
                state["rejected"] = True
                stale = True
        elif host == "app.slack.com" and "signin" in parsed.path:
            stale = True  # Slack bounced a dead login to "Find your workspace"
        if stale and not nav["signin"]:
            nav["signin"], nav["d"] = True, d
            page.goto(signin_url, wait_until="domcontentloaded")
            return None
        on_workspace_page = host.endswith(".slack.com") and host != "app.slack.com"
        if on_workspace_page and d and nav["signin"] and d != nav["d"]:
            # A fresh login: only the client page exposes its tokens.
            nav["d"] = d
            page.goto(client_url, wait_until="domcontentloaded")
            return None
        if not clicked["done"] and sso_button_text:
            with contextlib.suppress(Exception):
                page.get_by_text(sso_button_text).first.click(timeout=2_000)
                clicked["done"] = True
        return None

    return step


def web_login_step(
    kind: str,
    url: str,
    *,
    signin_url: str | None = None,
    sso_button_text: str = "Sign in with",
    api_base: str = OUTLOOK_API_BASE,
) -> Callable[[Any, Any], Any]:
    """The poll step that says "this tab now holds a login the service accepts".

    ``kind`` is ``"outlook"`` or ``"slack"`` (for Slack ``url`` is the client URL and
    ``signin_url`` the workspace sign-in page). Used by the single sign-in window and by the
    multi-tab flow in :mod:`workctx.auth.multilogin`.
    """
    if kind == "outlook":
        return _outlook_step(api_base, {"rejected": False})
    if kind == "slack":
        state = {"rejected": False}
        if signin_url:
            return _slack_interactive_step(url, signin_url, sso_button_text, state)
        return _slack_step(sso_button_text, state)
    raise ValueError(f"unknown login kind: {kind}")


def interactive_web_login(
    profile: str,
    url: str,
    kind: str,
    *,
    sso_button_text: str = "Sign in with",
    timeout_seconds: float = 300.0,
    api_base: str = OUTLOOK_API_BASE,
    signin_url: str | None = None,
) -> bool:
    """Open a visible browser at ``url`` and wait until the app session is usable.

    ``kind`` is ``"outlook"`` or ``"slack"`` (for Slack ``url`` is the client URL and
    ``signin_url`` the workspace sign-in page). Returns ``True`` once credentials that the
    service accepts can be read from the profile.
    """
    step = web_login_step(
        kind, url, signin_url=signin_url, sso_button_text=sso_button_text, api_base=api_base
    )
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


# A silent "is this login still alive?" check must be quick: renewal by single sign-on
# happens within seconds, otherwise a human is needed.
CHECK_WAIT_SECONDS = 20.0


def check_web_session(
    profile: str,
    url: str,
    kind: str,
    *,
    signin_url: str | None = None,
    sso_button_text: str = "Sign in with",
    api_base: str = OUTLOOK_API_BASE,
    wait_seconds: float = CHECK_WAIT_SECONDS,
) -> bool:
    """Silently (headless) test whether the profile holds a session the service accepts.

    ``True`` = signed in (no window needed), ``False`` = lapsed (needs a human sign-in).
    """
    try:
        if kind == "outlook":
            get_outlook_token(
                profile, url, force=True, wait_seconds=wait_seconds, api_base=api_base
            )
        elif kind == "slack":
            get_slack_session(
                profile,
                url,
                signin_url=signin_url,
                sso_button_text=sso_button_text,
                force=True,
                wait_seconds=wait_seconds,
            )
        else:
            raise ValueError(f"unknown login kind: {kind}")
    except WebSessionExpiredError:
        return False
    return True
