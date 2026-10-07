"""Outlook client and mail/calendar adapter tests against a fake REST API."""

from __future__ import annotations

import pytest

from tests.fake_web import OUTLOOK_HOST, days_ago
from workctx.auth.webtokens import WebSessionExpiredError, WebToken
from workctx.config import CalendarSource, MailSource
from workctx.models import ChangeAction, SourceType
from workctx.sources.outlook import CalendarAdapter, MailAdapter, OutlookClient
from workctx.state import StateDB

API = f"https://{OUTLOOK_HOST}/api/v2.0"


def mail_cfg(**kw):
    return MailSource(name="mail", profile="p", api_base=API, **kw)


def cal_cfg(**kw):
    return CalendarSource(name="cal", profile="p", api_base=API, **kw)


@pytest.fixture
def db(tmp_path):
    database = StateDB(tmp_path / "state.sqlite")
    yield database
    database.close()


# ---------------------------------------------------------------- client


def test_client_refreshes_token_once_on_401(outlook):
    outlook.provider_token = "stale"
    client = OutlookClient(profile="p", mailbox_url="https://x", api_base=API)
    assert client.get_json("/me")["DisplayName"] == "Bob Example"
    assert outlook.token_requests == [False, True]


def test_client_gives_up_when_session_is_really_dead(outlook):
    outlook.provider_token = "stale"
    outlook.always_stale = True
    client = OutlookClient(profile="p", mailbox_url="https://x", api_base=API)
    with pytest.raises(WebSessionExpiredError, match="login-web"):
        client.get("/me")
    assert outlook.token_requests == [False, True]


def test_client_backs_off_on_throttling(outlook):
    outlook.throttle_once = 2
    client = OutlookClient(profile="p", mailbox_url="https://x", api_base=API)
    assert client.get_json("/me")["DisplayName"]
    assert len(outlook.requests) == 3


def test_client_refuses_foreign_hosts_and_insecure_base(outlook):
    client = OutlookClient(profile="p", mailbox_url="https://x", api_base=API)
    with pytest.raises(ValueError, match="unrelated host"):
        client.get("https://evil.example.net/steal")
    assert outlook.requests == []
    for bad in (
        "http://outlook.example.com/api",
        "https://u:p@outlook.example.com/api",
        "nonsense",
    ):
        with pytest.raises(ValueError):
            OutlookClient(profile="p", mailbox_url="https://x", api_base=bad)


def test_client_follows_paging_and_limits(outlook):
    outlook.page_size = 2
    for i in range(5):
        outlook.add_message(f"m{i}", f"Subject {i}", days_ago(i))
    client = OutlookClient(profile="p", mailbox_url="https://x", api_base=API)
    items = list(client.pages("/me/mailfolders/inbox/messages", {"$top": "2"}))
    assert [i["Id"] for i in items] == ["m0", "m1", "m2", "m3", "m4"]
    assert len(list(client.pages("/me/mailfolders/inbox/messages", {}, max_items=3))) == 3


def test_client_get_json_raises_on_error_status(outlook):
    client = OutlookClient(profile="p", mailbox_url="https://x", api_base=API)
    with pytest.raises(RuntimeError, match="HTTP 404"):
        client.get_json("/me/messages/nope")


def test_cached_token_is_reused(outlook, monkeypatch):
    client = OutlookClient(profile="p", mailbox_url="https://x", api_base=API)
    client.get("/me")
    client.get("/me")
    assert outlook.token_requests == [False]
    assert isinstance(client._token, WebToken)


# ---------------------------------------------------------------- mail


def test_mail_discovery_and_render(outlook, db):
    outlook.add_message("m1", "Budget review", days_ago(1), body="Please approve the budget today.")
    outlook.add_message("m2", "Sent note", days_ago(2), folder="sentitems")
    outlook.add_message("old", "Ancient", days_ago(90))
    outlook.add_message("draft", "WIP", days_ago(1), IsDraft=True)
    adapter = MailAdapter(mail_cfg(since_days=30))
    changes = adapter.discover_changes(db, None)

    by_id = {c.source_id: c for c in changes}
    assert set(by_id) == {"m1", "m2"}
    first = by_id["m1"]
    assert first.action == ChangeAction.ADD
    assert first.metadata["occurred_on"] == days_ago(1).strftime("%Y-%m-%d")
    assert first.metadata["front_matter"]["participants"] == ["Bob Example"]
    assert by_id["m2"].metadata["folder"] == "Sent Items"
    assert adapter.source_type == SourceType.EMAIL

    markdown = adapter.render_content(first)
    assert "Please approve the budget today." in markdown and "# Budget review" in markdown
    assert adapter.get_current_ids() == {"m1", "m2"}


def test_mail_filters_exclude_senders_and_subjects(outlook, db):
    outlook.add_message("keep", "Real", days_ago(1))
    outlook.add_message("spam", "Hi", days_ago(1), sender=("News", "newsletter@example.com"))
    outlook.add_message("auto", "Automatic reply: away", days_ago(1))
    adapter = MailAdapter(
        mail_cfg(exclude_senders=["newsletter@*"], exclude_subjects=["automatic reply*"])
    )
    assert {c.source_id for c in adapter.discover_changes(db, None)} == {"keep"}
    assert adapter.get_current_ids() == {"keep"}


