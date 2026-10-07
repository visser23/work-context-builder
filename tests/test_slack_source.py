"""Slack client and adapter tests against a fake Web API."""

from __future__ import annotations

import pytest

from tests.fake_web import slack_ts
from workctx.auth.webtokens import SlackTeam
from workctx.config import SlackSource
from workctx.models import ChangeAction, SourceType
from workctx.sources.slack import SlackAdapter, SlackApiError, SlackAuthError, SlackClient
from workctx.state import StateDB

CLIENT_URL = "https://app.slack.com/client/E1/"
READ_ONLY = {
    "users.conversations",
    "conversations.history",
    "conversations.replies",
    "users.info",
    "auth.test",
}


def cfg(**kw):
    kw.setdefault("since_days", 7)
    return SlackSource(name="slack", profile="p", client_url=CLIENT_URL, **kw)


def channel(cid, name, **kw):
    return {"id": cid, "name": name, "is_channel": True, **kw}


def msg(ts, user, text, **kw):
    return {"ts": ts, "user": user, "text": text, **kw}


@pytest.fixture
def db(tmp_path):
    database = StateDB(tmp_path / "state.sqlite")
    yield database
    database.close()


@pytest.fixture
def ws(slack_api):
    slack_api.users = {
        "U1": {"name": "alice", "profile": {"display_name": "Alice A"}},
        "U2": {"name": "bob", "real_name": "Bob B", "profile": {}},
    }
    slack_api.conversations = [channel("C1", "general")]
    slack_api.messages["C1"] = [
        msg(slack_ts(1, 9, seq=1), "U1", "Morning <@U2>, see <https://example.com/doc|the doc>"),
        msg(slack_ts(1, 9, 5, seq=2), "U2", "*Thanks* &amp; welcome"),
        msg(slack_ts(0, 8, seq=3), "U1", "Today's note"),
        msg(slack_ts(1, 9, 1, seq=4), "U1", "joined", subtype="channel_join"),
    ]
    return slack_api


def discover(adapter, db, **kw):
    try:
        return adapter.discover_changes(db, None, **kw)
    finally:
        adapter.close()


def test_discovers_one_digest_per_conversation_day(ws, db):
    changes = discover(SlackAdapter(cfg()), db)
    assert len(changes) == 2
    by_day = {c.metadata["occurred_on"]: c for c in changes}
    older = by_day[min(by_day)]
    assert older.source_id == f"T1:C1:{min(by_day)}"
    assert older.action == ChangeAction.ADD
    assert older.title.startswith("#general - ")
    assert older.metadata["subpath"] == "contoso/general-C1"
    fm = older.metadata["front_matter"]
    assert fm["workspace"] == "Contoso" and fm["channel"] == "#general"
    assert fm["message_count"] == 2 and fm["participants"] == ["Alice A", "Bob B"]
    body = older.content_text
    assert "@Bob B" in body  # mention resolved
    assert "[the doc](https://example.com/doc)" in body
    assert "**Thanks** & welcome" in body
    assert "joined" not in body  # bookkeeping message skipped
    assert older.source_url.startswith("https://contoso.slack.com/archives/C1/p")


def test_only_read_only_api_methods_are_used(ws, db):
    ws.conversations.append({"id": "D1", "is_im": True, "user": "U1"})
    ws.messages["D1"] = [msg(slack_ts(0, 9), "U1", "hello")]
    discover(SlackAdapter(cfg()), db)
    assert set(ws.methods()) <= READ_ONLY
    assert "users.conversations" in ws.methods()


def test_dm_and_group_dm_titles(ws, db):
    ws.conversations = [
        {"id": "D1", "is_im": True, "user": "U1"},
        {"id": "G1", "is_mpim": True, "name": "mpdm-alice--bob--me-1"},
        channel("C2", "secret", is_private=True),
    ]
    for cid in ("D1", "G1", "C2"):
        ws.messages[cid] = [msg(slack_ts(0, 9), "U1", "hi")]
    titles = {c.metadata["front_matter"]["channel"] for c in discover(SlackAdapter(cfg()), db)}
    assert titles == {"DM with Alice A", "Group DM: alice, bob, me", "#secret"}


