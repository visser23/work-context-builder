"""Tests for browser-session token parsing and acquisition (no real browser)."""

from __future__ import annotations

import json

import pytest

from workctx.auth import webtokens as wt

RES = "https://outlook.office.com"
NOW = 1_000_000.0


def entry(secret="tok", scopes=None, expires=NOW + 3600):
    scopes = scopes or [f"{RES}/Mail.ReadWrite", f"{RES}/Calendars.ReadWrite"]
    return {"secret": secret, "target": " ".join(scopes), "expiresOn": str(expires)}


@pytest.fixture(autouse=True)
def clean_caches():
    wt.clear_caches()
    yield
    wt.clear_caches()


def test_pick_outlook_token_prefers_longest_lived_with_both_scopes():
    entries = [
        entry("short", expires=NOW + 1000),
        entry("long", expires=NOW + 5000),
        entry("mail-only", scopes=[f"{RES}/Mail.Read"], expires=NOW + 9000),
        entry("wrong-resource", scopes=["https://graph.microsoft.com/Mail.Read"] * 2),
    ]
    tok = wt.pick_outlook_token(entries, now=NOW)
    assert tok is not None and tok.value == "long"


def test_pick_outlook_token_ignores_expired_and_malformed():
    entries = [
        entry("expired", expires=NOW - 10),
        entry("almost", expires=NOW + 30),
        {"secret": "", "target": "x", "expiresOn": "1"},
        {"secret": "s", "target": "x", "expiresOn": "nan-ish"},
        {"secret": "s", "target": "x"},
    ]
    assert wt.pick_outlook_token(entries, now=NOW) is None
    assert wt.pick_outlook_token([], now=NOW) is None


def test_webtoken_valid_for():
    token = wt.WebToken("t", NOW + 100)
    assert token.valid_for(50, now=NOW)
    assert not token.valid_for(200, now=NOW)


def config_json(**teams):
    return json.dumps({"teams": teams})


def team(id, token="xoxc-abc", url="https://corp.slack.com/", **extra):
    return {
        "id": id,
        "name": f"Team {id}",
        "domain": f"dom-{id}",
        "token": token,
        "url": url,
        **extra,
    }


def test_parse_slack_local_config():
    raw = config_json(
        T1=team("T1"),
        E1=team("E1", url="https://org.enterprise.slack.com/"),
        bad_token=team("T2", token="not-a-token"),
        bad_url=team("T3", url="http://insecure"),
    )
    teams = wt.parse_slack_local_config(raw)
    assert {t.id for t in teams} == {"T1", "E1"}
    t1 = next(t for t in teams if t.id == "T1")
    assert t1.url == "https://corp.slack.com" and not t1.is_enterprise_org
    assert next(t for t in teams if t.id == "E1").is_enterprise_org


@pytest.mark.parametrize(
    "raw", [None, "", "not json", "[]", '{"teams": []}', '{"teams": {"a": 1}}']
)
def test_parse_slack_local_config_garbage(raw):
    assert wt.parse_slack_local_config(raw) == []


def test_select_slack_teams_default_skips_enterprise_org():
    teams = wt.parse_slack_local_config(config_json(T1=team("T1"), T2=team("T2"), E1=team("E1")))
    assert {t.id for t in wt.select_slack_teams(teams, [])} == {"T1", "T2"}
    assert {t.id for t in wt.select_slack_teams(teams, ["dom-t2"])} == {"T2"}
    assert {t.id for t in wt.select_slack_teams(teams, ["team *"])} == {"T1", "T2"}
    assert wt.select_slack_teams(teams, ["nothing"]) == []


def test_select_slack_teams_only_org_falls_back_to_org():
    teams = wt.parse_slack_local_config(config_json(E1=team("E1")))
    assert [t.id for t in wt.select_slack_teams(teams, [])] == ["E1"]


