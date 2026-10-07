"""Tests for SharePoint cookie refresh / login flows.

Regression coverage for the "session expired but I'm still logged in" bug:

* The browser profile keeps *persistent* rtFa/FedAuth cookies (days of life) even
  after SharePoint has invalidated the session server-side. The silent SSO
  round-trip (SharePoint -> login.microsoftonline.com -> SharePoint) takes a few
  seconds, so the refresh must WAIT for fresh, HTTP-valid cookies instead of
  reading the first cookies it sees (or the first login URL it sees).
* Stale cookies must never be written to the credential store.

Playwright is faked so the tests need neither a browser nor the optional extra.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from workctx.auth import sharepoint as sp

SITE = "https://example.sharepoint.com/sites/test"
LOGIN_URL = "https://login.microsoftonline.com/common/oauth2/authorize?x=1"
SITE_URL = f"{SITE}/SitePages/Home.aspx"

STALE = {"rtFa": "stale", "FedAuth": "stale"}
FRESH = {"rtFa": "good-rtfa", "FedAuth": "good-fedauth"}


class FakePage:
    def __init__(self, script: FakeBrowser) -> None:
        self._script = script

    @property
    def url(self) -> str:
        return self._script.step[0]

    def goto(self, *args, **kwargs) -> None:
        self._script.index = 0

    def wait_for_timeout(self, ms: int) -> None:
        self._script.advance()


class FakeContext:
    def __init__(self, script: FakeBrowser) -> None:
        self._script = script
        self.closed = False

    def new_page(self) -> FakePage:
        return FakePage(self._script)

    def cookies(self, urls=None) -> list[dict]:
        return [{"name": k, "value": v} for k, v in self._script.step[1].items()]

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    """Scripted sequence of (page_url, sharepoint_cookies) states, one per poll."""

    def __init__(self, steps: list[tuple[str, dict[str, str]]]) -> None:
        self.steps = steps
        self.index = 0
        self.context = FakeContext(self)

    @property
    def step(self) -> tuple[str, dict[str, str]]:
        return self.steps[min(self.index, len(self.steps) - 1)]

    def advance(self) -> None:
        self.index += 1


@pytest.fixture
def fake_playwright(monkeypatch):
    """Install a fake ``playwright.sync_api`` and return a loader for scripts."""
    holder: dict[str, FakeBrowser] = {}

    class _PW:
        def __enter__(self):
            pw = MagicMock()
            pw.chromium.launch_persistent_context.side_effect = lambda **kw: holder["b"].context
            return pw

        def __exit__(self, *exc):
            return False

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: _PW()  # type: ignore[attr-defined]
    pkg = types.ModuleType("playwright")
    pkg.sync_api = mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)

    def load(steps: list[tuple[str, dict[str, str]]]) -> FakeBrowser:
        holder["b"] = FakeBrowser(steps)
        return holder["b"]

    return load


@pytest.fixture
def profile(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sp, "_profiles_dir", lambda: tmp_path / "profiles")
    d = sp.get_profile_dir("src")
    (d / "Default").mkdir()
    return d


@pytest.fixture
def persisted(monkeypatch):
    calls: list[dict[str, str]] = []
    monkeypatch.setattr(sp, "_persist_cookies", lambda ref, ck, url: calls.append(dict(ck)))
    # Server accepts only "good-*" cookies; anything else is a rejected session.
    monkeypatch.setattr(
        sp, "_http_test_cookies", lambda url, ck: ck.get("FedAuth", "").startswith("good")
    )
    return calls


class TestKeepaliveWaitsForSilentSso:
    def test_waits_through_login_redirect_when_cookies_cleared(
        self, fake_playwright, profile, persisted
    ):
        """SharePoint cleared its cookies and bounced to login; SSO finishes later."""
        fake_playwright(
            [
                (LOGIN_URL, {}),
                (LOGIN_URL, {}),
                (LOGIN_URL, {}),
                (SITE_URL, FRESH),
            ]
        )
        result = sp.keepalive_and_extract(
            SITE, "src", "ref", sso_wait_seconds=5, poll_interval=0.0
        )
        assert result == FRESH
        assert persisted == [FRESH]

    def test_stale_persistent_cookies_are_not_trusted_or_stored(
        self, fake_playwright, profile, persisted
    ):
        """Profile still holds days-old cookies the server rejects; fresh ones follow."""
        fake_playwright(
            [
                (LOGIN_URL, STALE),
                (LOGIN_URL, STALE),
                (SITE_URL, FRESH),
            ]
        )
        result = sp.keepalive_and_extract(
            SITE, "src", "ref", sso_wait_seconds=5, poll_interval=0.0
        )
        assert result == FRESH
        assert persisted == [FRESH]  # STALE was never written

    def test_raises_and_stores_nothing_when_sso_needs_interaction(
        self, fake_playwright, profile, persisted
    ):
        fake_playwright([(LOGIN_URL, {})])
        with pytest.raises(sp.SessionExpiredError, match="interactive"):
            sp.keepalive_and_extract(
                SITE, "src", "ref", sso_wait_seconds=0.2, poll_interval=0.01
            )
        assert persisted == []

    def test_raises_when_cookies_never_validate(self, fake_playwright, profile, persisted):
        fake_playwright([(SITE_URL, STALE)])
        with pytest.raises(sp.SessionExpiredError):
            sp.keepalive_and_extract(
                SITE, "src", "ref", sso_wait_seconds=0.2, poll_interval=0.01
            )
        assert persisted == []

    def test_stale_pair_is_only_http_tested_once(self, fake_playwright, profile, monkeypatch):
        """Don't hammer SharePoint with the same rejected cookies every poll."""
        monkeypatch.setattr(sp, "_persist_cookies", lambda *a: None)
        tester = MagicMock(return_value=False)
        monkeypatch.setattr(sp, "_http_test_cookies", tester)
        fake_playwright([(SITE_URL, STALE)])
        with pytest.raises(sp.SessionExpiredError):
            sp.keepalive_and_extract(
                SITE, "src", "ref", sso_wait_seconds=0.2, poll_interval=0.01
            )
        assert tester.call_count == 1

    def test_browser_context_is_always_closed(self, fake_playwright, profile, persisted):
        browser = fake_playwright([(LOGIN_URL, {})])
        with pytest.raises(sp.SessionExpiredError):
            sp.keepalive_and_extract(
                SITE, "src", "ref", sso_wait_seconds=0.1, poll_interval=0.01
            )
        assert browser.context.closed

    def test_error_message_does_not_leak_url_query(self, fake_playwright, profile, persisted):
        fake_playwright([(LOGIN_URL + "&code=SECRET", {})])
        with pytest.raises(sp.SessionExpiredError) as exc:
            sp.keepalive_and_extract(
                SITE, "src", "ref", sso_wait_seconds=0.1, poll_interval=0.01
            )
        assert "SECRET" not in str(exc.value)
        assert "workctx-relogin" in str(exc.value) or "login-sharepoint" in str(exc.value)


