"""Tests for file convertibility checking."""

from __future__ import annotations

import pytest

from workctx.normalise.convertibility import (
    can_convert,
    should_skip_download,
)


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("doc.docx", True),
        ("report.pdf", True),
        ("code.py", True),
        ("config.yaml", True),
        ("readme.md", True),
        ("data.csv", True),
        ("notes.txt", True),
        ("script.sh", True),
        ("app.tsx", True),
        ("video.mov", False),
        ("song.mp3", False),
        ("photo.jpg", False),
        ("design.fig", False),
        ("image.png", False),
        ("binary.exe", False),
        ("font.ttf", False),
        ("movie.mp4", False),
        ("archive.dmg", False),
    ],
)
def test_can_convert(filename: str, expected: bool) -> None:
    assert can_convert(filename) == expected


def test_should_skip_download_by_extension() -> None:
    skip, reason = should_skip_download("video.mov")
    assert skip is True
    assert "unconvertible" in reason

    skip, reason = should_skip_download("doc.docx")
    assert skip is False


def test_should_skip_download_by_size() -> None:
    skip, reason = should_skip_download("huge.docx", 300 * 1024 * 1024)
    assert skip is True
    assert "too large" in reason

    skip, reason = should_skip_download("small.docx", 1024)
    assert skip is False


def test_should_skip_download_size_under_limit() -> None:
    skip, _ = should_skip_download("report.pdf", 50 * 1024 * 1024)
    assert skip is False
