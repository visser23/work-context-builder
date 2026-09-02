"""Integration tests for sync orchestration."""

import pytest
import yaml

from workctx.config import load_config
from workctx.models import RunStatus
from workctx.sync import run_sync


@pytest.fixture
def project_setup(tmp_path):
    """Create a complete test project with SharePoint source."""
    sp_dir = tmp_path / "sharepoint_files"
    sp_dir.mkdir()
    (sp_dir / "README.md").write_text("# Project README\n\nWelcome to the project.\n")
    (sp_dir / "notes.txt").write_text("Some important notes about the project.\n")

    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"

    config_dict = {
        "version": 1,
        "project": {
            "id": "test-project",
            "name": "Test Project",
            "output_root": str(output_dir),
        },
        "schedule": {"hour": 5, "minute": 30},
        "sync": {
            "overlap_minutes": 15,
            "reconciliation_days": 7,
            "max_concurrency": 4,
            "large_document_chars": 300000,
        },
        "sources": {
            "sharepoint": [
                {
                    "name": "test-docs",
                    "mode": "onedrive_local",
                    "local_path": str(sp_dir),
                    "include": ["**/*"],
                    "exclude": ["**/~$*", "**/.DS_Store"],
                }
            ]
        },
        "notifications": {
            "telegram": {"enabled": False},
            "macos": {"enabled": False},
        },
    }

    config_path = tmp_path / "test.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config_dict, f)

    config = load_config(config_path)
    # Override state dir to tmp
    config.__dict__["_state_dir_override"] = state_dir

    return {
        "config": config,
        "sp_dir": sp_dir,
        "output_dir": output_dir,
        "state_dir": state_dir,
    }


def _patch_state_dir(config, state_dir):
    """Patch the state_dir property to use our test directory."""
    original_state_dir = type(config).state_dir
    type(config).state_dir = property(lambda self: state_dir)
    return original_state_dir


def test_initial_sync(project_setup):
    config = project_setup["config"]
    state_dir = project_setup["state_dir"]
    output_dir = project_setup["output_dir"]

    original = _patch_state_dir(config, state_dir)
    try:
        result = run_sync(config, run_id="test-001", full=True)

        assert result.status in (RunStatus.HEALTHY, RunStatus.DEGRADED)
        assert len(result.source_results) == 1

        sr = result.source_results[0]
        assert sr.source_name == "test-docs"
        assert sr.objects_added >= 1 or sr.objects_checked >= 1

        assert output_dir.exists()
        assert (output_dir / "CONTEXT.md").exists()
        assert (output_dir / "AGENTS.md").exists()
        assert (output_dir / "CLAUDE.md").exists()
        assert (output_dir / "README.md").exists()
        assert (output_dir / "_meta" / "manifest.jsonl").exists()
        assert (output_dir / "_meta" / "health.json").exists()
        assert (output_dir / "_meta" / "INDEX.md").exists()

        sp_files = list((output_dir / "sharepoint").rglob("*.md"))
        assert len(sp_files) >= 1
    finally:
        type(config).state_dir = original


def test_second_sync_no_changes(project_setup):
    config = project_setup["config"]
    state_dir = project_setup["state_dir"]

    original = _patch_state_dir(config, state_dir)
    try:
        run_sync(config, run_id="test-001", full=True)
        result2 = run_sync(config, run_id="test-002")

        sr2 = result2.source_results[0]
        assert sr2.objects_added == 0
        assert sr2.objects_updated == 0
    finally:
        type(config).state_dir = original


def test_dry_run(project_setup):
    config = project_setup["config"]
    state_dir = project_setup["state_dir"]
    output_dir = project_setup["output_dir"]

    original = _patch_state_dir(config, state_dir)
    try:
        result = run_sync(config, run_id="dry-001", dry_run=True, full=True)

        sr = result.source_results[0]
        assert sr.objects_checked >= 1

        assert not (output_dir / "CONTEXT.md").exists()
    finally:
        type(config).state_dir = original


def test_sync_detects_new_file(project_setup):
    config = project_setup["config"]
    state_dir = project_setup["state_dir"]
    sp_dir = project_setup["sp_dir"]

    original = _patch_state_dir(config, state_dir)
    try:
        run_sync(config, run_id="test-001", full=True)

        (sp_dir / "new-document.txt").write_text("New document content")

        result = run_sync(config, run_id="test-002")
        sr = result.source_results[0]
        assert sr.objects_added >= 1
    finally:
        type(config).state_dir = original