def test_session_cookie_header_encodes_once():
    raw = wt.SlackSession([], "a+b/c=")
    assert raw.cookie_header() == "d=a%2Bb%2Fc%3D"
    assert wt.SlackSession([], "a%2Bb").cookie_header() == "d=a%2Bb"


def test_reprs_never_leak_secrets():
    t = wt.SlackTeam("T1", "n", "d", "https://x.slack.com", "xoxc-SECRET")
    assert "xoxc-SECRET" not in repr(t)
    assert "dcookie" not in repr(wt.SlackSession([t], "dcookie"))


class FakePage:
    def __init__(self, local_config=None, msal=None):
        self.local_config, self.msal = local_config, msal or []
        self.clicked: list[str] = []

    def evaluate(self, script):
        if "localConfig_v2" in script:
            return self.local_config
        return self.msal


def test_get_outlook_token_caches_and_force_refreshes(monkeypatch):
    calls = []
    page = FakePage(msal=[entry("A", expires=NOW + 3600)])

    def fake_run(profile, url, step, **kwargs):
        calls.append((profile, url))
        return step(page, None)

    monkeypatch.setattr(wt, "_run_in_browser", fake_run)
    monkeypatch.setattr(wt.time, "time", lambda: NOW)
    first = wt.get_outlook_token("prof", "https://outlook.example/mail/")
    second = wt.get_outlook_token("prof", "https://outlook.example/mail/")
    assert first is second and len(calls) == 1
    wt.get_outlook_token("prof", "https://outlook.example/mail/", force=True)
    assert len(calls) == 2


def test_get_outlook_token_without_session_raises(monkeypatch):
    monkeypatch.setattr(wt, "_run_in_browser", lambda *a, **k: None)
    with pytest.raises(wt.WebSessionExpiredError, match="login-web"):
        wt.get_outlook_token("prof", "https://outlook.example/mail/")


class FakeLocator:
    def __init__(self, page):
        self.page = page
        self.first = self

    def click(self, timeout=0):
        self.page.clicked.append("click")


class SsoPage(FakePage):
    def get_by_text(self, text):
        return FakeLocator(self)


class Ctx:
    def __init__(self, cookies):
        self._c = cookies

    def cookies(self):
        return self._c


def test_slack_step_clicks_sso_once_then_returns_session():
    page = SsoPage(local_config=None)
    ctx = Ctx([])
    step = wt._slack_step("Sign in with")
    assert step(page, ctx) is None
    assert step(page, ctx) is None
    assert page.clicked == ["click"]  # only once
    page.local_config = config_json(T1=team("T1"))
    ctx._c = [{"name": "d", "value": "cookie", "domain": ".slack.com"}]
    session = step(page, ctx)
    assert session is not None and session.d_cookie == "cookie"


def test_slack_step_ignores_foreign_d_cookie():
    page = SsoPage(local_config=config_json(T1=team("T1")))
    ctx = Ctx([{"name": "d", "value": "evil", "domain": ".example.com"}])
    assert wt._slack_step("")(page, ctx) is None


def test_get_slack_session_falls_back_to_signin_url(monkeypatch):
    urls = []
    session = wt.SlackSession([], "c")

    def fake_run(profile, url, step, **kwargs):
        urls.append(url)
        return session if "signin" in url else None

    monkeypatch.setattr(wt, "_run_in_browser", fake_run)
    got = wt.get_slack_session(
        "p", "https://app.slack.com/client/E1/", signin_url="https://signin.example"
    )
    assert got is session and urls == ["https://app.slack.com/client/E1/", "https://signin.example"]
    # cached afterwards
    wt.get_slack_session("p", "https://app.slack.com/client/E1/")
    assert len(urls) == 2


def test_get_slack_session_failure(monkeypatch):
    monkeypatch.setattr(wt, "_run_in_browser", lambda *a, **k: None)
    with pytest.raises(wt.WebSessionExpiredError):
        wt.get_slack_session("p", "https://app.slack.com/client/E1/")
