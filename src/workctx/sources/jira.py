"""Jira source adapter — Cloud and Data Center."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from workctx.config import JiraSource
from workctx.models import (
    ChangeAction,
    DiscoveredChange,
    SourceType,
    SyncCheckpoint,
)
from workctx.secrets import get_secret
from workctx.sources.base import Source
from workctx.state import StateDB

logger = logging.getLogger(__name__)

MAX_RESULTS = 100
REQUEST_TIMEOUT = 120.0


class JiraAdapter(Source):
    """Jira source adapter supporting both Cloud (API v3) and Data Center (API v2)."""

    def __init__(self, config: JiraSource) -> None:
        self.config = config
        self._client: httpx.Client | None = None
        self._field_map: dict[str, str] | None = None
        self._is_dc: bool | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def source_type(self) -> SourceType:
        return SourceType.JIRA

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.config.base_url:
            issues.append(f"{self.name}: base_url is required")
        if not self.config.projects:
            issues.append(f"{self.name}: at least one project is required")
        secret = get_secret(self.config.auth.secret_ref or "")
        if not secret:
            issues.append(
                f"{self.name}: no API token found for secret_ref '{self.config.auth.secret_ref}'"
            )
        return issues

    def _detect_deployment(self, secret: str) -> bool:
        """Detect whether this is Cloud or Data Center. Returns True for DC."""
        if self.config.deployment == "cloud":
            return False
        if self.config.deployment == "datacenter":
            return True
        base = self.config.base_url.rstrip("/")
        if ".atlassian.net" in base:
            return False
        try:
            resp = httpx.get(
                f"{base}/rest/api/2/myself",
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Accept": "application/json",
                },
                timeout=10.0,
            )
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        return False

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            secret = get_secret(self.config.auth.secret_ref or "")
            if not secret:
                raise RuntimeError(f"No API token for {self.name}")

            base = self.config.base_url.rstrip("/")
            if self._is_dc is None:
                self._is_dc = self._detect_deployment(secret)

            if self._is_dc or self.config.auth.mode == "pat":
                api_version = "2"
                headers = {
                    "Authorization": f"Bearer {secret}",
                    "Accept": "application/json",
                }
                auth = None
            else:
                api_version = "3"
                headers = {"Accept": "application/json"}
                if self.config.auth.username:
                    auth = httpx.BasicAuth(self.config.auth.username, secret)
                else:
                    auth = httpx.BasicAuth("", secret)

            logger.info(
                "Jira/%s: using API v%s (%s)",
                self.name,
                api_version,
                "DC" if self._is_dc else "Cloud",
            )
            self._client = httpx.Client(
                base_url=f"{base}/rest/api/{api_version}",
                auth=auth,
                timeout=REQUEST_TIMEOUT,
                headers=headers,
            )
        return self._client

    def discover_changes(
        self,
        db: StateDB,
        checkpoint: SyncCheckpoint | None,
        *,
        full: bool = False,
    ) -> list[DiscoveredChange]:
        client = self._get_client()
        self._load_field_map(client)
        projects_clause = ", ".join(self.config.projects)

        if full or not checkpoint or not checkpoint.last_checkpoint:
            jql = f"project IN ({projects_clause}) ORDER BY updated ASC"
        else:
            since = self._checkpoint_with_overlap(checkpoint.last_checkpoint)
            jql = f'project IN ({projects_clause}) AND updated >= "{since}" ORDER BY updated ASC'

        logger.info("Jira/%s: JQL = %s", self.name, jql)
        changes: list[DiscoveredChange] = []
        start_at = 0

        while True:
            data = self._search(client, jql, start_at)
            issues = data.get("issues", [])
            if not issues:
                break

            for issue in issues:
                change = self._issue_to_change(client, issue)
                if change:
                    existing = db.get_object(self.name, change.source_id)
                    if existing and existing.source_version == change.source_version:
                        continue
                    change.action = ChangeAction.ADD if not existing else ChangeAction.UPDATE
                    changes.append(change)

            start_at += len(issues)
            if start_at >= data.get("total", 0):
                break

        logger.info("Jira/%s: %d changes discovered", self.name, len(changes))
        return changes

    def get_current_ids(self) -> set[str]:
        client = self._get_client()
        projects_clause = ", ".join(self.config.projects)
        jql = f"project IN ({projects_clause}) ORDER BY key ASC"
        ids: set[str] = set()
        start_at = 0

        while True:
            data = self._search(client, jql, start_at, fields=["key"])
            issues = data.get("issues", [])
            if not issues:
                break
            for issue in issues:
                ids.add(issue["id"])
            start_at += len(issues)
            if start_at >= data.get("total", 0):
                break

        return ids

    def _search(
        self,
        client: httpx.Client,
        jql: str,
        start_at: int,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": MAX_RESULTS,
        }
        if fields:
            params["fields"] = ",".join(fields)
        resp = client.get("/search", params=params)
        resp.raise_for_status()
        return resp.json()

    def _issue_to_change(
        self,
        client: httpx.Client,
        issue: dict[str, Any],
    ) -> DiscoveredChange | None:
        fields = issue.get("fields", {})
        issue_id = issue["id"]
        issue_key = issue.get("key", "")
        summary = fields.get("summary", "")
        updated = fields.get("updated", "")
        base = self.config.base_url.rstrip("/")
        source_url = f"{base}/browse/{issue_key}"

        content_text = self._render_issue(issue, fields, client)
        if content_text is None:
            return None

        return DiscoveredChange(
            source_id=issue_id,
            source_key=issue_key,
            title=summary,
            source_url=source_url,
            source_version=updated,
            source_updated_at=_parse_jira_datetime(updated),
            action=ChangeAction.ADD,
            content_text=content_text,
            metadata={
                "project": fields.get("project", {}).get("key", ""),
            },
        )

    def _render_issue(
        self,
        issue: dict[str, Any],
        fields: dict[str, Any],
        client: httpx.Client,
    ) -> str | None:
        """Render a Jira issue as Markdown content (without front matter)."""
        from workctx.normalise.atlassian import adf_to_markdown

        issue_key = issue.get("key", "UNKNOWN")
        summary = fields.get("summary", "Untitled")
        lines: list[str] = []
        lines.append(f"# {issue_key}: {summary}")
        lines.append("")

        meta_fields = [
            ("Type", _nested_name(fields.get("issuetype"))),
            ("Status", _nested_name(fields.get("status"))),
            ("Priority", _nested_name(fields.get("priority"))),
            ("Resolution", _nested_name(fields.get("resolution"))),
            ("Reporter", _nested_name(fields.get("reporter"), key="displayName")),
            ("Assignee", _nested_name(fields.get("assignee"), key="displayName")),
            ("Created", fields.get("created", "")),
            ("Updated", fields.get("updated", "")),
        ]

        labels = fields.get("labels")
        if labels:
            meta_fields.append(("Labels", ", ".join(labels)))

        components = fields.get("components", [])
        if components:
            meta_fields.append(("Components", ", ".join(c.get("name", "") for c in components)))

        fix_versions = fields.get("fixVersions", [])
        if fix_versions:
            meta_fields.append(("Fix Versions", ", ".join(v.get("name", "") for v in fix_versions)))

        sprint_field = self._find_custom_field(fields, "Sprint")
        if sprint_field:
            meta_fields.append(("Sprint", str(sprint_field)))

        epic_field = self._find_custom_field(fields, "Epic Link") or self._find_custom_field(
            fields, "Epic Name"
        )
        if epic_field:
            meta_fields.append(("Epic", str(epic_field)))

        parent = fields.get("parent")
        if parent:
            parent_key = parent.get("key", "")
            parent_summary = parent.get("fields", {}).get("summary", "")
            meta_fields.append(("Parent", f"{parent_key}: {parent_summary}"))

        lines.append("## Details")
        lines.append("")
        for label, value in meta_fields:
            if value:
                lines.append(f"- **{label}**: {value}")
        lines.append("")

        description = fields.get("description")
        if description:
            lines.append("## Description")
            lines.append("")
            if isinstance(description, dict):
                lines.append(adf_to_markdown(description))
            else:
                lines.append(str(description))
            lines.append("")

        if self._field_map:
            custom_lines = self._render_custom_fields(fields)
            if custom_lines:
                lines.extend(custom_lines)

        links = fields.get("issuelinks", [])
        if links:
            lines.append("## Links")
            lines.append("")
            for link in links:
                if "outwardIssue" in link:
                    target = link["outwardIssue"]
                    direction = link.get("type", {}).get("outward", "relates to")
                    lines.append(
                        f"- {direction} [{target.get('key', '')}] "
                        f"{target.get('fields', {}).get('summary', '')}"
                    )
                elif "inwardIssue" in link:
                    target = link["inwardIssue"]
                    direction = link.get("type", {}).get("inward", "is related to")
                    lines.append(
                        f"- {direction} [{target.get('key', '')}] "
                        f"{target.get('fields', {}).get('summary', '')}"
                    )
            lines.append("")

        if self.config.include_comments:
            comments_data = self._fetch_comments(client, issue["id"])
            if comments_data:
                lines.append("## Comments")
                lines.append("")
                for comment in comments_data:
                    author = comment.get("author", {}).get("displayName", "Unknown")
                    created = comment.get("created", "")
                    body = comment.get("body", {})
                    lines.append(f"### {author} — {created[:10]}")
                    lines.append("")
                    if isinstance(body, dict):
                        lines.append(adf_to_markdown(body))
                    else:
                        lines.append(str(body))
                    lines.append("")

        attachments = fields.get("attachment", [])
        if attachments and not self.config.include_attachments:
            lines.append("## Attachments")
            lines.append("")
            for att in attachments:
                name = att.get("filename", "unknown")
                size = att.get("size", 0)
                mime = att.get("mimeType", "unknown")
                lines.append(f"- {name} ({mime}, {_human_size(size)})")
            lines.append("")

        return "\n".join(lines)

    def _fetch_comments(self, client: httpx.Client, issue_id: str) -> list[dict[str, Any]]:
        """Fetch all comments for an issue, handling pagination."""
        comments: list[dict[str, Any]] = []
        start_at = 0
        while True:
            resp = client.get(
                f"/issue/{issue_id}/comment",
                params={"startAt": start_at, "maxResults": MAX_RESULTS},
            )
            if resp.status_code != 200:
                logger.warning("Failed to fetch comments for issue %s", issue_id)
                break
            data = resp.json()
            batch = data.get("comments", [])
            comments.extend(batch)
            start_at += len(batch)
            if start_at >= data.get("total", 0):
                break
        return comments

    def _load_field_map(self, client: httpx.Client) -> None:
        """Load field metadata to map custom field IDs to names."""
        if self._field_map is not None:
            return
        try:
            resp = client.get("/field")
            if resp.status_code == 200:
                self._field_map = {f["id"]: f.get("name", f["id"]) for f in resp.json()}
            else:
                self._field_map = {}
        except Exception:
            logger.debug("Failed to load field metadata", exc_info=True)
            self._field_map = {}

    def _find_custom_field(self, fields: dict[str, Any], name: str) -> Any:
        if not self._field_map:
            return None
        for field_id, field_name in self._field_map.items():
            if field_name.lower() == name.lower() and field_id in fields:
                val = fields[field_id]
                if val is not None:
                    if isinstance(val, dict):
                        return val.get("name") or val.get("value") or str(val)
                    if isinstance(val, list):
                        return ", ".join(
                            item.get("name", str(item)) if isinstance(item, dict) else str(item)
                            for item in val
                        )
                    return val
        return None

    def _render_custom_fields(self, fields: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        rendered_any = False
        if not self._field_map:
            return lines
        for field_id, field_name in self._field_map.items():
            if not field_id.startswith("customfield_"):
                continue
            if field_id not in fields or fields[field_id] is None:
                continue
            if (
                self.config.custom_fields_include
                and field_name not in self.config.custom_fields_include
            ):
                continue
            if (
                self.config.custom_fields_exclude
                and field_name in self.config.custom_fields_exclude
            ):
                continue

            val = fields[field_id]
            rendered = _render_field_value(val)
            if rendered:
                if not rendered_any:
                    lines.append("## Custom Fields")
                    lines.append("")
                    rendered_any = True
                lines.append(f"- **{field_name}**: {rendered}")

        if rendered_any:
            lines.append("")
        return lines

    def _checkpoint_with_overlap(self, checkpoint: str) -> str:
        try:
            dt = datetime.fromisoformat(checkpoint)
            adjusted = dt - timedelta(minutes=15)
            return adjusted.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return checkpoint

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None


def _nested_name(obj: Any, key: str = "name") -> str:
    if not obj:
        return ""
    if isinstance(obj, dict):
        return obj.get(key, "")
    return str(obj)


def _render_field_value(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("name") or val.get("value") or val.get("displayName") or ""
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict):
                parts.append(item.get("name") or item.get("value") or str(item))
            else:
                parts.append(str(item))
        return ", ".join(parts)
    return str(val)


def _parse_jira_datetime(val: str) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size //= 1024
    return f"{size:.1f} TB"
