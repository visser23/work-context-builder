"""Shared pytest fixtures."""

from __future__ import annotations

import httpx
import pytest

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
