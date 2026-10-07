"""Tests for Teams transcript parsing and Markdown rendering (pure functions)."""

from __future__ import annotations

from datetime import date

import pytest

from workctx.normalise.transcript import (
    MAX_PARAGRAPH_CHARS,
    UNKNOWN_SPEAKER,
    ParsedTranscript,
    Turn,
    format_clock,
    format_duration,
    parse_offset,
    parse_recording_stamp,
    parse_transcript_json,
    parse_transcript_vtt,
    render_transcript_markdown,
)


def _entry(text, speaker, start, end, **extra):
    return {
        "id": f"id-{start}",
        "text": text,
        "speakerDisplayName": speaker,
        "startOffset": start,
        "endOffset": end,
        **extra,
    }


class TestOffsets:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("00:00:03.8090054", 3.8090054),
            ("01:15:30", 4530.0),
            ("02:05", 125.0),
            ("", 0.0),
            (None, 0.0),
            ("garbage", 0.0),
        ],
    )
    def test_parse_offset(self, raw, expected):
        assert parse_offset(raw) == pytest.approx(expected)

    def test_format_clock(self):
        assert format_clock(0) == "00:00:00"
        assert format_clock(3661.9) == "01:01:01"
        assert format_clock(-5) == "00:00:00"

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (None, "unknown"),
            (10, "under a minute"),
            (42 * 60, "42m"),
            (3600, "1h"),
            (75 * 60, "1h 15m"),
        ],
    )
    def test_format_duration(self, seconds, expected):
        assert format_duration(seconds) == expected


class TestRecordingStamp:
    def test_local_time_stamp(self):
        stamp = parse_recording_stamp("Weekly sync-20260625_101615-Meeting Transcript.mp4")
        assert stamp == (date(2026, 6, 25), "10:16", False)

    def test_utc_stamp(self):
        stamp = parse_recording_stamp("Sprint review-20260519_120402UTC-Meeting Recording.mp4")
        assert stamp == (date(2026, 5, 19), "12:04", True)

    def test_title_containing_digits_and_dashes(self):
        stamp = parse_recording_stamp("Q1-2026 plan-20260102_090000-Meeting Recording.mp4")
        assert stamp is not None
        assert stamp[0] == date(2026, 1, 2)

    @pytest.mark.parametrize(
        "name",
        ["notes.mp4", "Meeting Recording.mp4", "x-20261399_101615-Meeting Recording.mp4", ""],
    )
    def test_unparseable(self, name):
        assert parse_recording_stamp(name) is None


