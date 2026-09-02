"""Tests for normalisation utilities and Atlassian converters."""

from workctx.models import FrontMatter
from workctx.normalise.atlassian import adf_to_markdown, confluence_storage_to_markdown
from workctx.normalise.common import (
    content_hash,
    slugify,
    split_large_document,
    wrap_with_front_matter,
)


class TestSlugify:
    def test_basic(self):
        assert slugify("Hello World") == "hello-world"

    def test_special_chars(self):
        assert slugify("Fix: Handle #123 (edge case)") == "fix-handle-123-edge-case"

    def test_max_length(self):
        result = slugify("a" * 100, max_length=20)
        assert len(result) <= 20

    def test_unicode(self):
        result = slugify("Über cool café")
        assert "uber" in result


class TestContentHash:
    def test_deterministic(self):
        h1 = content_hash("hello world")
        h2 = content_hash("hello world")
        assert h1 == h2

    def test_different(self):
        h1 = content_hash("hello")
        h2 = content_hash("world")
        assert h1 != h2


class TestFrontMatter:
    def test_wrap(self):
        fm = FrontMatter(
            source_type="jira",
            source_name="test",
            source_id="1",
            title="Test",
        )
        result = wrap_with_front_matter(fm, "# Test\n\nBody text")
        assert result.startswith("---\n")
        assert "source_type: jira" in result
        assert "# Test" in result
        assert "Body text" in result


class TestSplitLargeDocument:
    def test_small_document_no_split(self):
        fm = FrontMatter(source_type="test", source_name="t", source_id="1", title="Small")
        parts = split_large_document(fm, "Short text", max_chars=1000)
        assert len(parts) == 1

    def test_large_document_splits(self):
        fm = FrontMatter(source_type="test", source_name="t", source_id="1", title="Large")
        body = "# Section 1\n\nLorem ipsum " * 100 + "\n\n# Section 2\n\nDolor sit " * 100
        parts = split_large_document(fm, body, max_chars=500, base_path="test.md")
        assert len(parts) > 1
        for part_fm, _part_body, part_path in parts:
            assert part_fm.part_number is not None
            assert part_fm.total_parts == len(parts)
            assert ".part-" in part_path


class TestADFConversion:
    def test_simple_paragraph(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Hello world"}],
                }
            ],
        }
        result = adf_to_markdown(adf)
        assert "Hello world" in result

    def test_heading(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": "Title"}],
                }
            ],
        }
        result = adf_to_markdown(adf)
        assert "## Title" in result

    def test_bold_text(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "bold",
                            "marks": [{"type": "strong"}],
                        }
                    ],
                }
            ],
        }
        result = adf_to_markdown(adf)
        assert "**bold**" in result

    def test_link(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "click here",
                            "marks": [
                                {
                                    "type": "link",
                                    "attrs": {"href": "https://example.com"},
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        result = adf_to_markdown(adf)
        assert "[click here](https://example.com)" in result

    def test_bullet_list(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item A"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item B"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        result = adf_to_markdown(adf)
        assert "Item A" in result
        assert "Item B" in result

    def test_code_block(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "codeBlock",
                    "attrs": {"language": "python"},
                    "content": [{"type": "text", "text": "print('hello')"}],
                }
            ],
        }
        result = adf_to_markdown(adf)
        assert "```python" in result
        assert "print('hello')" in result

    def test_table(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "table",
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "Name"}],
                                        }
                                    ],
                                },
                                {
                                    "type": "tableHeader",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [{"type": "text", "text": "Value"}],
                                        }
                                    ],
                                },
                            ],
                        },
                    ],
                }
            ],
        }
        result = adf_to_markdown(adf)
        assert "Name" in result
        assert "Value" in result

    def test_empty_adf(self):
        assert adf_to_markdown({}) == ""
        assert adf_to_markdown(None) == ""

    def test_mention(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "mention", "attrs": {"text": "Jane Smith"}},
                    ],
                }
            ],
        }
        result = adf_to_markdown(adf)
        assert "@Jane Smith" in result

    def test_inline_card(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "inlineCard",
                            "attrs": {"url": "https://example.com/issue"},
                        }
                    ],
                }
            ],
        }
        result = adf_to_markdown(adf)
        assert "https://example.com/issue" in result

    def test_status(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "status", "attrs": {"text": "IN PROGRESS"}},
                    ],
                }
            ],
        }
        result = adf_to_markdown(adf)
        assert "[IN PROGRESS]" in result


class TestConfluenceStorage:
    def test_headings(self):
        xml = "<h1>Title</h1><h2>Subtitle</h2>"
        result = confluence_storage_to_markdown(xml)
        assert "# Title" in result
        assert "## Subtitle" in result

    def test_paragraphs(self):
        xml = "<p>Hello world</p>"
        result = confluence_storage_to_markdown(xml)
        assert "Hello world" in result

    def test_links(self):
        xml = '<a href="https://example.com">Click</a>'
        result = confluence_storage_to_markdown(xml)
        assert "[Click](https://example.com)" in result

    def test_bold_italic(self):
        xml = "<strong>bold</strong> and <em>italic</em>"
        result = confluence_storage_to_markdown(xml)
        assert "**bold**" in result
        assert "*italic*" in result

    def test_code_macro(self):
        xml = (
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">python</ac:parameter>'
            "<ac:plain-text-body>print('hello')</ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        result = confluence_storage_to_markdown(xml)
        assert "```python" in result

    def test_info_macro(self):
        xml = (
            '<ac:structured-macro ac:name="info">'
            "<ac:rich-text-body><p>Important note</p></ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        result = confluence_storage_to_markdown(xml)
        assert "Info" in result
        assert "Important note" in result

    def test_table(self):
        xml = "<table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>1</td></tr></table>"
        result = confluence_storage_to_markdown(xml)
        assert "Name" in result
        assert "Value" in result
        assert "---" in result

    def test_empty_content(self):
        assert confluence_storage_to_markdown("") == ""
        assert confluence_storage_to_markdown("   ") == ""

    def test_task_list(self):
        xml = (
            "<ac:task-list>"
            "<ac:task><ac:task-status>complete</ac:task-status>"
            "<ac:task-body>Done item</ac:task-body></ac:task>"
            "<ac:task><ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>Todo item</ac:task-body></ac:task>"
            "</ac:task-list>"
        )
        result = confluence_storage_to_markdown(xml)
        assert "[x] Done item" in result
        assert "[ ] Todo item" in result

    def test_unsupported_macro(self):
        xml = (
            '<ac:structured-macro ac:name="roadmap">'
            "<ac:rich-text-body>content</ac:rich-text-body>"
            "</ac:structured-macro>"
        )
        result = confluence_storage_to_markdown(xml)
        assert "[Confluence macro: roadmap]" in result
