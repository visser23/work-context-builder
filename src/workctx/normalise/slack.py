"""Render Slack messages as a per-conversation, per-day Markdown digest (pure functions)."""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Messages that are bookkeeping, not conversation.
SKIPPED_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "group_join",
        "group_leave",
        "channel_archive",
        "channel_unarchive",
        "pinned_item",
        "unpinned_item",
        "message_deleted",
        "tombstone",
    }
)

_ANGLE = re.compile(r"<([^<>\n]+)>")
_BOLD = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_STRIKE = re.compile(r"(?<![\w~])~(?=\S)([^~\n]+?)(?<=\S)~(?![\w~])")
_USER_ID = re.compile(r"<@([UW][A-Z0-9]+)")


@dataclass
class SlackMessage:
    """One message prepared for rendering."""

    ts: str
    user: str
    text: str
    is_reply: bool = False
    thread_preview: str = ""
    files: list[str] = field(default_factory=list)
    reactions: list[tuple[str, int]] = field(default_factory=list)
    edited: bool = False
    bot: bool = False

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(float(self.ts), tz=UTC)


def ts_to_datetime(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=UTC)


def ts_to_day(ts: str) -> str:
    return ts_to_datetime(ts).strftime("%Y-%m-%d")


def mentioned_user_ids(text: str) -> set[str]:
    """User ids referenced as ``<@U123>`` mentions in raw Slack text."""
    return set(_USER_ID.findall(text or ""))


def convert_mrkdwn(
    text: str,
    resolve_user: Callable[[str], str],
    resolve_channel: Callable[[str], str] | None = None,
) -> str:
    """Convert Slack ``mrkdwn`` to Markdown: mentions, links, bold, strike, entities."""

    def replace_angle(match: re.Match[str]) -> str:
        inner = match.group(1)
        target, _, label = inner.partition("|")
        if target.startswith("@"):
            user_id = target[1:]
            return "@" + (resolve_user(user_id) or label or user_id)
        if target.startswith("#C"):
            channel_id = target[1:]
            name = label or (resolve_channel(channel_id) if resolve_channel else "") or channel_id
            return "#" + name
        if target.startswith("!"):
            command = target[1:]
            if command.startswith("subteam^"):
                return label if label.startswith("@") else "@" + (label or command)
            if command.startswith("date^"):
                return label or command
            return "@" + (label or command.split("^")[0])
        if re.match(r"^(https?://|mailto:|tel:)", target):
            plain = re.sub(r"^(mailto:|tel:)", "", target)
            if label and label not in (target, plain):
                return f"[{label}]({target})"
            return plain
        return match.group(0)

    converted = _ANGLE.sub(replace_angle, text or "")
    converted = html.unescape(converted)
    converted = _BOLD.sub(r"**\1**", converted)
    return _STRIKE.sub(r"~~\1~~", converted)


def message_fingerprint(raw: dict[str, Any]) -> str:
    """Stable digest of the parts of a Slack message that can change."""
    reactions = ",".join(f"{r.get('name')}:{r.get('count')}" for r in raw.get("reactions") or [])
    parts = (
        str(raw.get("ts")),
        str(raw.get("text") or ""),
        str((raw.get("edited") or {}).get("ts") or ""),
        str(raw.get("reply_count") or ""),
        str(raw.get("latest_reply") or ""),
        reactions,
        ",".join(str(f.get("id") or f.get("name")) for f in raw.get("files") or []),
    )
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def version_for(fingerprints: list[str]) -> str:
    """Version string for a day digest: message count + hash of every fingerprint."""
    digest = hashlib.sha1("\n".join(sorted(fingerprints)).encode("utf-8")).hexdigest()[:12]
    return f"{len(fingerprints)}:{digest}"


def _preview(text: str, limit: int = 60) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def format_message(message: SlackMessage) -> str:
    """One message as Markdown (multi-line bodies keep their line breaks)."""
    stamp = message.when.strftime("%H:%M")
    name = message.user or "unknown"
    prefix = f"**[{stamp}] {name}**"
    if message.bot:
        prefix += " _(app)_"
    if message.is_reply:
        quoted = f' "{message.thread_preview}"' if message.thread_preview else ""
        prefix = f"↳ {prefix} _(thread reply to{quoted})_"
    body = (message.text or "").strip()
    lines = body.split("\n") if body else [""]
    text = f"{prefix}: {lines[0]}".rstrip()
    if len(lines) > 1:
        text += "\n" + "\n".join(f"  {line}" if line else "" for line in lines[1:])
    extras: list[str] = []
    if message.files:
        extras.append("files: " + ", ".join(message.files))
    if message.reactions:
        extras.append("reactions: " + ", ".join(f":{n}: x{c}" for n, c in message.reactions))
    if message.edited:
        extras.append("edited")
    if extras:
        text += f"\n  _({'; '.join(extras)})_"
    return text


def render_day_markdown(
    *,
    conversation: str,
    workspace: str,
    day: str,
    messages: list[SlackMessage],
) -> str:
    """Markdown digest of one conversation on one (UTC) day."""
    ordered = sorted(messages, key=lambda m: float(m.ts))
    speakers = sorted({m.user for m in ordered if m.user})
    lines = [
        f"# {conversation} - {day}",
        "",
        f"- **Workspace**: {workspace}",
        f"- **Date**: {day} (times are UTC)",
        f"- **Messages**: {len(ordered)}",
    ]
    if speakers:
        lines.append(f"- **Participants**: {', '.join(speakers)}")
    lines += ["", "## Messages", ""]
    lines.append("\n\n".join(format_message(m) for m in ordered))
    return "\n".join(lines).rstrip() + "\n"


def thread_preview(text: str) -> str:
    return _preview(text)