def test_mail_trims_quoted_history_and_truncates(outlook, db):
    body = (
        "Looks good, please go ahead with the plan as discussed earlier today.\n\n"
        "On Mon, 5 Jan 2026 at 10:00, Alice <a@example.com> wrote:\n> old stuff\n"
    )
    outlook.add_message("m1", "Re: plan", days_ago(1), body=body)
    adapter = MailAdapter(mail_cfg())
    (change,) = adapter.discover_changes(db, None)
    md = adapter.render_content(change)
    assert "old stuff" not in md and "trimmed" in md
    keep = MailAdapter(mail_cfg(trim_quoted_replies=False))
    assert "old stuff" in keep.render_content(change)
    outlook.messages["m1"]["_body"] = "word " * 400
    short = MailAdapter(mail_cfg(max_body_chars=500))
    assert "Truncated" in short.render_content(change)


def test_mail_attachments_listed_and_tolerates_failure(outlook, db):
    outlook.add_message("m1", "With file", days_ago(1), HasAttachments=True)
    outlook.attachments["m1"] = [
        {"Name": "plan.pdf", "Size": 4096, "ContentType": "application/pdf"}
    ]
    adapter = MailAdapter(mail_cfg())
    (change,) = adapter.discover_changes(db, None)
    assert "plan.pdf" in adapter.render_content(change)
    outlook.fail_attachments = True
    assert "Attachments**: yes" in adapter.render_content(change)


def test_mail_custom_folder_resolution(outlook, db):
    outlook.custom_folders["Projects"] = "folder-projects"
    outlook.add_message("m1", "In projects", days_ago(1), folder="folder-projects")
    adapter = MailAdapter(mail_cfg(folders=["Projects"]))
    assert [c.source_id for c in adapter.discover_changes(db, None)] == ["m1"]
    missing = MailAdapter(mail_cfg(folders=["Nope"]))
    with pytest.raises(RuntimeError, match="Nope"):
        missing.discover_changes(db, None)


def test_mail_max_messages_cap(outlook, db):
    for i in range(6):
        outlook.add_message(f"m{i}", f"S{i}", days_ago(i * 0.1 + 0.5))
    adapter = MailAdapter(mail_cfg(max_messages=3))
    assert len(adapter.discover_changes(db, None)) == 3


def test_mail_retention_cutoff_matches_window(outlook):
    from datetime import UTC, datetime, timedelta

    cutoff = MailAdapter(mail_cfg(since_days=10)).retention_cutoff()
    assert abs(cutoff - (datetime.now(UTC) - timedelta(days=10))) < timedelta(seconds=5)


def test_mail_config_error_is_reported_by_validate():
    adapter = MailAdapter(MailSource(name="m", profile="p", api_base="http://insecure.example.com"))
    assert adapter.validate()
    with pytest.raises(RuntimeError):
        adapter.get_current_ids()


def test_mail_only_issues_get_requests(outlook, db):
    outlook.add_message("m1", "A", days_ago(1), HasAttachments=True)
    adapter = MailAdapter(mail_cfg())
    (change,) = adapter.discover_changes(db, None)
    adapter.render_content(change)
    adapter.get_current_ids()
    assert outlook.write_requests() == []
    assert all(r.url.host == OUTLOOK_HOST for r in outlook.requests)


# ---------------------------------------------------------------- calendar


def test_calendar_discovery_filters_and_renders(outlook, db):
    outlook.add_event("e1", "Planning", days_ago(1))
    outlook.add_event("e2", "Future review", days_ago(-5))
    outlook.add_event("e3", "Cancelled one", days_ago(1), IsCancelled=True)
    outlook.add_event("e4", "Lunch", days_ago(1))
    outlook.add_event("e5", "Way back", days_ago(100))
    adapter = CalendarAdapter(cal_cfg(exclude_titles=["lunch"]))
    changes = adapter.discover_changes(db, None)
    assert {c.source_id for c in changes} == {"e1", "e2"}
    planning = next(c for c in changes if c.source_id == "e1")
    assert planning.source_version == "ck:ck1"
    assert planning.metadata["front_matter"]["location"] == "Room 1"
    assert planning.metadata["front_matter"]["organizer"] == "Alice Example"
    md = adapter.render_content(planning)
    assert "# Planning" in md and "Agenda: discuss things" in md and "Bob Example (accepted)" in md
    assert adapter.get_current_ids() == {"e1", "e2"}
    assert adapter.source_type == SourceType.CALENDAR

    with_cancelled = CalendarAdapter(cal_cfg(include_cancelled=True, exclude_titles=["lunch"]))
    assert {c.source_id for c in with_cancelled.discover_changes(db, None)} == {"e1", "e2", "e3"}


def test_calendar_skips_body_fetch_when_disabled(outlook, db):
    outlook.add_event("e1", "Planning", days_ago(1))
    adapter = CalendarAdapter(cal_cfg(max_body_chars=0))
    (change,) = adapter.discover_changes(db, None)
    before = len(outlook.requests)
    md = adapter.render_content(change)
    assert len(outlook.requests) == before and "## Details" not in md


def test_calendar_paging(outlook, db):
    outlook.page_size = 2
    for i in range(5):
        outlook.add_event(f"e{i}", f"Event {i}", days_ago(i + 1))
    assert len(CalendarAdapter(cal_cfg()).discover_changes(db, None)) == 5
