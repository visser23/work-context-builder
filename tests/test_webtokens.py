"""Tests for browser-session token parsing and acquisition (no real browser)."""

from __future__ import annotations

import json

import pytest

from workctx.auth import webtokens as wt

RES = "https://outlook.office.com"
REAL_PROBE = wt.probe_outlook_token  # the autouse fixture stubs the module attribute
REAL_SLACK_PROBE = wt.probe_slack_session
NOW = 1_000_000.0


def entry(secret="tok", scopes=None, expires=NOW + 3600):
    scopes = scopes or [f"{RES}/Mail.ReadWrite", f"{RES}/Calendars.ReadWrite"]
    return {"secret": secret, "target": " ".join(scopes), "expiresOn": str(expires)}


@pytest.fixture(autouse=True)
def clean_caches(monkeypatch):
    # Never hit the network from a unit test: an unprobed token counts as "unknown".
    monkeypatch.setattr(wt, "probe_outlook_token", lambda *a, **k: None)
    monkeypatch.setattr(wt, "probe_slack_session", lambda *a, **k: None)
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
    with pytest.raises(wt.WebSessionExpiredError, match="relogin"):
        wt.get_outlook_token("prof", "https://outlook.example/mail/")


# ------------------------------------------- tokens the server has revoked (CAE)
#
# Live failure: the MSAL cache held a token that looked valid for 22 more hours, but
# Outlook answered 401 ``insufficient_claims`` (continuous access evaluation). The browser
# renews such a token by itself within seconds; we used to grab the stale one at once and
# a "forced refresh" re-read the very same one, so every source reported a dead login.


class SequencePage(FakePage):
    """A page whose MSAL cache changes between polls (OWA renewing a revoked token)."""

    def __init__(self, snapshots):
        super().__init__(msal=snapshots[0])
        self._snapshots = list(snapshots)

    def evaluate(self, script):
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]


def polling_run(page, polls=5):
    def fake_run(profile, url, step, **kwargs):
        for _ in range(polls):
            result = step(page, None)
            if result is not None:
                return result
        return None

    return fake_run


def test_revoked_cached_token_is_skipped_and_the_renewed_one_is_used(monkeypatch):
    page = SequencePage(
        [
            [entry("OLD", expires=NOW + 3600)],
            [entry("OLD", expires=NOW + 3600)],
            [entry("NEW", expires=NOW + 3600)],
        ]
    )
    monkeypatch.setattr(wt, "_run_in_browser", polling_run(page))
    monkeypatch.setattr(wt.time, "time", lambda: NOW)
    probed = []

    def probe(value, api_base=wt.OUTLOOK_API_BASE):
        probed.append(value)
        return value != "OLD"

    monkeypatch.setattr(wt, "probe_outlook_token", probe)
    token = wt.get_outlook_token("prof", "https://outlook.example/mail/")
    assert token.value == "NEW"
    assert probed == ["OLD", "NEW"]  # the rejected token is not probed again


def test_only_rejected_tokens_means_login_needed_and_says_why(monkeypatch):
    page = SequencePage([[entry("OLD", expires=NOW + 3600)]])
    monkeypatch.setattr(wt, "_run_in_browser", polling_run(page))
    monkeypatch.setattr(wt.time, "time", lambda: NOW)
    monkeypatch.setattr(wt, "probe_outlook_token", lambda *a, **k: False)
    with pytest.raises(wt.WebSessionExpiredError, match="rejected"):
        wt.get_outlook_token("prof", "https://outlook.example/mail/")


def test_inconclusive_probe_does_not_discard_a_token(monkeypatch):
    # Throttling / network blips must not be mistaken for a revoked session.
    page = SequencePage([[entry("A", expires=NOW + 3600)]])
    monkeypatch.setattr(wt, "_run_in_browser", polling_run(page))
    monkeypatch.setattr(wt.time, "time", lambda: NOW)
    monkeypatch.setattr(wt, "probe_outlook_token", lambda *a, **k: None)
    assert wt.get_outlook_token("prof", "https://outlook.example/mail/").value == "A"