def test_conversation_types_are_requested(ws, db):
    discover(SlackAdapter(cfg(conversation_types=["im", "mpim"])), db)
    params = next(p for m, p in ws.requests if m == "users.conversations")
    assert params["types"] == "im,mpim" and params["exclude_archived"] == "true"


def test_include_and_exclude_channel_filters(ws, db):
    ws.conversations = [
        channel("C1", "general"),
        channel("C2", "random"),
        channel("C3", "ops-alerts"),
    ]
    for cid in ("C1", "C2", "C3"):
        ws.messages[cid] = [msg(slack_ts(0, 9), "U1", "x")]

    def names(**kw):
        return {
            c.metadata["front_matter"]["channel"] for c in discover(SlackAdapter(cfg(**kw)), db)
        }

    assert names(include_channels=["gen*", "ops-*"]) == {"#general", "#ops-alerts"}
    assert names(exclude_channels=["ops-*"]) == {"#general", "#random"}


def test_thread_replies_are_included_with_context(ws, db):
    parent = msg(
        slack_ts(0, 9, seq=10),
        "U1",
        "Should we ship Friday?",
        thread_ts=slack_ts(0, 9, seq=10),
        reply_count=1,
    )
    reply = msg(slack_ts(0, 10, seq=11), "U2", "Yes, ship it", thread_ts=parent["ts"])
    ws.messages["C1"] = [parent, reply]
    (change,) = discover(SlackAdapter(cfg()), db)
    assert "Yes, ship it" in change.content_text
    assert (
        "thread reply to" in change.content_text and "Should we ship Friday?" in change.content_text
    )
    assert "conversations.replies" in ws.methods()
    ws.requests.clear()
    discover(SlackAdapter(cfg(include_threads=False)), db)
    assert "conversations.replies" not in ws.methods()


def test_history_paging(ws, db):
    ws.history_page_size = 2
    ws.messages["C1"] = [msg(slack_ts(0, 8, m, seq=m + 1), "U1", f"m{m}") for m in range(5)]
    (change,) = discover(SlackAdapter(cfg()), db)
    assert all(f"m{m}" in change.content_text for m in range(5))


def test_window_limits_history(ws, db):
    ws.messages["C1"] = [
        msg(slack_ts(30, 9), "U1", "ancient"),
        msg(slack_ts(0, 9), "U1", "recent"),
    ]
    (change,) = discover(SlackAdapter(cfg(since_days=3)), db)
    assert "recent" in change.content_text
    assert "ancient" not in change.content_text


def test_attachment_file_bot_and_empty_messages(ws, db):
    ws.messages["C1"] = [
        msg(slack_ts(0, 8, seq=1), "U1", "", files=[{"id": "F1", "name": "design.png"}]),
        {
            "ts": slack_ts(0, 8, 1, seq=2),
            "bot_id": "B1",
            "username": "deploybot",
            "text": "",
            "attachments": [{"text": "Deployed v2"}],
        },
        msg(slack_ts(0, 8, 2, seq=3), "U1", ""),  # nothing to show
    ]
    (change,) = discover(SlackAdapter(cfg()), db)
    assert "design.png" in change.content_text
    assert "deploybot" in change.content_text and "Deployed v2" in change.content_text
    assert change.metadata["front_matter"]["message_count"] == 2


def test_skippable_errors_are_ignored_but_other_errors_surface(ws, db, caplog):
    ws.conversations = [channel("C1", "general"), channel("C2", "gone")]
    ws.messages["C2"] = [msg(slack_ts(0, 9), "U1", "x")]
    ws.errors["C2"] = "not_in_channel"
    changes = discover(SlackAdapter(cfg()), db)
    assert {c.source_id.split(":")[1] for c in changes} == {"C1"}

    ws.errors["C2"] = "fatal_error"  # one bad conversation does not sink the run
    changes = discover(SlackAdapter(cfg()), db)
    assert {c.source_id.split(":")[1] for c in changes} == {"C1"}
    assert "failed" in caplog.text


def test_run_fails_when_every_conversation_fails(ws, db):
    ws.errors["C1"] = "fatal_error"
    with pytest.raises(RuntimeError, match="all 1 Slack conversations failed"):
        discover(SlackAdapter(cfg()), db)


def test_rejected_credentials_trigger_one_session_refresh(ws, db):
    ws.auth_fail_calls = 1
    changes = discover(SlackAdapter(cfg()), db)
    assert len(changes) == 2
    assert ws.session_requests == [False, True]


