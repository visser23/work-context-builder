"""YAML configuration loading and Pydantic models."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


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


class TranscriptsSource(BaseModel):
    """Microsoft Teams meeting transcripts, pulled via SharePoint/OneDrive browser cookies.

    Teams stores meeting recordings (and their transcripts) in the organiser's
    OneDrive or a team site, then shares them with attendees. This source uses
    the same browser session as a ``mode: browser`` SharePoint source to find
    every recording visible to the logged-in user and downloads its transcript.
    """

    name: str
    sharepoint_source: str | None = None
    """Name of a ``mode: browser`` SharePoint source whose login (cookies and
    browser profile) is reused. Recommended."""
    site_url: str | None = None
    """Any URL on your SharePoint tenant. Only needed when ``sharepoint_source``
    is not set (standalone login)."""
    auth: AuthConfig | None = None
    include_own: bool = True
    """Recordings stored in the logged-in user's own OneDrive (meetings they organised)."""
    include_shared: bool = True
    """Recordings other people's OneDrives have shared directly with the user."""
    include_invited: bool = True
    """Every meeting the user was invited to, wherever it was recorded: other
    people's OneDrives (Teams only grants those to invited participants), Teams
    team sites, and any SharePoint site where the user is a contributor
    (including the ``sharepoint_source`` project site). SharePoint Search is
    security-trimmed, so only recordings the user can open are ever seen."""
    include_all_sites: bool = False
    """Also include recordings on any *other* SharePoint site the user merely has
    read access to (e.g. organisation-wide webinars, directorate sites). Can add
    hundreds of meetings the user was not invited to, so it is off by default."""
    sites: list[str] = Field(default_factory=list)
    """Extra SharePoint site URLs whose meeting recordings are always included."""
    since_days: int | None = Field(default=None, ge=1)
    """Only include recordings modified in the last N days (default: all)."""
    exclude_titles: list[str] = Field(default_factory=list)
    """Case-insensitive glob patterns; matching meeting titles are skipped."""

    @model_validator(mode="after")
    def _needs_login_source(self) -> TranscriptsSource:
        if self.sharepoint_source:
            return self
        if not self.site_url:
            raise ValueError(
                f"Transcripts source '{self.name}' needs either 'sharepoint_source' "
                "(reuse an existing browser SharePoint login) or 'site_url'."
            )
        if not self.auth or not self.auth.secret_ref:
            raise ValueError(
                f"Transcripts source '{self.name}': standalone mode needs auth.secret_ref "
                "for cookie storage."
            )
        return self


class SourcesConfig(BaseModel):
    confluence: list[ConfluenceSource] = Field(default_factory=list)
    jira: list[JiraSource] = Field(default_factory=list)
    sharepoint: list[SharePointSource] = Field(default_factory=list)
    local_folders: list[LocalFolderSource] = Field(default_factory=list)
    transcripts: list[TranscriptsSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_source_names(self) -> SourcesConfig:
        names: list[str] = []
        for src in self.confluence:
            names.append(src.name)
        for src in self.jira:
            names.append(src.name)
        for src in self.sharepoint:
            names.append(src.name)
        for src in self.local_folders:
            names.append(src.name)
        for tx_src in self.transcripts:
            names.append(tx_src.name)
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise ValueError(
                    f"Duplicate source name '{name}'. "
                    "Source names must be unique across all source types."
                )
            seen.add(name)

        browser_sp = {sp.name for sp in self.sharepoint if sp.mode == "browser"}
        for tx in self.transcripts:
            if tx.sharepoint_source and tx.sharepoint_source not in browser_sp:
                raise ValueError(
                    f"Transcripts source '{tx.name}' references sharepoint_source "
                    f"'{tx.sharepoint_source}', which is not a 'mode: browser' SharePoint source."
                )
        return self


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
        for tx_src in self.sources.transcripts:
            names.append(tx_src.name)
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