def test_marked_rejected_token_is_not_handed_back_on_force(monkeypatch):
    page = SequencePage([[entry("OLD", expires=NOW + 3600)]])
    monkeypatch.setattr(wt, "_run_in_browser", polling_run(page))
    monkeypatch.setattr(wt.time, "time", lambda: NOW)
    monkeypatch.setattr(wt, "probe_outlook_token", lambda *a, **k: True)
    assert wt.get_outlook_token("prof", "https://outlook.example/mail/").value == "OLD"
    wt.mark_outlook_token_rejected("OLD")  # the API said 401 to it
    with pytest.raises(wt.WebSessionExpiredError):
        wt.get_outlook_token("prof", "https://outlook.example/mail/", force=True)


class FakeResp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, True), (401, False), (302, False), (429, None), (503, None), (404, None)],
)
def test_probe_outlook_token_classification(monkeypatch, status, expected):
    monkeypatch.setattr(wt.httpx, "get", lambda *a, **k: FakeResp(status))
    assert REAL_PROBE("secret") is expected


def test_probe_outlook_token_network_error_is_inconclusive(monkeypatch):
    def boom(*a, **k):
        raise wt.httpx.ConnectError("down")

    monkeypatch.setattr(wt.httpx, "get", boom)
    assert REAL_PROBE("secret") is None


def test_probe_never_sends_the_token_to_a_non_https_base(monkeypatch):
    sent = []
    monkeypatch.setattr(wt.httpx, "get", lambda *a, **k: sent.append(a) or FakeResp(200))
    assert REAL_PROBE("secret", "http://outlook.example/api") is None
    assert sent == []


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


# ----------------------------------------- Slack sessions Slack has invalidated
#
# Live failure: Slack loads the client with stale ``xoxc-`` tokens, rejects them
# (``invalid_auth``) and within ~4s bounces to /workspace-signin and wipes localStorage.
# We read the tokens at ~2s, called that a session, and refreshing re-read the same ones.


class JsonResp(FakeResp):
    def __init__(self, status, body=None):
        super().__init__(status)
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def a_session():
    return wt.SlackSession([wt.SlackTeam("T1", "n", "d", "https://d.slack.com", "xoxc-1")], "ck")


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"ok": True}, True),
        (200, {"ok": False, "error": "invalid_auth"}, False),
        (200, {"ok": False, "error": "token_revoked"}, False),
        (302, None, False),
        (200, {"ok": False, "error": "ratelimited"}, None),
        (500, None, None),
    ],
)
def test_probe_slack_session_classification(monkeypatch, status, body, expected):
    monkeypatch.setattr(wt.httpx, "post", lambda *a, **k: JsonResp(status, body))
    assert REAL_SLACK_PROBE(a_session()) is expected


def test_probe_slack_session_network_error_is_inconclusive(monkeypatch):
    def boom(*a, **k):
        raise wt.httpx.ConnectError("down")

    monkeypatch.setattr(wt.httpx, "post", boom)
    assert REAL_SLACK_PROBE(a_session()) is None


def test_probe_slack_session_refuses_foreign_hosts(monkeypatch):
    sent = []
    monkeypatch.setattr(wt.httpx, "post", lambda *a, **k: sent.append(a) or JsonResp(200, {}))
    evil = wt.SlackSession([wt.SlackTeam("T", "n", "d", "https://evil.example", "xoxc-1")], "ck")
    assert REAL_SLACK_PROBE(evil) is None and sent == []


def test_slack_step_skips_a_session_slack_rejects_and_presses_sso(monkeypatch):
    page = SsoPage(local_config=config_json(T1=team("T1")))
    ctx = Ctx([{"name": "d", "value": "cookie", "domain": ".slack.com"}])
    state = {"rejected": False}
    monkeypatch.setattr(wt, "probe_slack_session", lambda *a, **k: False)
    step = wt._slack_step("Sign in with", state)
    assert step(page, ctx) is None
    assert state["rejected"] is True
    assert page.clicked == ["click"]  # a stale login falls through to the SSO sign-in


