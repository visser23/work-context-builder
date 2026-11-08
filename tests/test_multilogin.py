"""One browser window, one tab per lapsed login: tracking, timeouts and cleanup (no browser)."""

from __future__ import annotations

import contextlib

import pytest

from workctx.auth import multilogin as ml
from workctx.auth import sharepoint as sp


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakePage:
    def __init__(self, ctx, url=""):
        self.ctx, self.url = ctx, url
        self.closed = False
        self.fronted = 0

    def goto(self, url, **kwargs):
        self.url = url

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True

    def bring_to_front(self):
        self.fronted += 1
        self.ctx.fronted.append(self.url)

    def wait_for_timeout(self, ms):
        if self.closed or self.ctx.closed:
            raise RuntimeError("browser closed")
        self.ctx.clock.now += ms / 1000
        self.ctx.on_tick()


class FakeContext:
    def __init__(self, clock):
        self.clock = clock
        self.pages: list[FakePage] = []
        self.closed = False
        self.fronted: list[str] = []
        self.on_tick = lambda: None

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


@pytest.fixture
def browser(monkeypatch):
    clock = Clock()
    ctx = FakeContext(clock)
    launched = []

    @contextlib.contextmanager
    def fake_session(profile):
        launched.append(profile)
        try:
            yield ctx
        finally:
            ctx.close()

    monkeypatch.setattr(ml, "_browser_session", fake_session)
    ctx.launched = launched
    return ctx


def run(browser, tasks, **kw):
    log: list[str] = []
    result = ml.run_login_tabs(
        "prof", tasks, report=log.append, clock=browser.clock, poll_seconds=2.0, **kw
    )
    return result, log


def after(n, value="ok"):
    """A step that succeeds on its n-th poll (1-based)."""
    calls = {"n": 0}

    def step(page, context):
        calls["n"] += 1
        return value if calls["n"] >= n else None

    return step


def task(name, step, url=None, on_success=None, kind="slack"):
    return ml.LoginTask(name, kind, url or f"https://{name}.example/", step, on_success)


def test_opens_one_tab_per_task_and_closes_the_browser_when_all_are_done(browser):
    saved = []
    tasks = [
        task("a", after(1), on_success=saved.append),
        task("b", after(3)),
        task("c", after(2)),
    ]
    result, log = run(browser, tasks)
    assert result == {"a": True, "b": True, "c": True}
    assert [p.url for p in browser.pages] == [t.url for t in tasks]
    assert browser.closed and all(p.closed for p in browser.pages)
    assert saved == ["ok"]
    joined = "\n".join(log)
    assert "(1/3)" in joined and "(2/3)" in joined and "(3/3)" in joined
    assert [line.split()[1] for line in log if "signed in" in line] == ["a", "c", "b"]


def test_no_tasks_launches_no_browser(browser):
    result, _ = run(browser, [])
    assert result == {} and browser.launched == []


def test_first_pending_tab_is_brought_to_the_front_after_each_completion(browser):
    tasks = [task("a", after(1)), task("b", after(2))]
    run(browser, tasks)
    assert browser.fronted[0] == tasks[0].url  # starts on the first login
    assert tasks[1].url in browser.fronted  # then moves on to the next pending one


def test_timeout_leaves_only_unfinished_logins_failed_and_still_closes(browser):
    tasks = [task("done", after(1)), task("stuck", lambda p, c: None)]
    result, log = run(browser, tasks, timeout_seconds=20)
    assert result == {"done": True, "stuck": False}
    assert browser.closed
    assert any("stuck" in line and "timed out" in line.lower() for line in log)


def test_waiting_status_is_reported_periodically(browser):
    result, log = run(browser, [task("slow", after(40))], timeout_seconds=300)
    assert result == {"slow": True}
    assert any("Waiting for" in line and "slow" in line for line in log)


def test_closing_one_tab_fails_only_that_login(browser):
    seen = {"n": 0}

    def step_b(page, context):
        seen["n"] += 1
        if seen["n"] == 2:
            browser.pages[1].closed = True  # the user closes tab b
        return None

    result, log = run(browser, [task("a", after(4)), task("b", step_b)])
    assert result == {"a": True, "b": False}
    assert any("b" in line and "tab closed" in line for line in log)


def test_closing_the_whole_window_stops_promptly(browser):
    def step(page, context):
        for p in browser.pages:
            p.closed = True
        return None

    result, _ = run(browser, [task("a", step), task("b", step)], timeout_seconds=300)
    assert result == {"a": False, "b": False}
    assert browser.clock.now < 10  # did not sit out the full timeout


def test_step_errors_are_treated_as_not_yet(browser):
    calls = {"n": 0}

    def flaky(page, context):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("page is navigating")
        return "ok"

    result, _ = run(browser, [task("a", flaky)])
    assert result == {"a": True}


def test_a_failing_save_fails_that_login_but_not_the_others(browser):
    def boom(result):
        raise OSError("keychain locked")

    result, log = run(browser, [task("a", after(1), on_success=boom), task("b", after(1))])
    assert result == {"a": False, "b": True}
    assert any("a" in line and "keychain locked" in line for line in log)


def test_browser_closes_even_if_something_unexpected_is_raised(browser):
    def explode(page, context):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(browser, [task("a", explode)])
    assert browser.closed


# ---------------------------------------------------- SharePoint tab step


def test_sharepoint_step_returns_cookies_only_once_sharepoint_accepts_them(monkeypatch):
    cookies = {"rtFa": "r", "FedAuth": "f"}
    monkeypatch.setattr(sp, "_extract_sp_cookies", lambda ctx, url: dict(cookies))
    verdicts = iter([False, True])
    tested = []
    monkeypatch.setattr(
        sp, "_http_test_cookies", lambda url, c: tested.append(dict(c)) or next(verdicts)
    )
    step = sp.sharepoint_login_step("https://t.sharepoint.example/sites/x")
    assert step(None, None) is None  # stale cookies from the profile: rejected
    assert step(None, None) is None  # same pair: not re-tested every poll
    assert len(tested) == 1
    cookies["FedAuth"] = "fresh"  # a new login produced new cookies
    assert step(None, None) == {"rtFa": "r", "FedAuth": "fresh"}


def test_sharepoint_step_needs_both_cookies(monkeypatch):
    monkeypatch.setattr(sp, "_extract_sp_cookies", lambda ctx, url: {"rtFa": "r"})
    monkeypatch.setattr(sp, "_http_test_cookies", lambda *a: pytest.fail("must not probe"))
    assert sp.sharepoint_login_step("https://t.sharepoint.example")(None, None) is None
