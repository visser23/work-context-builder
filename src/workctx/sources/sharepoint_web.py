"""SharePoint source adapter — browser mode via REST API.

Uses rtFa/FedAuth session cookies (captured via Playwright) to call
SharePoint's internal REST endpoints. Delta detection via GetChanges
with persisted ChangeTokens.
"""

from __future__ import annotations

import fnmatch
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from workctx.auth.sharepoint import (
    SessionExpiredError,
    keepalive_and_extract,
    load_cookies,
)
from workctx.config import SharePointSource
from workctx.models import (
    ChangeAction,
    DiscoveredChange,
    SourceType,
    SyncCheckpoint,
)
from workctx.sources.base import Source
from workctx.sources.sharepoint import _strip_glob_prefix
from workctx.state import StateDB

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 120.0

_CHROMIUM_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class SharePointWebSource(Source):
    """SharePoint via browser cookies + REST API."""

    def __init__(self, config: SharePointSource) -> None:
        self.config = config
        self._client: httpx.Client | None = None
        self._site_url = (config.site_url or "").rstrip("/")
        self._doc_library = config.doc_library or "Shared Documents"
        self._server_relative_path = config.server_relative_path

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def source_type(self) -> SourceType:
        return SourceType.SHAREPOINT

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self._site_url:
            issues.append(f"{self.name}: site_url is required for browser mode")
        if not self.config.auth or not self.config.auth.secret_ref:
            issues.append(f"{self.name}: auth.secret_ref is required for cookie storage")
        return issues

    def _get_cookies(self) -> dict[str, str]:
        """Get valid SharePoint cookies: try cache first, browser refresh as fallback."""
        secret_ref = self.config.auth.secret_ref if self.config.auth else ""
        if not secret_ref:
            raise RuntimeError(f"No secret_ref configured for {self.name}")

        cached = load_cookies(secret_ref)
        if cached:
            if self._test_cookies(cached):
                logger.debug("Using cached cookies for %s", self.name)
                return cached
            logger.info("Cached cookies stale for %s, refreshing via browser", self.name)

        try:
            fresh = keepalive_and_extract(
                self._site_url,
                self.name,
                secret_ref,
            )
            if self._test_cookies(fresh):
                return fresh
            logger.warning("Browser-refreshed cookies still invalid for %s", self.name)
        except Exception as e:
            logger.warning("Keep-alive failed for %s: %s", self.name, e)

        raise SessionExpiredError(
            f"No valid cookies for '{self.name}'. "
            f"Run: workctx auth login-sharepoint --source {self.name}"
        )

    def _test_cookies(self, cookies: dict[str, str]) -> bool:
        """Quick HTTP check that cookies actually work against the SP API."""
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        try:
            resp = httpx.get(
                f"{self._site_url}/_api/web/title",
                headers={
                    "Cookie": cookie_header,
                    "Accept": "application/json;odata=verbose",
                    "User-Agent": _CHROMIUM_UA,
                },
                timeout=30,
                follow_redirects=False,
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            cookies = self._get_cookies()

            cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
            self._client = httpx.Client(
                base_url=self._site_url,
                timeout=REQUEST_TIMEOUT,
                headers={
                    "Cookie": cookie_header,
                    "Accept": "application/json;odata=verbose",
                    "User-Agent": _CHROMIUM_UA,
                },
                follow_redirects=False,
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

        if full or not checkpoint or not checkpoint.last_checkpoint:
            return self._full_enumerate(client, db)

        return self._incremental_via_getchanges(client, db, checkpoint.last_checkpoint)

    def get_current_ids(self) -> set[str]:
        client = self._get_client()
        ids: set[str] = set()
        base_path = self._server_relative_path or self._default_server_relative_path()
        self._collect_ids_recursive(client, base_path, ids)
        return ids

    def _collect_ids_recursive(
        self, client: httpx.Client, server_relative_path: str, ids: set[str]
    ) -> None:
        files_resp = self._sp_get_by_path(
            client,
            "GetFolderByServerRelativeUrl",
            server_relative_path,
            suffix="/Files",
            params={"$select": "ServerRelativeUrl"},
        )
        if files_resp and files_resp.status_code == 200:
            for f in files_resp.json().get("d", {}).get("results", []):
                url = f.get("ServerRelativeUrl", "")
                if url:
                    ids.add(url)

        folders_resp = self._sp_get_by_path(
            client,
            "GetFolderByServerRelativeUrl",
            server_relative_path,
            suffix="/Folders",
            params={"$select": "Name,ServerRelativeUrl"},
        )
        if folders_resp and folders_resp.status_code == 200:
            for sub in folders_resp.json().get("d", {}).get("results", []):
                name = sub.get("Name", "")
                if name.startswith("_") or name == "Forms":
                    continue
                sub_url = sub.get("ServerRelativeUrl", "")
                if sub_url:
                    self._collect_ids_recursive(client, sub_url, ids)

    # ------------------------------------------------------------------
    # Full enumeration
    # ------------------------------------------------------------------

    def _full_enumerate(self, client: httpx.Client, db: StateDB) -> list[DiscoveredChange]:
        """Walk the full document library and return all files as changes."""
        changes: list[DiscoveredChange] = []
        base_path = self._server_relative_path or self._default_server_relative_path()

        self._recurse_folder(client, base_path, changes, db)

        current_token = self._get_current_change_token(client)
        if current_token:
            self._stash_change_token(current_token)

        logger.info(
            "SharePoint/%s: full enumeration found %d files",
            self.name,
            len(changes),
        )
        return changes

    def _recurse_folder(
        self,
        client: httpx.Client,
        server_relative_path: str,
        changes: list[DiscoveredChange],
        db: StateDB,
    ) -> None:
        """Recursively list files and subfolders."""
        logger.info("Enumerating folder: %s", server_relative_path)
        files_resp = self._sp_get_by_path(
            client,
            "GetFolderByServerRelativeUrl",
            server_relative_path,
            suffix="/Files",
            params={"$select": "Name,ServerRelativeUrl,TimeLastModified,Length,UniqueId"},
        )
        if files_resp and files_resp.status_code == 200:
            data = files_resp.json()
            results = data.get("d", {}).get("results", [])
            for f in results:
                change = self._file_to_change(f, db)
                if change:
                    changes.append(change)

        folders_resp = self._sp_get_by_path(
            client,
            "GetFolderByServerRelativeUrl",
            server_relative_path,
            suffix="/Folders",
            params={"$select": "Name,ServerRelativeUrl"},
        )
        if folders_resp and folders_resp.status_code == 200:
            data = folders_resp.json()
            results = data.get("d", {}).get("results", [])
            for folder in results:
                folder_name = folder.get("Name", "")
                if folder_name.startswith("_") or folder_name == "Forms":
                    continue
                sub_path = folder.get("ServerRelativeUrl", "")
                if sub_path:
                    self._recurse_folder(client, sub_path, changes, db)

    def _file_to_change(self, file_data: dict[str, Any], db: StateDB) -> DiscoveredChange | None:
        """Convert a SharePoint file API response to a DiscoveredChange."""
        from workctx.normalise.convertibility import should_skip_download

        server_url = file_data.get("ServerRelativeUrl", "")
        name = file_data.get("Name", "")
        modified = file_data.get("TimeLastModified", "")
        size = file_data.get("Length")
        unique_id = file_data.get("UniqueId", server_url)

        if self._should_exclude(name, server_url):
            return None

        file_size = int(size) if size else None
        skip, reason = should_skip_download(name, file_size)
        if skip:
            logger.debug("Skipping %s: %s", name, reason)
            return None

        existing = db.get_object(self.name, server_url)
        if existing and existing.source_version == modified and not existing.last_error:
            return None

        action = ChangeAction.ADD if not existing else ChangeAction.UPDATE
        source_url = f"{self._site_url}{server_url}"

        return DiscoveredChange(
            source_id=server_url,
            source_key=unique_id,
            title=Path(name).stem,
            source_url=source_url,
            source_version=modified,
            source_updated_at=_parse_sp_datetime(modified),
            action=action,
            file_size=file_size,
            metadata={"library": self._doc_library},
        )

    # ------------------------------------------------------------------
    # Incremental via GetChanges
    # ------------------------------------------------------------------

    def _incremental_via_getchanges(
        self,
        client: httpx.Client,
        db: StateDB,
        change_token: str,
    ) -> list[DiscoveredChange]:
        """Use SP GetChanges API for incremental delta detection."""
        changes: list[DiscoveredChange] = []
        token = change_token

        while True:
            batch, next_token = self._fetch_changes(client, token)
            if not batch:
                break

            for change_item in batch:
                change_type = change_item.get("ChangeType", 0)
                item_id = change_item.get("ItemId", 0)

                if change_type == 3:
                    change = self._resolve_deletion(item_id, db)
                    if change:
                        changes.append(change)
                elif change_type in (1, 2):
                    change = self._resolve_item(client, item_id, db, change_type)
                    if change:
                        changes.append(change)

            if next_token and next_token != token:
                token = next_token
            else:
                break

        if token != change_token:
            self._stash_change_token(token)

        logger.info(
            "SharePoint/%s: incremental sync found %d changes",
            self.name,
            len(changes),
        )
        return changes

    def _fetch_changes(
        self, client: httpx.Client, change_token: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Call GetChanges on the document library."""
        encoded_lib = quote(self._doc_library)
        resp = client.post(
            f"/_api/web/lists/getbytitle('{encoded_lib}')/getchanges",
            json={
                "query": {
                    "__metadata": {"type": "SP.ChangeQuery"},
                    "Item": True,
                    "Add": True,
                    "Update": True,
                    "DeleteObject": True,
                    "File": True,
                    "Move": True,
                    "Rename": True,
                    "FetchLimit": 500,
                    "ChangeTokenStart": {"StringValue": change_token},
                }
            },
            headers=self._post_headers(client),
        )
        if resp.status_code != 200:
            logger.warning(
                "GetChanges returned HTTP %d: %s",
                resp.status_code,
                resp.text[:300],
            )
            return [], None

        data = resp.json()
        results = data.get("d", {}).get("results", [])
        next_token = results[-1].get("ChangeToken", {}).get("StringValue") if results else None
        return results, next_token

    def _resolve_item(
        self,
        client: httpx.Client,
        item_id: int,
        db: StateDB,
        change_type: int,
    ) -> DiscoveredChange | None:
        """Resolve a changed item ID to a full DiscoveredChange."""
        encoded_lib = quote(self._doc_library)
        resp = client.get(
            f"/_api/web/lists/getbytitle('{encoded_lib}')/items({item_id})",
            params={
                "$select": ("FileRef,FileLeafRef,Modified,File_x0020_Size,UniqueId"),
            },
        )
        if resp.status_code != 200:
            return None

        item = resp.json().get("d", {})
        file_ref = item.get("FileRef", "")
        leaf = item.get("FileLeafRef", "")
        modified = item.get("Modified", "")
        size = item.get("File_x0020_Size")

        if not file_ref or not leaf:
            return None
        if self._should_exclude(leaf, file_ref):
            return None

        existing = db.get_object(self.name, file_ref)
        action = ChangeAction.ADD if (change_type == 1 or not existing) else ChangeAction.UPDATE

        return DiscoveredChange(
            source_id=file_ref,
            source_key=item.get("UniqueId", file_ref),
            title=Path(leaf).stem,
            source_url=f"{self._site_url}{file_ref}",
            source_version=modified,
            source_updated_at=_parse_sp_datetime(modified),
            action=action,
            file_size=int(size) if size else None,
            metadata={"library": self._doc_library},
        )

    def _resolve_deletion(self, item_id: int, db: StateDB) -> DiscoveredChange | None:
        """Best-effort deletion detection by looking up stored objects."""
        for obj in db.get_objects_for_source(self.name):
            if obj.source_key and str(item_id) in str(obj.source_key):
                return DiscoveredChange(
                    source_id=obj.source_id,
                    title=obj.title,
                    action=ChangeAction.DELETE,
                )
        return None

    # ------------------------------------------------------------------
    # File download
    # ------------------------------------------------------------------

    def download_file(self, server_relative_url: str) -> Path | None:
        """Download a file to a temp path for conversion, streaming to disk."""
        client = self._get_client()
        suffix = Path(server_relative_url).suffix

        escaped = server_relative_url.replace("'", "''")
        encoded = quote(escaped, safe="/")
        url = f"/_api/web/GetFileByServerRelativeUrl('{encoded}')/$value"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            try:
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        logger.warning(
                            "Failed to download %s: HTTP %s",
                            server_relative_url,
                            resp.status_code,
                        )
                        return None
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        tmp.write(chunk)
            except httpx.HTTPError as e:
                logger.warning("Download error %s: %s", server_relative_url, e)
                return None
        return Path(tmp.name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_current_change_token(self, client: httpx.Client) -> str | None:
        """Get the current ChangeToken from the document library."""
        encoded_lib = quote(self._doc_library)
        resp = client.get(
            f"/_api/web/lists/getbytitle('{encoded_lib}')",
            params={"$select": "CurrentChangeToken"},
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("d", {}).get("CurrentChangeToken", {}).get("StringValue")
        return None

    def _stash_change_token(self, token: str) -> None:
        """Store change token for next incremental sync.

        The token is returned via the checkpoint mechanism in sync.py.
        We store it on the instance so it can be read back.
        """
        self._latest_change_token = token

    def _default_server_relative_path(self) -> str:
        """Derive server-relative path from site URL."""
        from urllib.parse import urlparse

        parsed = urlparse(self._site_url)
        site_path = parsed.path.rstrip("/")
        return f"{site_path}/{self._doc_library}"

    def _sp_get_by_path(
        self,
        client: httpx.Client,
        api_method: str,
        server_relative_path: str,
        *,
        suffix: str = "",
        params: dict[str, str] | None = None,
    ) -> httpx.Response | None:
        """Call a SP REST endpoint that takes a server-relative path.

        Tries inline encoding first, falls back to @path parameter
        notation if the server returns 400 (common with & and other
        special chars in folder/file names).
        """
        escaped = server_relative_path.replace("'", "''")
        encoded = quote(escaped, safe="/")
        url = f"/_api/web/{api_method}('{encoded}'){suffix}"
        try:
            resp = client.get(url, params=params)
            if resp.status_code != 400:
                return resp
        except httpx.HTTPError:
            pass

        encoded_full = quote(server_relative_path, safe="")
        encoded_full = encoded_full.replace("'", "%27")
        url_fallback = f"/_api/web/{api_method}(@path){suffix}"
        fallback_params = {"@path": f"'{encoded_full}'"}
        if params:
            fallback_params.update(params)
        try:
            return client.get(url_fallback, params=fallback_params)
        except httpx.HTTPError as e:
            logger.warning("SP REST call failed for %s: %s", server_relative_path, e)
            return None

    def _post_headers(self, client: httpx.Client) -> dict[str, str]:
        """Get headers for POST requests including request digest."""
        headers = {"Content-Type": "application/json;odata=verbose"}

        digest_resp = client.post("/_api/contextinfo")
        if digest_resp.status_code == 200:
            digest_data = digest_resp.json()
            digest_value = (
                digest_data.get("d", {})
                .get("GetContextWebInformation", {})
                .get("FormDigestValue", "")
            )
            if digest_value:
                headers["X-RequestDigest"] = digest_value

        return headers

    def _should_exclude(self, filename: str, server_url: str) -> bool:
        for pattern in self.config.exclude:
            base = _strip_glob_prefix(pattern)
            if fnmatch.fnmatch(filename, base):
                return True
            if fnmatch.fnmatch(server_url, base):
                return True
        return False

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None


def _parse_sp_datetime(val: str) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None
