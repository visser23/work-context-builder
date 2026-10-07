"""Pure-function tests for the Slack Markdown normaliser."""

from __future__ import annotations

from workctx.normalise import slack as s

USERS = {"U1": "Alice", "U2": "Bob"}


def resolve(uid):
    return USERS.get(uid, uid)


def test_ts_helpers_use_utc():
    assert s.ts_to_day("1767614400.000100") == "2026-01-05"
    assert s.ts_to_datetime("1767614400.0").hour == 12


def test_mentioned_user_ids():
    assert s.mentioned_user_ids("hi <@U1> and <@W2|bob> <#C1|x>") == {"U1", "W2"}
    assert s.mentioned_user_ids("") == set()


def test_convert_mrkdwn_mentions_links_and_commands():
    text = (
        "<@U1> see <https://example.com/a|the doc> or <https://example.com/b> "
        "cc <!here> <#C123|general>"
    )
    out = s.convert_mrkdwn(text, resolve)
    assert "@Alice" in out
    assert "[the doc](https://example.com/a)" in out
    assert "https://example.com/b" in out
    assert "@here" in out
    assert "#general" in out


def test_convert_mrkdwn_channel_without_label_uses_resolver():
    out = s.convert_mrkdwn("<#C9>", resolve, lambda cid: "random")
    assert out == "#random"


def test_convert_mrkdwn_subteam_date_mailto_and_unknown():
    assert s.convert_mrkdwn("<!subteam^S1|@devs>", resolve) == "@devs"
    assert s.convert_mrkdwn("<!date^1^{date}|Jan 5>", resolve) == "Jan 5"
    assert s.convert_mrkdwn("<mailto:a@example.com|a@example.com>", resolve) == "a@example.com"
    assert s.convert_mrkdwn("<something odd>", resolve) == "<something odd>"


def test_convert_mrkdwn_formatting_and_entities():
    out = s.convert_mrkdwn("*bold* ~gone~ a &amp; b &lt;tag&gt; 2*3*4", resolve)
    assert "**bold**" in out
    assert "~~gone~~" in out
    assert "a & b <tag>" in out
    assert "2*3*4" in out  # not emphasis


def test_fingerprint_changes_with_edits_replies_and_reactions():
    base = {"ts": "1.0", "text": "hello"}
    fp = s.message_fingerprint(base)
    assert fp == s.message_fingerprint(dict(base))
    assert fp != s.message_fingerprint({**base, "text": "hello!"})
    assert fp != s.message_fingerprint({**base, "edited": {"ts": "2.0"}})
    assert fp != s.message_fingerprint({**base, "reply_count": 2})
    assert fp != s.message_fingerprint({**base, "reactions": [{"name": "+1", "count": 1}]})


def test_version_for_is_order_independent_and_counts():
    a = s.version_for(["x", "y"])
    assert a == s.version_for(["y", "x"])
    assert a.startswith("2:")
    assert a != s.version_for(["x", "z"])


def test_format_message_variants():
    plain = s.SlackMessage(ts="1767614400.0", user="Alice", text="hi")
    assert s.format_message(plain) == "**[12:00] Alice**: hi"
    rich = s.SlackMessage(
        ts="1767614400.0",
        user="Bot",
        text="line1\nline2",
        is_reply=True,
        thread_preview="the topic",
        files=["a.png"],
        reactions=[("tada", 2)],
        edited=True,
        bot=True,
    )
    out = s.format_message(rich)
    assert "_(app)_" in out and 'thread reply to "the topic"' in out
    assert "\n  line2" in out
    assert "files: a.png" in out and ":tada: x2" in out and "edited" in out


def test_render_day_markdown_orders_messages_and_lists_participants():
    msgs = [
        s.SlackMessage(ts="1767614500.0", user="Bob", text="second"),
        s.SlackMessage(ts="1767614400.0", user="Alice", text="first"),
    ]
    md = s.render_day_markdown(
        conversation="#general", workspace="Contoso", day="2026-01-05", messages=msgs
    )
    assert md.startswith("# #general - 2026-01-05")
    assert md.index("first") < md.index("second")
    assert "**Participants**: Alice, Bob" in md and "**Messages**: 2" in md


def test_thread_preview_truncates():
    out = s.thread_preview("word " * 40)
    assert len(out) <= 60 and out.endswith("…")