class TestInteractiveLoginValidates:
    def test_does_not_store_stale_profile_cookies(self, fake_playwright, profile, persisted):
        """Relogin must wait past stale persistent cookies and store only valid ones."""
        fake_playwright(
            [
                (LOGIN_URL, STALE),
                (LOGIN_URL, STALE),
                (SITE_URL, FRESH),
            ]
        )
        result = sp.interactive_login(
            SITE, "src", "ref", headless=True, timeout_seconds=5, poll_interval=0.0
        )
        assert result == FRESH
        assert persisted == [FRESH]

    def test_raises_if_nothing_valid_before_timeout(self, fake_playwright, profile, persisted):
        fake_playwright([(LOGIN_URL, STALE)])
        with pytest.raises(RuntimeError):
            sp.interactive_login(
                SITE, "src", "ref", headless=True, timeout_seconds=0.2, poll_interval=0.01
            )
        assert persisted == []


class TestDaemonTriesRefreshBeforeAlerting:
    @staticmethod
    def _daemon():
        from workctx.daemon import Daemon

        config = MagicMock()
        config.project.name = "t"
        config.project.id = "t"
        src = MagicMock()
        src.mode = "browser"
        src.name = "nhs-sharepoint"
        src.auth.secret_ref = "ref"
        config.sources.sharepoint = [src]
        config.notifications.telegram.enabled = False
        return Daemon(config, "/tmp/workctx.yaml")

    BLOB: ClassVar[dict] = {"cookies": STALE, "site_url": SITE}

    def test_no_alert_when_headless_refresh_recovers(self):
        d = self._daemon()
        with (
            patch("workctx.auth.sharepoint.load_cookie_blob", return_value=self.BLOB),
            patch("workctx.auth.sharepoint.http_keepalive", return_value=False),
            patch("workctx.auth.sharepoint.keepalive_and_extract", return_value=FRESH) as ka,
            patch.object(d, "_notify") as notify,
        ):
            d._check_cookie_keepalive()
        ka.assert_called_once()
        notify.assert_not_called()

    def test_alerts_only_when_refresh_cannot_recover(self):
        d = self._daemon()
        with (
            patch("workctx.auth.sharepoint.load_cookie_blob", return_value=self.BLOB),
            patch("workctx.auth.sharepoint.http_keepalive", return_value=False),
            patch(
                "workctx.auth.sharepoint.keepalive_and_extract",
                side_effect=sp.SessionExpiredError("needs interactive"),
            ),
            patch.object(d, "_notify") as notify,
        ):
            d._check_cookie_keepalive()
        notify.assert_called_once()
        assert "session expired" in notify.call_args[0][0].lower()

    def test_no_refresh_when_cookies_still_valid(self):
        d = self._daemon()
        with (
            patch("workctx.auth.sharepoint.load_cookie_blob", return_value=self.BLOB),
            patch("workctx.auth.sharepoint.http_keepalive", return_value=True),
            patch("workctx.auth.sharepoint.keepalive_and_extract") as ka,
            patch.object(d, "_notify") as notify,
        ):
            d._check_cookie_keepalive()
        ka.assert_not_called()
        notify.assert_not_called()

    def test_unexpected_refresh_crash_alerts_instead_of_killing_daemon(self):
        d = self._daemon()
        with (
            patch("workctx.auth.sharepoint.load_cookie_blob", return_value=self.BLOB),
            patch("workctx.auth.sharepoint.http_keepalive", return_value=False),
            patch("workctx.auth.sharepoint.keepalive_and_extract", side_effect=OSError("boom")),
            patch.object(d, "_notify") as notify,
        ):
            d._check_cookie_keepalive()
        notify.assert_called_once()
