"""Render Outlook messages and calendar events as Markdown (pure functions)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

# A line that starts the quoted earlier part of a reply. Everything from the first
# such line (after some real content) is dropped: those messages have their own files.
_QUOTE_START_PATTERNS = (
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^_{10,}\s*$"),
    re.compile(r"^On .{5,200}\bwrote:\s*$", re.IGNORECASE),
    re.compile(r"^Le .{5,200}\bécrit\s*:\s*$", re.IGNORECASE),
    re.compile(r"^Am .{5,200}\bschrieb\b.*:\s*$", re.IGNORECASE),
)
_HEADER_FROM = re.compile(r"^(From|De|Von)\s*:\s*\S", re.IGNORECASE)
_HEADER_FOLLOW = re.compile(r"^(Sent|Date|To|Subject|Envoy|Gesendet)\s*:", re.IGNORECASE)
# Teams / Outlook invitations end their human text at a long rule line.
_INVITE_RULE = re.compile(r"^_{20,}\s*$")

MIN_KEEP_CHARS = 40
TRIM_NOTE = "_[Earlier messages in this thread trimmed - see the thread's other files.]_"
TRUNCATE_NOTE = "_[Truncated]_"


def _clean_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def collapse_blank_lines(text: str) -> str:
    """Strip trailing spaces and squeeze runs of blank lines to one."""
    out: list[str] = []
    blank = 0
    for line in _clean_lines(text):
        stripped = line.rstrip()
        if stripped:
            blank = 0
            out.append(stripped)
        else:
            blank += 1
            if blank == 1:
                out.append("")
    return "\n".join(out).strip()


def trim_quoted_reply(text: str) -> tuple[str, bool]:
    """Cut the quoted history from a reply. Returns ``(text, was_trimmed)``.

    Only cuts when real content precedes the quote marker, so a message that is
    entirely a forwarded body is left intact.
    """
    lines = _clean_lines(text)
    offset = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        marker = any(p.match(stripped) for p in _QUOTE_START_PATTERNS)
        if not marker and _HEADER_FROM.match(stripped):
            window = lines[i + 1 : i + 5]
            marker = any(_HEADER_FOLLOW.match(w.strip()) for w in window)
        if marker and offset >= MIN_KEEP_CHARS:
            return collapse_blank_lines("\n".join(lines[:i])), True
        offset += len(line) + 1
    return collapse_blank_lines(text), False


def trim_invite_boilerplate(text: str) -> str:
    """Drop the "Join the meeting" footer that Teams/Outlook append after a rule line."""
    lines = _clean_lines(text)
    for i, line in enumerate(lines):
        if _INVITE_RULE.match(line.strip()):
            return collapse_blank_lines("\n".join(lines[:i]))
    return collapse_blank_lines(text)


def limit_chars(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n" + TRUNCATE_NOTE


def parse_dt(value: str | None) -> datetime | None:
    """Parse an Outlook ISO timestamp (with or without ``Z`` / fractional seconds)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def day_of(value: str | None) -> str | None:
    parsed = parse_dt(value)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d") if parsed else None