def test_slack_step_returns_a_session_slack_accepts(monkeypatch):
    page = SsoPage(local_config=config_json(T1=team("T1")))
    ctx = Ctx([{"name": "d", "value": "cookie", "domain": ".slack.com"}])
    monkeypatch.setattr(wt, "probe_slack_session", lambda *a, **k: True)
    assert wt._slack_step("Sign in with", {"rejected": False})(page, ctx) is not None


def test_get_slack_session_says_why_when_slack_rejected_everything(monkeypatch):
    def fake_run(profile, url, step, **kwargs):
        page = SsoPage(local_config=config_json(T1=team("T1")))
        ctx = Ctx([{"name": "d", "value": "c", "domain": ".slack.com"}])
        monkeypatch.setattr(wt, "probe_slack_session", lambda *a, **k: False)
        return step(page, ctx)

    monkeypatch.setattr(wt, "_run_in_browser", fake_run)
    with pytest.raises(wt.WebSessionExpiredError, match="rejected") as err:
        wt.get_slack_session("p", "https://app.slack.com/client/E1/")
    assert "relogin" in str(err.value)


def test_signin_fallback_phase_gets_the_full_wait(monkeypatch):
    waits = []

    def fake_run(profile, url, step, **kwargs):
        waits.append(kwargs["wait_seconds"])
        return None

    monkeypatch.setattr(wt, "_run_in_browser", fake_run)
    with pytest.raises(wt.WebSessionExpiredError):
        wt.get_slack_session("p", "https://c/", signin_url="https://s/", wait_seconds=60)
    assert waits[0] < waits[1] == 60  # a lapsed client bounces to sign-in in seconds


def test_clear_slack_cache_leaves_outlook_rejections_alone():
    wt.mark_outlook_token_rejected("OLD")
    wt.clear_slack_cache()
    assert "OLD" in wt._outlook_rejected


# ------------------------------------------------- interactive Slack sign-in window
#
# Live failure: the window sat on https://<org>.enterprise.slack.com/ after SSO, where
# localStorage has no ``localConfig_v2`` (it lives on app.slack.com), so the login was never
# detected. The step must carry the window to the client once a new login exists.

CLIENT = "https://app.slack.com/client/E1/"
SIGNIN = "https://org.enterprise.slack.com/"


class NavPage(SsoPage):
    def __init__(self, url, local_config=None):
        super().__init__(local_config=local_config)
        self.url = url
        self.gotos: list[str] = []

    def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url


def cookie(value):
    return [{"name": "d", "value": value, "domain": ".slack.com"}]


def interactive(monkeypatch, probe=True):
    monkeypatch.setattr(wt, "probe_slack_session", lambda *a, **k: probe)
    return wt._slack_interactive_step(CLIENT, SIGNIN, "Sign in with", {"rejected": False})


def test_interactive_step_accepts_a_live_session_on_the_client(monkeypatch):
    page = NavPage(CLIENT + "C1", local_config=config_json(T1=team("T1")))
    assert interactive(monkeypatch)(page, Ctx(cookie("d1"))) is not None
    assert page.gotos == []


def test_interactive_step_sends_a_stale_client_to_the_signin_page_once(monkeypatch):
    page = NavPage(CLIENT, local_config=config_json(T1=team("T1")))
    step = interactive(monkeypatch, probe=False)
    ctx = Ctx(cookie("stale"))
    assert step(page, ctx) is None
    assert page.gotos == [SIGNIN]
    page.local_config = None
    step(page, ctx)
    assert page.gotos == [SIGNIN]  # not yanked around again while the user signs in


