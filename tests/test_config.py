"""Tests for configuration loading and validation."""

import pytest
import yaml

from workctx.config import (
    AuthConfig,
    ProjectConfig,
    ProjectInfo,
    SharePointSource,
    load_config,
)


@pytest.fixture
def sample_config_dict():
    return {
        "version": 1,
        "project": {
            "id": "test-project",
            "name": "Test Project",
            "output_root": "/tmp/workctx-test-output",
        },
        "schedule": {"hour": 6, "minute": 0},
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
                    "local_path": "/tmp/test-sharepoint",
                    "include": ["**/*"],
                    "exclude": ["**/~$*", "**/.DS_Store"],
                }
            ],
            "jira": [
                {
                    "name": "test-jira",
                    "base_url": "https://test.atlassian.net",
                    "projects": ["TEST"],
                    "auth": {
                        "mode": "api_token",
                        "username": "user@test.com",
                        "secret_ref": "workctx/test/jira",
                    },
                }
            ],
            "confluence": [
                {
                    "name": "test-wiki",
                    "base_url": "https://test.atlassian.net",
                    "spaces": ["TEST"],
                    "auth": {
                        "mode": "api_token",
                        "username": "user@test.com",
                        "secret_ref": "workctx/test/confluence",
                    },
                }
            ],
        },
        "notifications": {
            "telegram": {"enabled": False},
            "macos": {"enabled": True},
        },
    }


@pytest.fixture
def config_file(sample_config_dict, tmp_path):
    config_path = tmp_path / "test.yaml"
    with open(config_path, "w") as f:
        yaml.dump(sample_config_dict, f)
    return config_path


def test_load_config(config_file):
    config = load_config(config_file)
    assert config.version == 1
    assert config.project.id == "test-project"
    assert config.project.name == "Test Project"


def test_config_state_dir():
    config = ProjectConfig(
        project=ProjectInfo(
            id="my-project",
            name="My Project",
            output_root="/tmp/test",
        )
    )
    assert "WorkContextMirror" in str(config.state_dir)
    assert "my-project" in str(config.state_dir)


def test_config_all_source_names(sample_config_dict):
    config = ProjectConfig.model_validate(sample_config_dict)
    names = config.all_source_names()
    assert "test-wiki" in names
    assert "test-jira" in names
    assert "test-docs" in names


def test_load_missing_file():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path.yaml")


def test_config_defaults():
    config = ProjectConfig(
        project=ProjectInfo(
            id="minimal",
            name="Minimal",
            output_root="/tmp/test",
        )
    )
    assert config.sync.overlap_minutes == 15
    assert config.sync.reconciliation_days == 7
    assert config.sync.max_concurrency == 4
    assert config.schedule.hour == 5
    assert config.schedule.minute == 30


def test_sharepoint_default_excludes():
    sp = SharePointSource(name="test", local_path="/tmp/test")
    assert "**/~$*" in sp.exclude
    assert "**/.DS_Store" in sp.exclude


def test_auth_config_defaults():
    auth = AuthConfig()
    assert auth.mode == "api_token"
    assert auth.username is None
    assert auth.secret_ref is None


def test_duplicate_source_names_rejected():
    """Source names must be unique across all source types."""
    from pydantic import ValidationError

    from workctx.config import SourcesConfig

    with pytest.raises(ValidationError, match="Duplicate source name"):
        SourcesConfig(
            jira=[
                {
                    "name": "my-source",
                    "base_url": "https://test.atlassian.net",
                    "projects": ["PROJ"],
                    "auth": {"secret_ref": "jira-token"},
                }
            ],
            confluence=[
                {
                    "name": "my-source",
                    "base_url": "https://test.atlassian.net",
                    "spaces": ["ENG"],
                    "auth": {"secret_ref": "conf-token"},
                }
            ],
        )


def test_duplicate_names_within_same_type_rejected():
    from pydantic import ValidationError

    from workctx.config import SourcesConfig

    with pytest.raises(ValidationError, match="Duplicate source name"):
        SourcesConfig(
            sharepoint=[
                {"name": "docs", "local_path": "/tmp/a"},
                {"name": "docs", "local_path": "/tmp/b"},
            ],
        )


def test_unique_source_names_accepted():
    from workctx.config import SourcesConfig

    cfg = SourcesConfig(
        sharepoint=[
            {"name": "sp-docs", "local_path": "/tmp/a"},
        ],
        local_folders=[
            {"name": "local-docs", "paths": ["/tmp/b"]},
        ],
    )
    assert len(cfg.sharepoint) == 1
    assert len(cfg.local_folders) == 1
