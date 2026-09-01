"""Tests for domain models."""

from datetime import UTC, datetime

from workctx.models import (
    FrontMatter,
    ManifestEntry,
    RunStatus,
    SourceResult,
    SourceType,
    SyncResult,
)


def test_front_matter_yaml():
    fm = FrontMatter(
        source_type="confluence",
        source_name="test-wiki",
        source_id="12345",
        title="Test Page",
        source_url="https://example.com/page/12345",
        space="TEST",
        source_version=5,
        synced_at=datetime(2026, 9, 1, 5, 30, 0, tzinfo=UTC),
    )
    yaml_str = fm.to_yaml_str()
    assert "---" in yaml_str
    assert "source_type: confluence" in yaml_str
    assert "source_id: 12345" in yaml_str
    assert "title: Test Page" in yaml_str
    assert "space: TEST" in yaml_str


def test_front_matter_special_chars():
    fm = FrontMatter(
        source_type="jira",
        source_name="test",
        source_id="1",
        title="Fix: Handle edge case #123",
    )
    yaml_str = fm.to_yaml_str()
    assert 'title: "Fix: Handle edge case #123"' in yaml_str


def test_sync_result_aggregate():
    result = SyncResult(
        run_id="test",
        started_at=datetime.now(UTC),
    )
    result.source_results = [
        SourceResult(source_name="a", source_type=SourceType.JIRA, status=RunStatus.HEALTHY),
        SourceResult(source_name="b", source_type=SourceType.CONFLUENCE, status=RunStatus.HEALTHY),
    ]
    assert result.aggregate_status() == RunStatus.HEALTHY

    result.source_results.append(
        SourceResult(source_name="c", source_type=SourceType.SHAREPOINT, status=RunStatus.DEGRADED)
    )
    assert result.aggregate_status() == RunStatus.DEGRADED

    result.source_results.append(
        SourceResult(source_name="d", source_type=SourceType.JIRA, status=RunStatus.FAILED)
    )
    assert result.aggregate_status() == RunStatus.FAILED


def test_manifest_entry_json():
    entry = ManifestEntry(
        source_type="jira",
        source_name="test-jira",
        source_id="100",
        source_key="TEST-1",
        output_path="jira/test-jira/TEST/TEST-1.md",
        title="Test Issue",
    )
    json_str = entry.model_dump_json()
    assert "TEST-1" in json_str
    assert "jira" in json_str