def fmt_dt(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if value else "unknown"


def one_line(value: str | None) -> str:
    """Collapse whitespace so a value is safe inside a single Markdown/YAML line."""
    return " ".join((value or "").split())


def format_address(entry: dict[str, Any] | None) -> str:
    """``Name <addr>`` from an Outlook ``Recipient`` / ``Attendee`` object."""
    address = (entry or {}).get("EmailAddress") or {}
    name = one_line(address.get("Name"))
    addr = one_line(address.get("Address"))
    if name and addr and name.lower() != addr.lower():
        return f"{name} <{addr}>"
    return addr or name


def display_name(entry: dict[str, Any] | None) -> str:
    address = (entry or {}).get("EmailAddress") or {}
    return one_line(address.get("Name")) or one_line(address.get("Address"))


def human_size(size: int | None) -> str:
    if not isinstance(size, int) or size < 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def render_mail_markdown(
    message: dict[str, Any],
    body: str,
    *,
    folder: str,
    attachments: list[dict[str, Any]] | None = None,
    trimmed: bool = False,
) -> str:
    """Markdown for one email."""
    subject = one_line(message.get("Subject")) or "(no subject)"
    received = parse_dt(message.get("ReceivedDateTime") or message.get("SentDateTime"))
    lines = [f"# {subject}", ""]
    sender = format_address(message.get("From") or message.get("Sender"))
    lines.append(f"- **From**: {sender or 'unknown'}")
    for label, key in (("To", "ToRecipients"), ("Cc", "CcRecipients")):
        recipients = [format_address(r) for r in message.get(key) or []]
        recipients = [r for r in recipients if r]
        if recipients:
            lines.append(f"- **{label}**: {'; '.join(recipients)}")
    lines.append(f"- **Date**: {fmt_dt(received)}")
    lines.append(f"- **Folder**: {folder}")
    importance = str(message.get("Importance") or "Normal")
    if importance.lower() != "normal":
        lines.append(f"- **Importance**: {importance}")
    if attachments:
        names = [
            f"{one_line(a.get('Name'))} ({human_size(a.get('Size'))})".replace(" ()", "")
            for a in attachments
            if one_line(a.get("Name"))
        ]
        if names:
            lines.append(f"- **Attachments**: {'; '.join(names)}")
    elif message.get("HasAttachments"):
        lines.append("- **Attachments**: yes")
    lines += ["", "## Message", "", body or "_(empty message)_"]
    if trimmed:
        lines += ["", TRIM_NOTE]
    return "\n".join(lines).rstrip() + "\n"


def _attendee_label(attendee: dict[str, Any]) -> str:
    name = display_name(attendee)
    response = ((attendee.get("Status") or {}).get("Response") or "").strip()
    kind = str(attendee.get("Type") or "").lower()
    notes = [
        n for n in (("optional" if kind == "optional" else ""), _response_label(response)) if n
    ]
    return f"{name} ({', '.join(notes)})" if notes else name


def _response_label(response: str) -> str:
    return {
        "accepted": "accepted",
        "declined": "declined",
        "tentativelyaccepted": "tentative",
        "organizer": "organiser",
    }.get(response.lower(), "")


def render_event_markdown(event: dict[str, Any], body: str) -> str:
    """Markdown for one calendar event (times in UTC)."""
    subject = one_line(event.get("Subject")) or "(no title)"
    start = parse_dt((event.get("Start") or {}).get("DateTime"))
    end = parse_dt((event.get("End") or {}).get("DateTime"))
    all_day = bool(event.get("IsAllDay"))
    lines = [f"# {subject}", ""]
    if start and all_day:
        when = f"{start.strftime('%a %d %b %Y')} (all day)"
    elif start and end:
        same_day = start.date() == end.date()
        end_text = end.strftime("%H:%M UTC") if same_day else fmt_dt(end)
        when = f"{start.strftime('%a %d %b %Y %H:%M')} - {end_text}"
    else:
        when = fmt_dt(start)
    lines.append(f"- **When**: {when}")
    location = one_line((event.get("Location") or {}).get("DisplayName"))
    if location:
        lines.append(f"- **Where**: {location}")
    organizer = format_address(event.get("Organizer"))
    if organizer:
        lines.append(f"- **Organiser**: {organizer}")
    attendees = [_attendee_label(a) for a in event.get("Attendees") or []]
    attendees = [a for a in attendees if a]
    if attendees:
        lines.append(f"- **Attendees**: {'; '.join(attendees)}")
    join_url = one_line(
        (event.get("OnlineMeeting") or {}).get("JoinUrl") or event.get("OnlineMeetingUrl")
    )
    if join_url:
        lines.append(f"- **Online meeting**: {join_url}")
    show_as = one_line(event.get("ShowAs"))
    if show_as and show_as.lower() != "busy":
        lines.append(f"- **Shown as**: {show_as}")
    response = _response_label((event.get("ResponseStatus") or {}).get("Response") or "")
    if response:
        lines.append(f"- **Your response**: {response}")
    if event.get("IsCancelled"):
        lines.append("- **Status**: cancelled")
    if str(event.get("Type") or "").lower() in ("occurrence", "exception", "seriesmaster"):
        lines.append("- **Recurring**: yes")
    categories = [one_line(c) for c in event.get("Categories") or [] if one_line(c)]
    if categories:
        lines.append(f"- **Categories**: {', '.join(categories)}")
    if body:
        lines += ["", "## Details", "", body]
    return "\n".join(lines).rstrip() + "\n"
