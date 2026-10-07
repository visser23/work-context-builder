"""End-to-end sync of mail, calendar and Slack through the real pipeline (fake services)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from tests.fake_web import days_ago, slack_ts
from workctx.config import load_config
from workctx.corpus import build_output_path
from workctx.indexing import SearchIndex
from workctx.models import RunStatus, SourceType
from workctx.sources.outlook import CalendarAdapter, MailAdapter
from workctx.state import StateDB
from workctx.sync import _reconcile_source, run_sync

CLIENT_URL = "https://app.slack.com/client/E1/"


def make_project(tmp_path, *, since_days=30, past_days=30):
    cfg = {
        "version": 1,
        "project": {
            "id": "ctx",
            "name": "Context Test",
            "output_root": str(tmp_path / "out"),
            "state_dir": str(tmp_path / "state"),
        },
        "sources": {
            "mail": [
                {
                    "name": "mail",
                    "profile": "p",
                    "api_base": "https://outlook.example.com/api/v2.0",
                    "since_days": since_days,
                }
            ],
            "calendar": [
                {
                    "name": "cal",
                    "profile": "p",
                    "api_base": "https://outlook.example.com/api/v2.0",
                    "past_days": past_days,
                }
            ],
            "slack": [{"name": "chat", "profile": "p", "client_url": CLIENT_URL, "since_days": 7}],
        },
        "notifications": {"telegram": {"enabled": False}, "macos": {"enabled": False}},
    }
    path = tmp_path / "workctx.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return {
        "config": load_config(path),
        "path": path,
        "out": tmp_path / "out",
        "state": tmp_path / "state",
        "tmp": tmp_path,
    }


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def run(project, *names, config=None):
    return run_sync(
        config or project["config"], run_id="t", quiet=True, only_sources=frozenset(names)
    )


def files(out, kind):
    return sorted((out / kind).rglob("*.md"))


def reconcile(project, source, config=None):
    """Run reconciliation now (``run_sync`` only does so every ``reconciliation_days``)."""
    db = StateDB(project["state"] / "state.sqlite")
    idx = SearchIndex(project["state"] / "state.sqlite")
    try:
        _reconcile_source(source, db, idx, project["out"])
        return db.get_all_source_ids(source.name)
    finally:
        source.close()
        db.close()
        idx.close()


def front_matter(path):
    text = path.read_text()
    return yaml.safe_load(text.split("\n---\n")[0].strip("-\n"))


def search(project, query):
    idx = SearchIndex(project["state"] / "state.sqlite")
    try:
        return idx.search(query, limit=10)
    finally:
        idx.close()


# ------------------------------------------------------------------- mail


def test_mail_sync_writes_indexes_and_is_idempotent(outlook, project):
    outlook.add_message("m1", "Budget review", days_ago(1), body="Approve the zanzibar budget.")
    outlook.add_message("m2", "Sent reply", days_ago(2), folder="sentitems")

    result = run(project, "mail")
    assert result.status == RunStatus.HEALTHY
    assert (result.source_results[0].objects_added, result.source_results[0].objects_failed) == (
        2,
        0,
    )

    paths = files(project["out"], "email")
    assert len(paths) == 2
    day = days_ago(1).strftime("%Y-%m-%d")
    budget = next(p for p in paths if "budget-review" in p.name)
    assert budget.parent.parent.name == day[:4] and budget.name.startswith(day)
    meta = front_matter(budget)
    assert meta["source_type"] == "email" and meta["sender"] == "Alice Example"
    assert meta["folder"] == "Inbox" and meta["participants"] == ["Bob Example"]
    assert "Approve the zanzibar budget." in budget.read_text()

    hits = search(project, "zanzibar")
    assert [h["source_type"] for h in hits] == ["email"]

    again = run(project, "mail").source_results[0]
    assert (again.objects_added, again.objects_updated, again.objects_deleted) == (0, 0, 0)
    assert outlook.write_requests() == []


def test_new_mail_is_picked_up_incrementally(outlook, project):
    outlook.add_message("m1", "First", days_ago(1))
    run(project, "mail")
    outlook.add_message("m2", "Second", days_ago(0.5))
    second = run(project, "mail").source_results[0]
    assert (second.objects_added, second.objects_updated) == (1, 0)
    assert len(files(project["out"], "email")) == 2


def test_deleted_mail_is_removed_but_aged_out_mail_is_kept(outlook, tmp_path):
    wide = make_project(tmp_path, since_days=30)
    outlook.add_message("recent", "Recent one", days_ago(2))
    outlook.add_message("older", "Older one", days_ago(20))
    outlook.add_message("doomed", "Deleted later", days_ago(3))
    run(wide, "mail")
    assert len(files(wide["out"], "email")) == 3

    del outlook.messages["doomed"]  # deleted server-side, inside the window
    del outlook.messages["older"]  # no longer returned...
    narrow_cfg = yaml.safe_load(wide["path"].read_text())
    narrow_cfg["sources"]["mail"][0]["since_days"] = 7  # ...because the window shrank
    wide["path"].write_text(yaml.safe_dump(narrow_cfg))
    narrow = load_config(wide["path"])

    remaining = reconcile(wide, MailAdapter(narrow.sources.mail[0]))
    assert remaining == {"recent", "older"}, "aged-out mail retained, deleted mail removed"
    names = [p.name for p in files(wide["out"], "email")]
    assert any("older-one" in n for n in names)
    assert not any("deleted-later" in n for n in names)
    assert search(wide, "Older one"), "retained mail stays searchable"
    assert not [h for h in search(wide, "Deleted later") if "deleted-later" in h["output_path"]]


def test_expired_session_fails_the_source_without_wiping_data(outlook, project):
    outlook.add_message("m1", "Keep me", days_ago(1))
    run(project, "mail")
    outlook.valid_token = "rotated"
    outlook.always_stale = True
    result = run(project, "mail")
    assert result.status != RunStatus.HEALTHY
    assert len(files(project["out"], "email")) == 1
    outlook.always_stale = False
    outlook.provider_token = "rotated"
    assert run(project, "mail").status == RunStatus.HEALTHY


# ---------------------------------------------------------------- calendar


def test_calendar_sync_update_and_removal(outlook, project):
    outlook.add_event("e1", "Planning", days_ago(1), body="Discuss quokka roadmap")
    outlook.add_event("e2", "Upcoming review", days_ago(-3))
    first = run(project, "cal").source_results[0]
    assert first.objects_added == 2
    paths = files(project["out"], "calendar")
    planning = next(p for p in paths if "planning" in p.name)
    meta = front_matter(planning)
    assert meta["source_type"] == "calendar" and meta["location"] == "Room 1"
    assert meta["organizer"] == "Alice Example"
    assert search(project, "quokka")

    assert run(project, "cal").source_results[0].objects_updated == 0
    outlook.events["e1"]["ChangeKey"] = "ck2"
    outlook.events["e1"]["_body"] = "Revised agenda with giraffe topic"
    assert run(project, "cal").source_results[0].objects_updated == 1
    assert "giraffe" in planning.read_text()

    outlook.events["e2"]["IsCancelled"] = True  # cancelled -> not current -> removed
    remaining = reconcile(project, CalendarAdapter(project["config"].sources.calendar[0]))
    assert remaining == {"e1"}
    assert len(files(project["out"], "calendar")) == 1


def test_past_events_outside_window_are_retained(outlook, tmp_path):
    wide = make_project(tmp_path, past_days=60)
    outlook.add_event("old", "Old meeting", days_ago(40))
    run(wide, "cal")
    del outlook.events["old"]
    cfg = yaml.safe_load(wide["path"].read_text())
    cfg["sources"]["calendar"][0]["past_days"] = 10
    wide["path"].write_text(yaml.safe_dump(cfg))
    narrow = load_config(wide["path"])
    assert reconcile(wide, CalendarAdapter(narrow.sources.calendar[0])) == {"old"}
    assert len(files(wide["out"], "calendar")) == 1


# ------------------------------------------------------------------- slack


@pytest.fixture
def chat(slack_api):
    slack_api.users = {"U1": {"name": "alice", "profile": {"display_name": "Alice A"}}}
    slack_api.conversations = [{"id": "C1", "name": "general", "is_channel": True}]
    slack_api.messages["C1"] = [
        {"ts": slack_ts(1, 9, seq=1), "user": "U1", "text": "Yesterday's okapi plan"},
        {"ts": slack_ts(0, 8, seq=2), "user": "U1", "text": "Today's update"},
    ]
    return slack_api


def test_slack_sync_layout_search_and_updates(chat, project):
    result = run(project, "chat")
    assert result.status == RunStatus.HEALTHY
    assert result.source_results[0].objects_added == 2

    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    path = project["out"] / "slack" / "chat" / "contoso" / "general-c1" / f"{yesterday}.md"
    assert path.exists()
    meta = front_matter(path)
    assert meta["source_type"] == "slack" and meta["channel"] == "#general"
    assert meta["workspace"] == "Contoso" and meta["message_count"] == 1
    assert search(project, "okapi")

    quiet = run(project, "chat").source_results[0]
    assert (quiet.objects_added, quiet.objects_updated) == (0, 0)

    chat.messages["C1"].append({"ts": slack_ts(0, 9, seq=3), "user": "U1", "text": "Late addition"})
    updated = run(project, "chat").source_results[0]
    assert (updated.objects_added, updated.objects_updated) == (0, 1)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert "Late addition" in (path.parent / f"{today}.md").read_text()

    chat.messages["C1"][1]["text"] = "Edited text"
    assert run(project, "chat").source_results[0].objects_updated == 1


def test_slack_history_is_never_deleted_by_reconcile(chat, project):
    run(project, "chat")
    chat.messages["C1"] = []
    chat.conversations = []
    from workctx.sources.slack import SlackAdapter

    remaining = reconcile(project, SlackAdapter(project["config"].sources.slack[0]))
    assert len(remaining) == 2
    assert len(files(project["out"], "slack")) == 2


def test_slack_session_refresh_end_to_end(chat, project):
    chat.auth_fail_calls = 1
    assert run(project, "chat").status == RunStatus.HEALTHY
    assert chat.session_requests == [False, True]


# --------------------------------------------------- everything together


def test_all_sources_together_update_corpus_docs_and_manifest(outlook, chat, project):
    outlook.add_message("m1", "Mail subject", days_ago(1))
    outlook.add_event("e1", "Meeting", days_ago(1))
    result = run_sync(project["config"], run_id="t", quiet=True)
    assert result.status == RunStatus.HEALTHY
    assert {r.source_name for r in result.source_results} == {"mail", "cal", "chat"}

    out = project["out"]
    index_md = (out / "_meta" / "INDEX.md").read_text()
    assert "### Email: mail" in index_md and "### Calendar: cal" in index_md
    assert "### Slack: chat" in index_md
    brief = (out / "PROJECT_BRIEF.md").read_text()
    assert "**Email** (mail): 1 messages" in brief and "**Slack** (chat)" in brief
    assert "email/" in (out / "CLAUDE.md").read_text()
    assert "Slack (chat)" in (out / "CHATGPT_INSTRUCTIONS.md").read_text()
    manifest = [
        json.loads(line) for line in (out / "_meta" / "manifest.jsonl").read_text().splitlines()
    ]
    assert {m["source_type"] for m in manifest} == {"email", "calendar", "slack"}

    db = StateDB(project["state"] / "state.sqlite")
    try:
        assert db.count_objects("mail") == 1 and db.count_objects("chat") == 2
    finally:
        db.close()


def test_one_failing_source_does_not_block_the_others(outlook, chat, project):
    outlook.add_message("m1", "Mail subject", days_ago(1))
    chat.token = "xoxc-rotated"  # Slack now rejects us
    result = run_sync(project["config"], run_id="t", quiet=True)
    by_name = {r.source_name: r for r in result.source_results}
    assert by_name["mail"].objects_added == 1
    assert result.status != RunStatus.HEALTHY
    assert files(project["out"], "email")


# ----------------------------------------------------------- output paths


def test_build_output_path_for_new_source_types():
    email = build_output_path(
        SourceType.EMAIL, "mail", "AAMk/1=", title="Re: Hello, World!", occurred_on="2026-03-09"
    )
    assert email.startswith("email/mail/2026/03/2026-03-09-re-hello-world-")
    assert email.endswith(".md") and ".." not in email
    undated = build_output_path(SourceType.CALENDAR, "cal", "e1", title="Meeting")
    assert undated.startswith("calendar/cal/undated/meeting-")
    slack = build_output_path(
        SourceType.SLACK,
        "chat",
        "T1:C1:2026-03-09",
        title="t",
        occurred_on="2026-03-09",
        subpath="Contoso/../General-C1",
    )
    assert slack == "slack/chat/contoso/x/general-c1/2026-03-09.md"  # ".." can never survive
    hostile = build_output_path(
        SourceType.SLACK, "chat", "id", title="t", occurred_on="../../etc", subpath="../../x"
    )
    assert hostile.startswith("slack/chat/") and ".." not in hostile
    assert build_output_path(SourceType.SLACK, "chat", "id", title="t").startswith(
        "slack/chat/undated-"
    )


def test_distinct_ids_never_collide_on_path():
    a = build_output_path(SourceType.EMAIL, "m", "id-1", title="Same", occurred_on="2026-01-01")
    b = build_output_path(SourceType.EMAIL, "m", "id-2", title="Same", occurred_on="2026-01-01")
    assert a != b
