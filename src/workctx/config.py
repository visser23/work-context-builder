"""YAML configuration loading and Pydantic models."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

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


class BrowserSessionSource(BaseModel):
    """Base for sources that read a web app through a logged-in browser profile.

    Email, calendar and Slack have no app registration or API token here: they
    reuse the persistent Playwright browser profile that already holds your
    single-sign-on session (the same one a ``mode: browser`` SharePoint source
    uses). Credentials are read from that profile at sync time and kept only in
    memory.
    """

    name: str
    sharepoint_source: str | None = None
    """Name of a ``mode: browser`` SharePoint source whose browser profile
    (and therefore SSO session) is reused. Recommended."""
    profile: str | None = None
    """Standalone alternative: a browser-profile name. Create it with
    ``workctx auth login-web --source <name>``."""

    @model_validator(mode="after")
    def _needs_profile(self) -> BrowserSessionSource:
        if not (self.sharepoint_source or self.profile):
            raise ValueError(
                f"Source '{self.name}' needs either 'sharepoint_source' (reuse an existing "
                "browser SharePoint login) or 'profile' (a standalone browser profile name)."
            )
        return self

    def profile_name(self, sharepoint_sources: dict[str, SharePointSource] | None = None) -> str:
        """Browser-profile directory name this source logs in with."""
        if self.sharepoint_source:
            sp = (sharepoint_sources or {}).get(self.sharepoint_source)
            return sp.name if sp else self.sharepoint_source
        assert self.profile
        return self.profile


class MailSource(BrowserSessionSource):
    """Outlook / Exchange Online mailbox, read through Outlook on the web."""

    mailbox_url: str = "https://outlook.office.com/mail/"
    """Outlook on the web start page (used for login and to obtain an access token)."""
    api_base: str = "https://outlook.office.com/api/v2.0"
    folders: list[str] = Field(default_factory=lambda: ["inbox", "sentitems"])
    """Well-known folder names (inbox, sentitems, archive, drafts, deleteditems,
    junkemail) or display names of your own folders."""
    since_days: int = Field(default=90, ge=1)
    """How far back to read (use 3650 for 'everything'). Older messages already in
    the corpus are kept."""
    max_messages: int = Field(default=50_000, ge=1)
    """Safety cap on messages per folder per run."""
    exclude_senders: list[str] = Field(default_factory=list)
    """Case-insensitive globs on the sender address or name; matches are skipped."""
    exclude_subjects: list[str] = Field(default_factory=list)
    """Case-insensitive globs on the subject; matches are skipped."""
    max_body_chars: int = Field(default=20_000, ge=500)
    trim_quoted_replies: bool = True
    """Cut the quoted earlier messages from replies (they are in their own files)."""


class CalendarSource(BrowserSessionSource):
    """Outlook calendar events (past and upcoming), one file per event."""

    mailbox_url: str = "https://outlook.office.com/mail/"
    api_base: str = "https://outlook.office.com/api/v2.0"
    past_days: int = Field(default=90, ge=0)
    future_days: int = Field(default=60, ge=0)
    include_cancelled: bool = False
    exclude_titles: list[str] = Field(default_factory=list)
    """Case-insensitive globs on the event title; matches are skipped."""
    max_body_chars: int = Field(default=5_000, ge=0)


class SlackSource(BrowserSessionSource):
    """Slack messages from a workspace (or Enterprise Grid org) you are logged in to.

    One Markdown file per conversation per day. Uses your browser session, so it
    sees exactly what you see in Slack; nothing is posted or marked as read.
    """

    client_url: str
    """Your Slack web client URL, e.g. ``https://app.slack.com/client/E0XXXXXXXX/``."""
    since_days: int = Field(default=30, ge=1)
    conversation_types: list[Literal["public_channel", "private_channel", "mpim", "im"]] = Field(
        default_factory=lambda: ["public_channel", "private_channel", "mpim", "im"]
    )
    workspaces: list[str] = Field(default_factory=list)
    """Globs on workspace id, domain or name. Empty = every workspace in the session."""
    include_channels: list[str] = Field(default_factory=list)
    """Globs on channel name (or DM participant names). Empty = all."""
    exclude_channels: list[str] = Field(default_factory=list)
    include_threads: bool = True
    max_channels: int = Field(default=500, ge=1)
    signin_url: str | None = None
    """Your workspace's own sign-in page (e.g. ``https://<org>.enterprise.slack.com/``).
    Used to sign back in silently through SSO when the Slack session has lapsed."""
    sso_button_text: str = "Sign in with"
    """Text of the single-sign-on button on Slack's sign-in page (clicked
    automatically when the session has lapsed)."""


class SourcesConfig(BaseModel):
    confluence: list[ConfluenceSource] = Field(default_factory=list)
    jira: list[JiraSource] = Field(default_factory=list)
    sharepoint: list[SharePointSource] = Field(default_factory=list)
    local_folders: list[LocalFolderSource] = Field(default_factory=list)
    transcripts: list[TranscriptsSource] = Field(default_factory=list)
    mail: list[MailSource] = Field(default_factory=list)
    calendar: list[CalendarSource] = Field(default_factory=list)
    slack: list[SlackSource] = Field(default_factory=list)

    def all_names(self) -> list[str]:
        """Names of every configured source, across all source types."""
        groups: list[list[Any]] = [
            self.confluence,
            self.jira,
            self.sharepoint,
            self.local_folders,
            self.transcripts,
            self.mail,
            self.calendar,
            self.slack,
        ]
        return [src.name for group in groups for src in group]

    @model_validator(mode="after")
    def _unique_source_names(self) -> SourcesConfig:
        names = self.all_names()
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise ValueError(
                    f"Duplicate source name '{name}'. "
                    "Source names must be unique across all source types."
                )
            seen.add(name)

        browser_sp = {sp.name for sp in self.sharepoint if sp.mode == "browser"}
        browser_users: list[Any] = [*self.transcripts, *self.mail, *self.calendar, *self.slack]
        for src in browser_users:
            if src.sharepoint_source and src.sharepoint_source not in browser_sp:
                raise ValueError(
                    f"Source '{src.name}' references sharepoint_source "
                    f"'{src.sharepoint_source}', which is not a 'mode: browser' SharePoint source."
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
        return self.sources.all_names()


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
