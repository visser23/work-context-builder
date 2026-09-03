"""Shared domain models for Work Context Mirror."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SourceType(enum.StrEnum):
    CONFLUENCE = "confluence"
    JIRA = "jira"
    SHAREPOINT = "sharepoint"
    LOCAL_FOLDER = "local_folder"


class RunStatus(enum.StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class SourceObject(BaseModel):
    """A tracked object from a source system."""

    id: int | None = None
    source_name: str
    source_type: SourceType
    source_id: str
    source_key: str | None = None
    title: str | None = None
    source_url: str | None = None
    source_version: str | None = None
    source_updated_at: datetime | None = None
    content_sha256: str | None = None
    output_path: str | None = None
    file_size: int | None = None
    file_mtime: float | None = None
    last_processed_at: datetime | None = None
    last_error: str | None = None
    retry_count: int = 0
    sp_item_id: int | None = None


class SyncCheckpoint(BaseModel):
    """Persisted sync checkpoint for a source."""

    source_name: str
    source_type: SourceType
    last_checkpoint: str | None = None
    last_success: datetime | None = None
    last_reconciliation: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChangeAction(enum.StrEnum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"


class DiscoveredChange(BaseModel):
    """A change detected by a source adapter."""

    source_id: str
    source_key: str | None = None
    title: str | None = None
    source_url: str | None = None
    source_version: str | None = None
    source_updated_at: datetime | None = None
    action: ChangeAction
    content: bytes | None = None
    content_text: str | None = None
    local_path: str | None = None
    file_size: int | None = None
    file_mtime: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FrontMatter(BaseModel):
    """YAML front matter for normalised Markdown output."""

    workctx_version: int = 1
    source_type: str
    source_name: str
    source_id: str
    title: str
    source_url: str | None = None
    source_key: str | None = None
    issue_key: str | None = None
    project: str | None = None
    space: str | None = None
    source_path: str | None = None
    status: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    source_version: int | str | None = None
    content_sha256: str | None = None
    synced_at: datetime | None = None
    part_number: int | None = None
    total_parts: int | None = None
    parent_source_id: str | None = None

    def to_yaml_str(self) -> str:
        """Render as YAML front matter block."""
        lines = ["---"]
        for key, value in self.model_dump(exclude_none=True).items():
            if isinstance(value, datetime):
                lines.append(f"{key}: {value.isoformat()}")
            elif isinstance(value, int | float):
                lines.append(f"{key}: {value}")
            else:
                safe = str(value).replace('"', '\\"')
                if any(c in safe for c in ":#{}[]|>&*!%@`"):
                    lines.append(f'{key}: "{safe}"')
                else:
                    lines.append(f"{key}: {safe}")
        lines.append("---")
        return "\n".join(lines)


class ManifestEntry(BaseModel):
    """One line of _meta/manifest.jsonl."""

    source_type: str
    source_name: str
    source_id: str
    source_key: str | None = None
    output_path: str
    title: str | None = None
    source_url: str | None = None
    updated_at: datetime | None = None
    synced_at: datetime | None = None
    content_sha256: str | None = None


class SourceResult(BaseModel):
    """Result of syncing one source."""

    source_name: str
    source_type: SourceType
    status: RunStatus = RunStatus.HEALTHY
    objects_checked: int = 0
    objects_added: int = 0
    objects_updated: int = 0
    objects_deleted: int = 0
    objects_failed: int = 0
    errors: list[str] = Field(default_factory=list)


class SyncResult(BaseModel):
    """Aggregate result of a full sync run."""

    run_id: str
    started_at: datetime
    completed_at: datetime | None = None
    status: RunStatus = RunStatus.HEALTHY
    source_results: list[SourceResult] = Field(default_factory=list)

    def aggregate_status(self) -> RunStatus:
        if any(r.status == RunStatus.FAILED for r in self.source_results):
            return RunStatus.FAILED
        if any(r.status == RunStatus.DEGRADED for r in self.source_results):
            return RunStatus.DEGRADED
        return RunStatus.HEALTHY
