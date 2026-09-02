"""YAML configuration loading and Pydantic models."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator


class AuthConfig(BaseModel):
    mode: Literal["api_token", "pat", "basic", "browser"] = "api_token"
    username: str | None = None
    secret_ref: str | None = None


class ConfluenceSource(BaseModel):
    name: str
    base_url: str
    deployment: Literal["auto", "cloud", "datacenter"] = "auto"
    spaces: list[str]
    auth: AuthConfig
    include_attachments: bool = False


class JiraSource(BaseModel):
    name: str
    base_url: str
    deployment: Literal["auto", "cloud", "datacenter"] = "auto"
    projects: list[str]
    auth: AuthConfig
    include_comments: bool = True
    include_changelog: bool = False
    include_attachments: bool = False
    custom_fields_include: list[str] | None = None
    custom_fields_exclude: list[str] | None = None


class SharePointSource(BaseModel):
    name: str
    site_url: str | None = None
    mode: Literal["onedrive_local", "browser"] = "onedrive_local"
    local_path: str | None = None
    doc_library: str = "Shared Documents"
    server_relative_path: str | None = None
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(default_factory=lambda: ["**/~$*", "**/.DS_Store", "**/*.tmp"])
    auth: AuthConfig | None = None


class LocalFolderSource(BaseModel):
    name: str
    paths: list[str]
    include: list[str] = Field(default_factory=lambda: ["**/*"])
    exclude: list[str] = Field(default_factory=lambda: ["**/~$*", "**/.DS_Store", "**/*.tmp"])


class SourcesConfig(BaseModel):
    confluence: list[ConfluenceSource] = Field(default_factory=list)
    jira: list[JiraSource] = Field(default_factory=list)
    sharepoint: list[SharePointSource] = Field(default_factory=list)
    local_folders: list[LocalFolderSource] = Field(default_factory=list)


class ScheduleConfig(BaseModel):
    hour: int = 5
    minute: int = 30


class SyncConfig(BaseModel):
    overlap_minutes: int = 15
    reconciliation_days: int = 7
    max_concurrency: int = 4
    large_document_chars: int = 300_000


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token_ref: str | None = None
    chat_id_ref: str | None = None


class MacOSNotificationConfig(BaseModel):
    enabled: bool = True


class NotificationsConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    macos: MacOSNotificationConfig = Field(default_factory=MacOSNotificationConfig)


class ProjectInfo(BaseModel):
    id: str
    name: str
    output_root: str
    state_dir: str | None = None

    @field_validator("output_root")
    @classmethod
    def expand_output_root(cls, v: str) -> str:
        return str(Path(v).expanduser())

    @field_validator("state_dir")
    @classmethod
    def expand_state_dir(cls, v: str | None) -> str | None:
        if v:
            return str(Path(v).expanduser())
        return v


class ProjectConfig(BaseModel):
    """Root configuration model for a Work Context Mirror project."""

    version: int = 1
    project: ProjectInfo
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    @property
    def state_dir(self) -> Path:
        if self.project.state_dir:
            return Path(self.project.state_dir)
        import platform

        system = platform.system()
        if system == "Darwin":
            base = Path.home() / "Library" / "Application Support"
        elif system == "Windows":
            base = Path.home() / "AppData" / "Local"
        else:
            base = Path.home() / ".local" / "share"
        return base / "WorkContextMirror" / self.project.id

    @property
    def output_root_path(self) -> Path:
        return Path(self.project.output_root)

    def all_source_names(self) -> list[str]:
        names: list[str] = []
        for src in self.sources.confluence:
            names.append(src.name)
        for src in self.sources.jira:
            names.append(src.name)
        for src in self.sources.sharepoint:
            names.append(src.name)
        for src in self.sources.local_folders:
            names.append(src.name)
        return names


def load_config(path: str | Path) -> ProjectConfig:
    """Load and validate a project configuration from a YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Invalid configuration file: {config_path}")

    return ProjectConfig.model_validate(raw)