def test_persistent_auth_failure_propagates(ws, db):
    ws.token = "xoxc-rotated"  # server now rejects the session's token
    with pytest.raises(SlackAuthError):
        discover(SlackAdapter(cfg()), db)
    assert ws.session_requests == [False, True]


def test_rate_limit_is_retried(ws, db):
    ws.rate_limit_once = 2
    assert len(discover(SlackAdapter(cfg()), db)) == 2


def test_no_matching_workspace_is_an_error(ws, db):
    with pytest.raises(RuntimeError, match="no Slack workspace matched"):
        discover(SlackAdapter(cfg(workspaces=["nothing-like-this"])), db)


def test_max_channels_cap(ws, db):
    ws.conversations = [channel(f"C{i}", f"chan{i}") for i in range(5)]
    for i in range(5):
        ws.messages[f"C{i}"] = [msg(slack_ts(0, 9), "U1", "x")]
    assert len(discover(SlackAdapter(cfg(max_channels=2)), db)) == 2


def test_adapter_properties(ws):
    adapter = SlackAdapter(cfg())
    assert adapter.source_type == SourceType.SLACK
    assert adapter.name == "slack"
    assert adapter.reconcile_supported() is False
    assert adapter.get_current_ids() == set()
    assert adapter.retention_cutoff() is not None
    assert adapter.validate() == []


def test_client_refuses_non_slack_hosts():
    for url in ("https://evil.example.com", "http://x.slack.com", "https://u:p@x.slack.com"):
        team = SlackTeam("T1", "n", "d", url, "xoxc-1")
        with pytest.raises(ValueError, match="refusing"):
            SlackClient(team, "d=x")


def test_client_error_mapping(ws):
    client = SlackClient(ws.team, "d=test-d-cookie", client=ws.client())
    assert client.call("auth.test")["ok"]
    with pytest.raises(SlackApiError) as info:
        client.call("users.info", user="NOPE")
    assert info.value.error == "user_not_found" and not isinstance(info.value, SlackAuthError)
    with pytest.raises(SlackApiError, match="unknown_method"):
        client.call("chat.postMessage", channel="C1", text="never")
    bad = SlackClient(ws.team, "d=wrong", client=ws.client())
    with pytest.raises(SlackAuthError):
        bad.call("auth.test")


def _client_for(responses):
    import httpx

    from tests.fake_web import _REAL_CLIENT

    seq = iter(responses)
    team = SlackTeam("T1", "n", "d", "https://corp.slack.com", "xoxc-1")
    transport = httpx.MockTransport(lambda request: next(seq))
    return SlackClient(team, "d=x", client=_REAL_CLIENT(transport=transport))


def test_client_survives_transient_server_errors_and_bad_json(monkeypatch):
    import httpx

    from workctx.sources import slack as sl

    monkeypatch.setattr(sl.time, "sleep", lambda _s: None)
    client = _client_for(
        [
            httpx.Response(502),
            httpx.Response(200, text="<html>not json</html>"),
            httpx.Response(200, json={"ok": False, "error": "ratelimited"}),
            httpx.Response(200, json={"ok": True, "x": 1}),
        ]
    )
    assert client.call("auth.test")["x"] == 1


def test_client_http_auth_failures_and_bad_shapes(monkeypatch):
    import httpx

    from workctx.sources import slack as sl

    monkeypatch.setattr(sl.time, "sleep", lambda _s: None)
    with pytest.raises(SlackAuthError):
        _client_for([httpx.Response(302)]).call("auth.test")
    with pytest.raises(SlackAuthError):
        _client_for([httpx.Response(403)]).call("auth.test")
    with pytest.raises(SlackApiError, match="bad_response"):
        _client_for([httpx.Response(200, json=["nope"])]).call("auth.test")
    with pytest.raises(SlackApiError, match="retries_exhausted"):
        _client_for([httpx.Response(500)] * 5).call("auth.test")


def test_client_paged_follows_cursors():
    import httpx

    client = _client_for(
        [
            httpx.Response(
                200, json={"ok": True, "items": [1], "response_metadata": {"next_cursor": "c2"}}
            ),
            httpx.Response(200, json={"ok": True, "items": [2], "response_metadata": {}}),
        ]
    )
    assert client.paged("x.list", "items") == [1, 2]
