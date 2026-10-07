"""Pure-function tests for the Outlook mail/calendar Markdown normaliser."""

from __future__ import annotations

from workctx.normalise import outlook as o


def addr(name, address):
    return {"EmailAddress": {"Name": name, "Address": address}}


LONG_INTRO = "Thanks for the update, this looks good to me and I will proceed shortly.\n"


def test_trim_quoted_reply_cuts_on_wrote_marker():
    text = LONG_INTRO + "\nOn Mon, 5 Jan 2026 at 10:00, Alice <a@example.com> wrote:\n> old\n> text"
    out, trimmed = o.trim_quoted_reply(text)
    assert trimmed is True
    assert out == LONG_INTRO.strip()


def test_trim_quoted_reply_cuts_on_from_sent_header_block():
    text = (
        LONG_INTRO + "\nFrom: Alice <a@example.com>\nSent: Monday\nTo: Bob\nSubject: Re: x\n\nold"
    )
    out, trimmed = o.trim_quoted_reply(text)
    assert trimmed
    assert "Alice" not in out


def test_trim_quoted_reply_keeps_message_that_is_entirely_a_quote():
    text = "-----Original Message-----\nFrom: A\nSent: B\nbody of forward"
    out, trimmed = o.trim_quoted_reply(text)
    assert not trimmed
    assert "Original Message" in out


def test_trim_quoted_reply_lone_from_line_is_not_a_header():
    text = LONG_INTRO + "From: the team, with love\nthat is all."
    out, trimmed = o.trim_quoted_reply(text)
    assert not trimmed
    assert "that is all" in out


def test_trim_invite_boilerplate_drops_join_footer():
    text = "Agenda: roadmap\n\n" + "_" * 40 + "\nMicrosoft Teams meeting\nJoin: https://x"
    assert o.trim_invite_boilerplate(text) == "Agenda: roadmap"


def test_collapse_blank_lines():
    assert o.collapse_blank_lines("a  \n\n\n\nb\n") == "a\n\nb"


def test_limit_chars_truncates_with_note():
    assert o.limit_chars("short", 10) == "short"
    out = o.limit_chars("x" * 50, 10)
    assert out.startswith("x" * 10) and out.endswith(o.TRUNCATE_NOTE)


def test_parse_dt_variants():
    assert o.parse_dt("2026-01-05T10:00:00Z").hour == 10
    assert o.parse_dt("2026-01-05T10:00:00.0000000").tzinfo is not None
    assert o.parse_dt("2026-01-05T10:00:00+01:00").astimezone().utcoffset() is not None
    assert o.parse_dt(None) is None
    assert o.parse_dt("not a date") is None


def test_day_of_converts_to_utc():
    assert o.day_of("2026-01-05T23:30:00-02:00") == "2026-01-06"
    assert o.day_of("") is None


def test_helpers_one_line_address_size():
    assert o.one_line("  a\n b\t c ") == "a b c"
    assert o.format_address(addr("Alice A", "a@example.com")) == "Alice A <a@example.com>"
    assert o.format_address(addr("", "a@example.com")) == "a@example.com"
    assert o.format_address(None) == ""
    assert o.display_name(addr("", "a@example.com")) == "a@example.com"
    assert o.human_size(2048).startswith("2")
    assert o.human_size(None) == ""


def test_render_mail_markdown_full():
    message = {
        "Subject": "Quarterly\nplan",
        "From": addr("Alice", "a@example.com"),
        "ToRecipients": [addr("Bob", "b@example.com")],
        "CcRecipients": [addr("Cy", "c@example.com")],
        "ReceivedDateTime": "2026-01-05T10:00:00Z",
        "Importance": "High",
        "HasAttachments": True,
    }
    md = o.render_mail_markdown(
        message,
        "Body text",
        folder="Inbox",
        attachments=[{"Name": "plan.pdf", "Size": 2048}],
        trimmed=True,
    )
    assert md.startswith("# Quarterly plan\n")
    for expected in (
        "**From**: Alice <a@example.com>",
        "**To**: Bob <b@example.com>",
        "**Cc**: Cy <c@example.com>",
        "**Date**: 2026-01-05 10:00 UTC",
        "**Folder**: Inbox",
        "**Importance**: High",
        "plan.pdf",
        "Body text",
        o.TRIM_NOTE,
    ):
        assert expected in md


def test_render_mail_markdown_minimal_and_empty_body():
    md = o.render_mail_markdown({}, "", folder="Inbox")
    assert "(no subject)" in md and "_(empty message)_" in md
    assert "Attachments" not in md


def test_render_event_markdown():
    event = {
        "Subject": "Planning",
        "Start": {"DateTime": "2026-01-05T10:00:00.0000000"},
        "End": {"DateTime": "2026-01-05T11:00:00.0000000"},
        "Location": {"DisplayName": "Room 1"},
        "Organizer": addr("Alice", "a@example.com"),
        "Attendees": [
            {**addr("Bob", "b@example.com"), "Status": {"Response": "Accepted"}},
            {**addr("Cy", "c@example.com"), "Type": "Optional"},
        ],
        "OnlineMeeting": {"JoinUrl": "https://teams.example.com/join/1"},
        "ShowAs": "Tentative",
        "ResponseStatus": {"Response": "TentativelyAccepted"},
        "IsCancelled": True,
        "Type": "Occurrence",
        "Categories": ["Blue"],
    }
    md = o.render_event_markdown(event, "Agenda")
    for expected in (
        "# Planning",
        "Mon 05 Jan 2026 10:00 - 11:00 UTC",
        "**Where**: Room 1",
        "Bob (accepted)",
        "Cy (optional)",
        "https://teams.example.com/join/1",
        "**Shown as**: Tentative",
        "**Your response**: tentative",
        "**Status**: cancelled",
        "**Recurring**: yes",
        "Blue",
        "## Details",
        "Agenda",
    ):
        assert expected in md


def test_render_event_all_day_and_multi_day():
    all_day = o.render_event_markdown(
        {"Subject": "OOO", "IsAllDay": True, "Start": {"DateTime": "2026-01-05T00:00:00"}}, ""
    )
    assert "(all day)" in all_day
    span = o.render_event_markdown(
        {
            "Subject": "Offsite",
            "Start": {"DateTime": "2026-01-05T09:00:00"},
            "End": {"DateTime": "2026-01-06T17:00:00"},
        },
        "",
    )
    assert "2026-01-06 17:00 UTC" in span
