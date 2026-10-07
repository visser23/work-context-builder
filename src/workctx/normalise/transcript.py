"""Teams meeting transcript parsing and Markdown rendering.

Pure functions (no network, no I/O) so they are trivially unit-testable.

Teams stores transcripts as Stream "media transcripts". Two serialisations are
useful: the JSON form (``format=json``) carries speaker names; the WebVTT form
(``format=vtt``) does not, so it is only used as a fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

UNKNOWN_SPEAKER = "Unknown speaker"

# Consecutive utterances by one speaker are merged into a paragraph when the
# silence between them is at most this many seconds…
MERGE_GAP_SECONDS = 10.0
# …and the paragraph stays below this many characters.
MAX_PARAGRAPH_CHARS = 1200

_RECORDING_STAMP_RE = re.compile(
    r"-(?P<date>\d{8})_(?P<time>\d{6})(?P<utc>UTC)?-Meeting (?:Recording|Transcript)",
    re.IGNORECASE,
)
_VTT_TIMING_RE = re.compile(
    r"^(?P<start>(?:\d+:)?\d{1,2}:\d{2}(?:\.\d+)?)\s*-->\s*(?P<end>(?:\d+:)?\d{1,2}:\d{2}(?:\.\d+)?)"
)
_VTT_VOICE_RE = re.compile(r"<v(?:\.[^\s>]+)?\s+([^>]+)>")
_VTT_TAG_RE = re.compile(r"</?[^>]+>")


@dataclass(frozen=True)
class Turn:
    """One paragraph of speech by a single speaker."""

    speaker: str
    start_seconds: float
    text: str


@dataclass
class ParsedTranscript:
    """Structured transcript ready for rendering."""

    turns: list[Turn] = field(default_factory=list)
    duration_seconds: float | None = None
    language: str | None = None

    @property
    def speakers(self) -> list[str]:
        """Speakers in order of first appearance."""
        seen: dict[str, None] = {}
        for turn in self.turns:
            seen.setdefault(turn.speaker, None)
        return list(seen)

    @property
    def word_count(self) -> int:
        return sum(len(t.text.split()) for t in self.turns)


def parse_offset(value: str | None) -> float:
    """Parse ``HH:MM:SS(.fraction)`` (or ``MM:SS``) into seconds. Bad input → 0.0."""
    if not value:
        return 0.0
    parts = value.strip().split(":")
    try:
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + float(part)
        return seconds
    except ValueError:
        return 0.0


def format_clock(seconds: float) -> str:
    """Render seconds as ``HH:MM:SS``."""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def format_duration(seconds: float | None) -> str:
    """Human-friendly duration such as ``1h 15m`` or ``42m``."""
    if seconds is None:
        return "unknown"
    if seconds < 30:
        return "under a minute"
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def parse_recording_stamp(filename: str) -> tuple[date, str, bool] | None:
    """Extract ``(date, "HH:MM", is_utc)`` from a Teams recording file name.

    Teams names files ``<title>-YYYYMMDD_HHMMSS[UTC]-Meeting Recording.mp4``. The
    time is in the *organiser's* local time unless the ``UTC`` suffix is present.
    """
    match = _RECORDING_STAMP_RE.search(filename)
    if not match:
        return None
    raw_date, raw_time = match.group("date"), match.group("time")
    try:
        day = datetime.strptime(raw_date, "%Y%m%d").date()
    except ValueError:
        return None
    return day, f"{raw_time[0:2]}:{raw_time[2:4]}", bool(match.group("utc"))


def parse_transcript_json(doc: dict[str, Any]) -> ParsedTranscript:
    """Convert a Stream ``format=json`` transcript into merged speaker turns."""
    entries = [e for e in doc.get("entries") or [] if isinstance(e, dict)]
    utterances: list[tuple[float, float, str, str]] = []
    language: str | None = None
    for entry in entries:
        text = " ".join(str(entry.get("text") or "").split())
        if not text:
            continue
        speaker = str(entry.get("speakerDisplayName") or "").strip() or UNKNOWN_SPEAKER
        start = parse_offset(entry.get("startOffset"))
        end = parse_offset(entry.get("endOffset")) or start
        utterances.append((start, end, speaker, text))
        language = language or entry.get("spokenLanguageTag")

    utterances.sort(key=lambda u: (u[0], u[1]))

    duration = max((u[1] for u in utterances), default=0.0)
    for event in doc.get("events") or []:
        if isinstance(event, dict) and event.get("eventType") == "TranscriptStopped":
            duration = max(duration, parse_offset(event.get("startOffset")))

    return ParsedTranscript(
        turns=_merge_utterances(utterances),
        duration_seconds=duration or None,
        language=language,
    )


def parse_transcript_vtt(vtt: str) -> ParsedTranscript:
    """Fallback parser for WebVTT (speaker names only if ``<v Name>`` tags exist)."""
    utterances: list[tuple[float, float, str, str]] = []
    for block in re.split(r"\n\s*\n", vtt.replace("\r\n", "\n").lstrip("\ufeff")):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        timing_idx = next(
            (i for i, ln in enumerate(lines) if _VTT_TIMING_RE.match(ln.strip())), None
        )
        if timing_idx is None:
            continue
        timing = _VTT_TIMING_RE.match(lines[timing_idx].strip())
        assert timing is not None  # for type-checkers; guarded above
        payload = " ".join(lines[timing_idx + 1 :])
        voice = _VTT_VOICE_RE.search(payload)
        speaker = voice.group(1).strip() if voice else UNKNOWN_SPEAKER
        text = " ".join(_VTT_TAG_RE.sub("", payload).split())
        if text:
            utterances.append(
                (
                    parse_offset(timing.group("start")),
                    parse_offset(timing.group("end")),
                    speaker,
                    text,
                )
            )
    utterances.sort(key=lambda u: (u[0], u[1]))
    duration = max((u[1] for u in utterances), default=0.0)
    return ParsedTranscript(turns=_merge_utterances(utterances), duration_seconds=duration or None)


def _merge_utterances(utterances: list[tuple[float, float, str, str]]) -> list[Turn]:
    turns: list[Turn] = []
    cur_speaker = ""
    cur_start = 0.0
    cur_end = 0.0
    cur_text: list[str] = []
    cur_len = 0

    def flush() -> None:
        if cur_text:
            turns.append(Turn(cur_speaker, cur_start, " ".join(cur_text)))

    for start, end, speaker, text in utterances:
        can_merge = (
            cur_text
            and speaker == cur_speaker
            and start - cur_end <= MERGE_GAP_SECONDS
            and cur_len + len(text) < MAX_PARAGRAPH_CHARS
        )
        if can_merge:
            cur_text.append(text)
            cur_len += len(text) + 1
            cur_end = max(cur_end, end)
            continue
        flush()
        cur_speaker, cur_start, cur_end = speaker, start, end
        cur_text, cur_len = [text], len(text)
    flush()
    return turns


def render_transcript_markdown(
    title: str,
    transcript: ParsedTranscript,
    *,
    meeting_date: date | None = None,
    start_time: str | None = None,
    start_time_is_utc: bool = False,
    recorded_by: str | None = None,
    recording_name: str | None = None,
    recording_url: str | None = None,
) -> str:
    """Render a transcript as Markdown (without YAML front matter)."""
    lines = [f"# {title}", ""]

    if meeting_date:
        when = meeting_date.isoformat()
        if start_time:
            zone = "UTC" if start_time_is_utc else "organiser's local time"
            when += f", starting {start_time} ({zone})"
        lines.append(f"- **Meeting date**: {when}")
    lines.append(f"- **Duration**: {format_duration(transcript.duration_seconds)}")
    if transcript.speakers:
        lines.append(f"- **Speakers**: {'; '.join(transcript.speakers)}")
    if recorded_by:
        lines.append(f"- **Recorded by**: {recorded_by}")
    if recording_url:
        label = recording_name or "Teams recording"
        lines.append(f"- **Recording**: [{label}]({recording_url})")
    if transcript.language:
        lines.append(f"- **Language**: {transcript.language}")
    lines.append(f"- **Words**: {transcript.word_count:,}")
    lines.extend(["", "## Transcript", ""])

    if not transcript.turns:
        lines.append("*The transcript contains no spoken text.*")
    for turn in transcript.turns:
        lines.append(f"[{format_clock(turn.start_seconds)}] **{turn.speaker}:** {turn.text}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
