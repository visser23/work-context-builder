"""Confluence source adapter — Cloud and Data Center."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from workctx.config import ConfluenceSource
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

PAGE_LIMIT = 100
REQUEST_TIMEOUT = 120.0


class ConfluenceAdapter(Source):
    """Confluence source adapter supporting both Cloud and Data Center."""

    def __init__(self, config: ConfluenceSource) -> None:
        self.config = config
        self._client: httpx.Client | None = None
        self._is_dc: bool | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def source_type(self) -> SourceType:
        return SourceType.CONFLUENCE

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.config.base_url:
            issues.append(f"{self.name}: base_url is required")
        if not self.config.spaces:
            issues.append(f"{self.name}: at least one space is required")
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
        for api_base in [f"{base}/rest/api", f"{base}/wiki/rest/api"]:
            try:
                resp = httpx.get(
                    f"{api_base}/space",
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "Accept": "application/json",
                    },
                    params={"limit": 1},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    self._api_base = api_base
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
                api_base = getattr(self, "_api_base", f"{base}/rest/api")
                headers = {
                    "Authorization": f"Bearer {secret}",
                    "Accept": "application/json",
                }
                auth = None
            else:
                api_base = f"{base}/wiki/rest/api"
                headers = {"Accept": "application/json"}
                if self.config.auth.username:
                    auth = httpx.BasicAuth(self.config.auth.username, secret)
                else:
                    auth = httpx.BasicAuth("", secret)

            logger.info(
                "Confluence/%s: using %s (%s)",
                self.name,
                api_base,
                "DC" if self._is_dc else "Cloud",
            )
            self._client = httpx.Client(
                base_url=api_base,
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
        changes: list[DiscoveredChange] = []

        for space_key in self.config.spaces:
            if full or not checkpoint or not checkpoint.last_checkpoint:
                space_changes = self._enumerate_all_pages(client, space_key, db)
            else:
                space_changes = self._enumerate_updated_pages(client, space_key, checkpoint, db)
            changes.extend(space_changes)

        logger.info("Confluence/%s: %d changes discovered", self.name, len(changes))
        return changes

    def get_current_ids(self) -> set[str]:
        """Get all current page IDs across configured spaces."""
        client = self._get_client()
        ids: set[str] = set()
        for space_key in self.config.spaces:
            ids.update(self._get_space_page_ids(client, space_key))
        return ids

    def _enumerate_all_pages(
        self, client: httpx.Client, space_key: str, db: StateDB
    ) -> list[DiscoveredChange]:
        """Enumerate all pages in a space (initial sync)."""
        changes: list[DiscoveredChange] = []
        start = 0

        while True:
            resp = client.get(
                "/content",
                params={
                    "spaceKey": space_key,
                    "type": "page",
                    "status": "current",
                    "expand": "version,body.storage",
                    "limit": PAGE_LIMIT,
                    "start": start,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break

            for page in results:
                change = self._page_to_change(page, space_key, db)
                if change:
                    changes.append(change)

            if data.get("size", 0) < PAGE_LIMIT:
                break
            start += len(results)

        return changes

    def _enumerate_updated_pages(
        self,
        client: httpx.Client,
        space_key: str,
        checkpoint: SyncCheckpoint,
        db: StateDB,
    ) -> list[DiscoveredChange]:
        """Query for pages updated since the checkpoint."""
        since = self._checkpoint_with_overlap(checkpoint.last_checkpoint or "")
        cql = f'space = "{space_key}" AND type = "page" AND lastmodified >= "{since}"'

        changes: list[DiscoveredChange] = []
        start = 0

        while True:
            resp = client.get(
                "/content/search",
                params={
                    "cql": cql,
                    "expand": "version,body.storage",
                    "limit": PAGE_LIMIT,
                    "start": start,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break

            for page in results:
                change = self._page_to_change(page, space_key, db)
                if change:
                    changes.append(change)

            if data.get("size", 0) < PAGE_LIMIT:
                break
            start += len(results)

        return changes

    def _page_to_change(
        self, page: dict[str, Any], space_key: str, db: StateDB
    ) -> DiscoveredChange | None:
        """Convert a Confluence page API response to a DiscoveredChange."""
        from workctx.normalise.atlassian import confluence_storage_to_markdown

        page_id = str(page.get("id", ""))
        title = page.get("title", "Untitled")
        version_info = page.get("version", {})
        version_num = str(version_info.get("number", ""))
        updated = version_info.get("when", "")

        existing = db.get_object(self.name, page_id)
        if existing and existing.source_version == version_num:
            return None

        storage_body = page.get("body", {}).get("storage", {}).get("value", "")
        base = self.config.base_url.rstrip("/")

        links = page.get("_links", {})
        if "webui" in links:
            webui = links["webui"]
            source_url = f"{base}{webui}" if self._is_dc else f"{base}/wiki{webui}"
        elif self._is_dc:
            source_url = f"{base}/pages/viewpage.action?pageId={page_id}"
        else:
            source_url = f"{base}/wiki/spaces/{space_key}/pages/{page_id}"

        content_md = confluence_storage_to_markdown(storage_body, title)

        action = ChangeAction.ADD if not existing else ChangeAction.UPDATE

        return DiscoveredChange(
            source_id=page_id,
            title=title,
            source_url=source_url,
            source_version=version_num,
            source_updated_at=_parse_confluence_datetime(updated),
            action=action,
            content_text=content_md,
            metadata={
                "space": space_key,
                "created": page.get("history", {}).get("createdDate", ""),
            },
        )

    def _get_space_page_ids(self, client: httpx.Client, space_key: str) -> set[str]:
        ids: set[str] = set()
        start = 0
        while True:
            resp = client.get(
                "/content",
                params={
                    "spaceKey": space_key,
                    "type": "page",
                    "status": "current",
                    "limit": PAGE_LIMIT,
                    "start": start,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break
            for page in results:
                ids.add(str(page["id"]))
            if data.get("size", 0) < PAGE_LIMIT:
                break
            start += len(results)
        return ids

    def _checkpoint_with_overlap(self, checkpoint: str) -> str:
        try:
            dt = datetime.fromisoformat(checkpoint)
            overlap = timedelta(minutes=15)
            adjusted = dt - overlap
            return adjusted.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return checkpoint

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None


def _parse_confluence_datetime(val: str) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None