class TestParseJson:
    def test_merges_same_speaker_and_sorts_by_time(self):
        doc = {
            "entries": [
                _entry("second bit.", "Ada", "00:00:04.0", "00:00:06.0"),
                _entry("Hello there.", "Ada", "00:00:01.0", "00:00:03.0"),
                _entry("Hi!", "Bob", "00:00:03.5", "00:00:04.0"),
            ]
        }
        parsed = parse_transcript_json(doc)
        # Ada(1s) -> Bob(3.5s) -> Ada(4s): speaker changes prevent merging across Bob
        assert [(t.speaker, t.text) for t in parsed.turns] == [
            ("Ada", "Hello there."),
            ("Bob", "Hi!"),
            ("Ada", "second bit."),
        ]

    def test_merges_close_utterances_but_not_after_long_gap(self):
        doc = {
            "entries": [
                _entry("One.", "Ada", "00:00:00", "00:00:02"),
                _entry("Two.", "Ada", "00:00:05", "00:00:07"),
                _entry("Three.", "Ada", "00:01:00", "00:01:02"),
            ]
        }
        parsed = parse_transcript_json(doc)
        assert [t.text for t in parsed.turns] == ["One. Two.", "Three."]
        assert parsed.turns[1].start_seconds == 60.0

    def test_paragraph_length_cap(self):
        long_text = "word " * (MAX_PARAGRAPH_CHARS // 10)
        doc = {
            "entries": [
                _entry(long_text.strip(), "Ada", "00:00:00", "00:00:05"),
                _entry(long_text.strip(), "Ada", "00:00:05", "00:00:10"),
                _entry(long_text.strip(), "Ada", "00:00:10", "00:00:15"),
            ]
        }
        parsed = parse_transcript_json(doc)
        assert len(parsed.turns) > 1

    def test_blank_entries_and_missing_speaker(self):
        doc = {
            "entries": [
                _entry("   ", "Ada", "00:00:00", "00:00:01"),
                _entry(None, "Ada", "00:00:01", "00:00:02"),
                _entry("Spoken.", None, "00:00:02", "00:00:03"),
            ]
        }
        parsed = parse_transcript_json(doc)
        assert len(parsed.turns) == 1
        assert parsed.turns[0].speaker == UNKNOWN_SPEAKER

    def test_duration_prefers_transcript_stopped_event(self):
        doc = {
            "entries": [_entry("Hi.", "Ada", "00:00:00", "00:00:02")],
            "events": [{"eventType": "TranscriptStopped", "startOffset": "01:00:00"}],
        }
        assert parse_transcript_json(doc).duration_seconds == pytest.approx(3600)

    def test_language_taken_from_entries(self):
        doc = {"entries": [_entry("Hi.", "Ada", "0:0:0", "0:0:1", spokenLanguageTag="en-gb")]}
        assert parse_transcript_json(doc).language == "en-gb"

    def test_empty_and_malformed_documents(self):
        assert parse_transcript_json({}).turns == []
        assert parse_transcript_json({"entries": None}).turns == []
        assert parse_transcript_json({"entries": ["not-a-dict", 5]}).turns == []

    def test_whitespace_normalised(self):
        doc = {"entries": [_entry("a   b\n c", "Ada", "00:00:00", "00:00:01")]}
        assert parse_transcript_json(doc).turns[0].text == "a b c"


class TestParseVtt:
    VTT = (
        "\ufeffWEBVTT\n\n"
        "uuid-1/6-0\n00:00:03.809 --> 00:00:06.714\nright?\nYou never find out\n\n"
        "uuid-1/5-0\n00:00:01.000 --> 00:00:02.000\nExactly.\n\n"
        "not a cue\n"
    )

    def test_parses_sorts_and_merges_cues(self):
        parsed = parse_transcript_vtt(self.VTT)
        # Cues are time-ordered; with no speaker info close cues merge into one turn.
        assert [t.text for t in parsed.turns] == ["Exactly. right? You never find out"]
        assert all(t.speaker == UNKNOWN_SPEAKER for t in parsed.turns)
        assert parsed.turns[0].start_seconds == 1.0

    def test_voice_tags_give_speakers(self):
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Ada Lovelace>Hello</v>\n"
        parsed = parse_transcript_vtt(vtt)
        assert parsed.turns == [Turn("Ada Lovelace", 1.0, "Hello")]

    def test_crlf_and_hour_timestamps(self):
        vtt = "WEBVTT\r\n\r\n01:00:00.000 --> 01:00:02.000\r\nLate line\r\n"
        parsed = parse_transcript_vtt(vtt)
        assert parsed.turns[0].start_seconds == 3600.0


class TestRender:
    def _parsed(self):
        return ParsedTranscript(
            turns=[Turn("Ada", 3.0, "Hello."), Turn("Bob", 65.0, "Hi there.")],
            duration_seconds=4500,
            language="en-gb",
        )

    def test_full_render(self):
        md = render_transcript_markdown(
            "Weekly sync",
            self._parsed(),
            meeting_date=date(2026, 6, 25),
            start_time="10:16",
            recorded_by="Ada",
            recording_name="Weekly sync.mp4",
            recording_url="https://example.sharepoint.com/x.mp4",
        )
        assert md.startswith("# Weekly sync\n")
        assert "- **Meeting date**: 2026-06-25, starting 10:16 (organiser's local time)" in md
        assert "- **Duration**: 1h 15m" in md
        assert "- **Speakers**: Ada; Bob" in md
        assert "- **Recorded by**: Ada" in md
        assert "[Weekly sync.mp4](https://example.sharepoint.com/x.mp4)" in md
        assert "- **Words**: 3" in md
        assert "[00:00:03] **Ada:** Hello." in md
        assert "[00:01:05] **Bob:** Hi there." in md
        assert md.endswith("\n") and not md.endswith("\n\n")

    def test_utc_label(self):
        md = render_transcript_markdown(
            "T",
            self._parsed(),
            meeting_date=date(2026, 1, 2),
            start_time="09:00",
            start_time_is_utc=True,
        )
        assert "starting 09:00 (UTC)" in md

    def test_optional_fields_omitted(self):
        md = render_transcript_markdown("T", ParsedTranscript())
        assert "Meeting date" not in md
        assert "Recorded by" not in md
        assert "no spoken text" in md