def test_interactive_step_follows_a_new_login_back_to_the_client(monkeypatch):
    page = NavPage(CLIENT, local_config=config_json(T1=team("T1")))
    step = interactive(monkeypatch, probe=False)
    ctx = Ctx(cookie("stale"))
    step(page, ctx)  # -> signin page
    page.local_config = None
    ctx._c = cookie("fresh")  # SSO finished: Slack issued a new d cookie
    assert step(page, ctx) is None
    assert page.gotos == [SIGNIN, CLIENT]


def test_interactive_step_leaves_the_user_alone_while_signing_in(monkeypatch):
    page = NavPage(CLIENT, local_config=config_json(T1=team("T1")))
    step = interactive(monkeypatch, probe=False)
    ctx = Ctx(cookie("stale"))
    step(page, ctx)  # -> signin page
    page.local_config = None
    page.url = "https://login.microsoftonline.com/common/oauth2"  # on the identity provider
    ctx._c = cookie("fresh")
    step(page, ctx)
    assert page.gotos == [SIGNIN]
    page.url = SIGNIN  # back on Slack but the cookie has not changed
    ctx._c = cookie("fresh")
    step(page, ctx)
    step(page, ctx)
    assert page.gotos == [SIGNIN, CLIENT]  # navigates once per new login, not per poll


def test_interactive_login_starts_at_the_client_and_passes_the_signin_url(monkeypatch):
    seen = {}

    def fake_run(profile, url, step, **kwargs):
        seen.update(url=url, headless=kwargs.get("headless"))
        return object()

    monkeypatch.setattr(wt, "_run_in_browser", fake_run)
    assert wt.interactive_web_login("p", CLIENT, "slack", signin_url=SIGNIN) is True
    assert seen == {"url": CLIENT, "headless": False}


# --------------------------------------------- silent "is it lapsed?" check


def test_check_web_session_outlook_and_slack(monkeypatch):
    monkeypatch.setattr(wt, "get_outlook_token", lambda *a, **k: wt.WebToken("t", NOW + 3600))
    assert wt.check_web_session("p", "https://x/", "outlook") is True

    def expired(*a, **k):
        raise wt.WebSessionExpiredError("nope")

    monkeypatch.setattr(wt, "get_outlook_token", expired)
    assert wt.check_web_session("p", "https://x/", "outlook") is False
    monkeypatch.setattr(wt, "get_slack_session", lambda *a, **k: a_session())
    assert wt.check_web_session("p", CLIENT, "slack", signin_url=SIGNIN) is True
    monkeypatch.setattr(wt, "get_slack_session", expired)
    assert wt.check_web_session("p", CLIENT, "slack") is False


def test_check_web_session_is_quick_and_bypasses_the_cache(monkeypatch):
    seen = {}

    def fake(profile, url, **kwargs):
        seen.update(kwargs)
        return wt.WebToken("t", NOW + 3600)

    monkeypatch.setattr(wt, "get_outlook_token", fake)
    wt.check_web_session("p", "https://x/", "outlook")
    assert seen["force"] is True and seen["wait_seconds"] <= 30


def test_web_login_step_picks_the_right_step_for_each_kind(monkeypatch):
    seen = {}
    monkeypatch.setattr(wt, "_outlook_step", lambda api_base, state: seen.setdefault("o", api_base))
    monkeypatch.setattr(
        wt, "_slack_interactive_step", lambda c, s, sso, state: seen.setdefault("i", (c, s))
    )
    monkeypatch.setattr(wt, "_slack_step", lambda sso, state: seen.setdefault("p", sso))
    assert (
        wt.web_login_step("outlook", "u", api_base="https://api.example/v2")
        == "https://api.example/v2"
    )
    assert wt.web_login_step("slack", CLIENT, signin_url=SIGNIN) == (CLIENT, SIGNIN)
    assert wt.web_login_step("slack", CLIENT, sso_button_text="Sign in with X") == "Sign in with X"
    with pytest.raises(ValueError):
        wt.web_login_step("carrier-pigeon", "u")
