"""Shared pytest fixtures."""

from __future__ import annotations

import httpx
import pytest

from tests import fake_web
from tests.fake_sharepoint import FakeTenant
from workctx.sources import teams_transcripts as tt


@pytest.fixture
def tenant(monkeypatch) -> FakeTenant:
    """A fake SharePoint tenant wired into ``TeamsTranscriptSource`` (no network/browser)."""
    fake = FakeTenant()
    real_client = httpx.Client

    def make_client(**kwargs):
        return real_client(transport=httpx.MockTransport(fake.handler), **kwargs)

    monkeypatch.setattr(tt.httpx, "Client", make_client)
    monkeypatch.setattr(
        tt, "get_valid_cookies", lambda root, profile, ref: {"rtFa": f"rt-{ref}", "FedAuth": "fa"}
    )
    monkeypatch.setattr(tt.time, "sleep", lambda _s: None)
    return fake


def _httpx_with_client(factory):
    """A stand-in for the ``httpx`` module whose ``Client`` is ``factory``.

    Each source module gets its own stand-in so several fakes can coexist.
    """
    import types

    proxy = types.ModuleType("httpx_proxy")
    proxy.__dict__.update({k: v for k, v in vars(httpx).items() if not k.startswith("__")})
    proxy.Client = factory
    return proxy


@pytest.fixture
def outlook(monkeypatch):
    """A fake Outlook REST API wired into the mail/calendar adapters (no network/browser)."""
    import time as _time

    from tests.fake_web import FakeOutlook
    from workctx.auth import webtokens
    from workctx.sources import outlook as ol

    fake = FakeOutlook()
    fake.provider_token = fake.valid_token
    fake.token_requests = []

    def provider(profile, mailbox_url, *, force=False, **_kw):
        fake.token_requests.append(force)
        if force and not getattr(fake, "always_stale", False):
            fake.provider_token = fake.valid_token
        return webtokens.WebToken(fake.provider_token, _time.time() + 3 * 3600)

    monkeypatch.setattr(ol, "httpx", _httpx_with_client(fake.client_factory()))
    monkeypatch.setattr(webtokens, "get_outlook_token", provider)
    monkeypatch.setattr(ol.time, "sleep", lambda _s: None)
    return fake


@pytest.fixture
def slack_api(monkeypatch):
    """A fake Slack Web API plus a browser session provider (no network/browser)."""
    from tests.fake_web import SLACK_COOKIE, FakeSlack
    from workctx.auth import webtokens
    from workctx.sources import slack as sl

    fake = FakeSlack()
    fake.team = webtokens.SlackTeam(
        id="T1",
        name="Contoso",
        domain="contoso",
        url="https://contoso.slack.com",
        token=fake.token,
    )
    fake.session_requests = []

    def provider(profile, client_url, *, force=False, **_kw):
        fake.session_requests.append(force)
        return webtokens.SlackSession([fake.team], SLACK_COOKIE.removeprefix("d="))

    def make_client(**kw):
        return fake_web._REAL_CLIENT(transport=httpx.MockTransport(fake.handler), **kw)

    monkeypatch.setattr(sl, "httpx", _httpx_with_client(make_client))
    monkeypatch.setattr(webtokens, "get_slack_session", provider)
    monkeypatch.setattr(sl.time, "sleep", lambda _s: None)
    return fake
