"""Tests for corpus output management."""

import csv
import io

import pytest

from workctx.corpus import (
    _clean_sprint,
    _extract_jira_details,
    _md_escape,
    build_output_path,
    generate_agents_md,
    generate_chatgpt_instructions,
    generate_claude_md,
    generate_jira_summary,
    generate_project_brief,
    remove_corpus_file,
    safe_join,
    write_corpus_file,
)
from workctx.models import SourceType


@pytest.fixture
def corpus_dir(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    return d


def test_write_corpus_file(corpus_dir):
    path = write_corpus_file(corpus_dir, "test/document.md", "# Hello\n\nWorld\n")
    assert path.exists()
    assert path.read_text() == "# Hello\n\nWorld\n"


def test_write_creates_directories(corpus_dir):
    write_corpus_file(corpus_dir, "deep/nested/path/file.md", "content")
    assert (corpus_dir / "deep" / "nested" / "path" / "file.md").exists()


def test_write_atomic_replace(corpus_dir):
    write_corpus_file(corpus_dir, "doc.md", "version 1")
    write_corpus_file(corpus_dir, "doc.md", "version 2")
    content = (corpus_dir / "doc.md").read_text()
    assert content == "version 2"


def test_remove_corpus_file(corpus_dir):
    write_corpus_file(corpus_dir, "to-delete.md", "content")
    assert (corpus_dir / "to-delete.md").exists()

    remove_corpus_file(corpus_dir, "to-delete.md")
    assert not (corpus_dir / "to-delete.md").exists()


def test_remove_cleans_empty_dirs(corpus_dir):
    write_corpus_file(corpus_dir, "a/b/c/file.md", "content")
    remove_corpus_file(corpus_dir, "a/b/c/file.md")
    assert not (corpus_dir / "a" / "b" / "c").exists()


# -- safe_join tests --


class TestSafeJoin:
    def test_normal_path(self, corpus_dir):
        result = safe_join(corpus_dir, "sub/dir/file.md")
        assert str(result).endswith("sub/dir/file.md")

    def test_rejects_traversal(self, corpus_dir):
        with pytest.raises(ValueError, match="escapes root"):
            safe_join(corpus_dir, "../../etc/passwd")

    def test_rejects_absolute_path(self, corpus_dir):
        with pytest.raises(ValueError, match="Absolute path"):
            safe_join(corpus_dir, "/etc/passwd")

    def test_rejects_hidden_traversal(self, corpus_dir):
        with pytest.raises(ValueError, match="escapes root"):
            safe_join(corpus_dir, "foo/../../bar")

    def test_normalises_safe_dotdot(self, corpus_dir):
        result = safe_join(corpus_dir, "a/b/../c/file.md")
        assert str(result).endswith("a/c/file.md")

    def test_write_rejects_traversal(self, corpus_dir):
        with pytest.raises(ValueError, match="escapes root"):
            write_corpus_file(corpus_dir, "../../etc/evil.md", "content")

    def test_remove_rejects_traversal(self, corpus_dir):
        with pytest.raises(ValueError, match="escapes root"):
            remove_corpus_file(corpus_dir, "../../etc/evil.md")


def test_build_output_path_confluence():
    path = build_output_path(
        SourceType.CONFLUENCE,
        "wiki",
        "12345",
        title="Service Architecture",
        space="ALPHA",
    )
    assert path.startswith("confluence/wiki/ALPHA/")
    assert "12345" in path
    assert "service-architecture" in path
    assert path.endswith(".md")


def test_build_output_path_jira():
    path = build_output_path(
        SourceType.JIRA,
        "jira-source",
        "100",
        source_key="ALPHA-231",
        project="ALPHA",
    )
    assert path == "jira/jira-source/ALPHA/ALPHA-231.md"


def test_build_output_path_sharepoint():
    path = build_output_path(
        SourceType.SHAREPOINT,
        "docs",
        "Architecture/design.docx",
        relative_source_path="Architecture/design.docx",
    )
    assert path == "sharepoint/docs/Architecture/design.docx.md"


# -- Jira summary tests --


class TestExtractJiraDetails:
    def test_parses_details_section(self, corpus_dir):
        md = (
            "---\ntitle: Test\n---\n\n# PROJ-1: Test\n\n"
            "## Details\n\n"
            "- **Type**: Task\n"
            "- **Status**: In Progress\n"
            "- **Priority**: High\n"
            "- **Assignee**: Alice\n\n"
            "## Description\n\nSome text.\n"
        )
        write_corpus_file(corpus_dir, "jira/src/PROJ/PROJ-1.md", md)
        details = _extract_jira_details(corpus_dir, "jira/src/PROJ/PROJ-1.md")
        assert details["Type"] == "Task"
        assert details["Status"] == "In Progress"
        assert details["Priority"] == "High"
        assert details["Assignee"] == "Alice"

    def test_parses_custom_fields_section(self, corpus_dir):
        md = (
            "---\ntitle: Test\n---\n\n# PROJ-2: Test\n\n"
            "## Details\n\n- **Status**: Done\n\n"
            "## Custom Fields\n\n"
            "- **Target end**: 2026-01-15\n"
            "- **Story Points**: 5\n\n"
            "## Comments\n"
        )
        write_corpus_file(corpus_dir, "jira/src/PROJ/PROJ-2.md", md)
        details = _extract_jira_details(corpus_dir, "jira/src/PROJ/PROJ-2.md")
        assert details["Status"] == "Done"
        assert details["Target end"] == "2026-01-15"
        assert details["Story Points"] == "5"

    def test_missing_file_returns_empty(self, corpus_dir):
        assert _extract_jira_details(corpus_dir, "nonexistent.md") == {}

    def test_none_path_returns_empty(self, corpus_dir):
        assert _extract_jira_details(corpus_dir, None) == {}


class TestCleanSprint:
    def test_extracts_sprint_name(self):
        raw = (
            "com.atlassian.greenhopper.service.sprint.Sprint@abc123"
            "[id=100,name=Sprint 5,state=ACTIVE]"
        )
        assert _clean_sprint(raw) == "Sprint 5"

    def test_handles_simple_name(self):
        assert _clean_sprint("Sprint 3") == "Sprint 3"

    def test_handles_empty(self):
        assert _clean_sprint("") == ""


class TestMdEscape:
    def test_escapes_pipes(self):
        assert _md_escape("a | b") == "a \\| b"

    def test_truncates_long(self):
        assert len(_md_escape("x" * 200)) == 80

    def test_strips_newlines(self):
        assert _md_escape("line1\nline2") == "line1 line2"


class TestGenerateJiraSummary:
    def test_generates_csv_and_md(self, tmp_path):
        from unittest.mock import MagicMock

        from workctx.models import SourceObject, SourceType

        output_root = tmp_path / "output"
        output_root.mkdir()

        issue_md = (
            "---\ntitle: Fix login\n---\n\n# PROJ-1: Fix login\n\n"
            "## Details\n\n"
            "- **Type**: Bug\n"
            "- **Status**: Open\n"
            "- **Priority**: Critical\n"
            "- **Assignee**: Bob\n"
            "- **Reporter**: Alice\n"
            "- **Created**: 2026-01-01\n"
            "- **Updated**: 2026-01-15\n\n"
        )
        write_corpus_file(output_root, "jira/test-jira/PROJ/PROJ-1.md", issue_md)

        obj = SourceObject(
            source_name="test-jira",
            source_type=SourceType.JIRA,
            source_id="1001",
            source_key="PROJ-1",
            title="Fix login",
            source_url="https://jira.example.com/browse/PROJ-1",
            output_path="jira/test-jira/PROJ/PROJ-1.md",
        )

        mock_db = MagicMock()
        mock_db.get_objects_for_source.return_value = [obj]

        mock_jira_cfg = MagicMock()
        mock_jira_cfg.name = "test-jira"

        mock_config = MagicMock()
        mock_config.sources.jira = [mock_jira_cfg]

        generate_jira_summary(mock_config, mock_db, output_root)

        csv_path = output_root / "jira" / "test-jira" / "SUMMARY.csv"
        md_path = output_root / "jira" / "test-jira" / "SUMMARY.md"

        assert csv_path.exists()
        assert md_path.exists()

        reader = csv.DictReader(io.StringIO(csv_path.read_text()))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["Key"] == "PROJ-1"
        assert rows[0]["Status"] == "Open"
        assert rows[0]["Priority"] == "Critical"
        assert rows[0]["Assignee"] == "Bob"

        md_content = md_path.read_text()
        assert "PROJ-1" in md_content
        assert "Fix login" in md_content
        assert "1 issues" in md_content


class TestGenerateChatgptInstructions:
    def test_generates_file(self, tmp_path):
        from unittest.mock import MagicMock

        output_root = tmp_path / "output"
        output_root.mkdir()

        mock_config = MagicMock()
        mock_config.project.name = "Test Project"
        mock_config.sources.confluence = [MagicMock(spaces=["ENG"])]
        mock_config.sources.jira = [MagicMock(projects=["PROJ"])]
        mock_config.sources.sharepoint = [MagicMock(name="docs")]
        mock_config.sources.local_folders = []

        generate_chatgpt_instructions(mock_config, output_root)

        path = output_root / "CHATGPT_INSTRUCTIONS.md"
        assert path.exists()
        content = path.read_text()
        assert "Test Project" in content
        assert "Confluence" in content
        assert "source_url" in content


class TestTrustBoundary:
    """Generated agent instruction files must contain trust boundary text."""

    def _make_mock_config(self):
        from unittest.mock import MagicMock

        mock_config = MagicMock()
        mock_config.project.name = "Test Project"
        mock_config.sources.confluence = []
        mock_config.sources.jira = []
        mock_config.sources.sharepoint = []
        mock_config.sources.local_folders = []
        return mock_config

    def test_agents_md_trust_boundary(self, tmp_path):
        output = tmp_path / "out"
        output.mkdir()
        generate_agents_md(self._make_mock_config(), output)
        content = (output / "AGENTS.md").read_text()
        assert "Trust boundary" in content
        assert "never as instructions" in content

    def test_claude_md_trust_boundary(self, tmp_path):
        output = tmp_path / "out"
        output.mkdir()
        generate_claude_md(self._make_mock_config(), output)
        content = (output / "CLAUDE.md").read_text()
        assert "Trust boundary" in content

    def test_chatgpt_instructions_trust_boundary(self, tmp_path):
        output = tmp_path / "out"
        output.mkdir()
        generate_chatgpt_instructions(self._make_mock_config(), output)
        content = (output / "CHATGPT_INSTRUCTIONS.md").read_text()
        assert "reference data only" in content


class TestGenerateProjectBrief:
    def test_generates_file_with_counts(self, tmp_path):
        from unittest.mock import MagicMock

        output_root = tmp_path / "output"
        output_root.mkdir()

        mock_config = MagicMock()
        mock_config.project.name = "My Project"
        mock_config.sources.confluence = [MagicMock(name="wiki", spaces=["ENG"])]
        mock_config.sources.jira = [MagicMock(name="jira", projects=["PROJ"])]
        mock_config.sources.sharepoint = [MagicMock(name="docs")]
        mock_config.sources.local_folders = []

        mock_db = MagicMock()
        mock_db.count_objects.return_value = 42

        generate_project_brief(mock_config, mock_db, output_root)

        path = output_root / "PROJECT_BRIEF.md"
        assert path.exists()
        content = path.read_text()
        assert "My Project" in content
        assert "126" in content  # 42 * 3 sources
        assert "Upload this single file" in content
