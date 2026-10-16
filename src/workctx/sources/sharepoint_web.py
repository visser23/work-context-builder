"""SharePoint source adapter — browser mode via REST API.

Uses rtFa/FedAuth session cookies (captured via Playwright) to call
SharePoint's internal REST endpoints.

Change detection is a **full enumerate-and-diff** on every run: walk the configured
library folder, compare each file's ``TimeLastModified`` with what the state database
holds, and report adds, updates and (when the walk was complete) deletions. That costs
about a minute for a few thousand files and — unlike the SharePoint ``GetChanges``
delta feed this adapter used to rely on — cannot silently lose events: a rename,
a moved folder, a throttled request or a rejected field simply shows up as a
difference on the next walk.

Every HTTP call retries transient failures (connection resets, 429/5xx) with backoff,
and an enumeration that could not read part of the tree never produces deletions.
"""

from __future__ import annotations

import contextlib
import fnmatch
import logging
import os
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from urllib.parse import quote

import httpx

from workctx.auth.sharepoint import (
    CookieStatus,
    SessionExpiredError,
    cookie_status,
    keepalive_and_extract,
    load_cookies,
    relogin_hint,
)
from workctx.config import SharePointSource
from workctx.models import (
    ChangeAction,
    DiscoveredChange,
    SourceObject,
    SourceType,
    SyncCheckpoint,
)
from workctx.sources.base import Source
from workctx.sources.sharepoint import _strip_glob_prefix
from workctx.state import StateDB

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 120.0
MAX_ATTEMPTS = 5
"""Tries per HTTP request before a transient failure is surfaced."""
MAX_BACKOFF_SECONDS = 60.0
MAX_SESSION_REFRESHES = 3
"""Browser refreshes allowed per run when SharePoint starts rejecting the cookies."""
MAX_STUB_RETRIES = 3
"""A file that keeps failing to convert is retried this many runs, then only when it changes."""
MASS_DELETE_MIN = 100
MASS_DELETE_RATIO = 0.5
"""Deleting more than this share of stored files (and at least ``MASS_DELETE_MIN``) in one
run is treated as a symptom of a bad listing, not a real clean-up. Use ``--full`` to force."""

_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

_CHROMIUM_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class EnumerationIncompleteError(RuntimeError):
    """The SharePoint folder walk could not read the whole tree (or returned nonsense).

    Raised instead of returning a partial listing so a flaky run can never be mistaken
    for "those files were deleted" or "nothing has changed".
    """


@dataclass
class _Listing:
    """Result of walking the library: every file SharePoint returned."""

    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    folders: int = 0
    inaccessible: list[str] = field(default_factory=list)
    """Folders SharePoint refused (403/404): their contents are unknown, not deleted."""


class SharePointWebSource(Source):
    """SharePoint via browser cookies + REST API."""

    def __init__(self, config: SharePointSource, *, max_workers: int = 12) -> None:
        self.config = config
        self._max_workers = max(1, max_workers)
        self._client: httpx.Client | None = None
        self._auth_lock = RLock()
        self._session_refreshes = 0
        self._listing: _Listing | None = None
        self._skipped: Counter[str] = Counter()
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

    # ------------------------------------------------------------------ auth

    def _get_cookies(self) -> dict[str, str]:
        """Get valid SharePoint cookies: try cache first, browser refresh as fallback.

        Cached cookies are only discarded when SharePoint *rejects* them; a request
        that merely fails (throttling, outage) keeps them, because that says nothing
        about whether the login is still good.
        """
        secret_ref = self.config.auth.secret_ref if self.config.auth else ""
        if not secret_ref:
            raise RuntimeError(f"No secret_ref configured for {self.name}")

        cached = load_cookies(secret_ref)
        if cached:
            status = cookie_status(self._site_url, cached)
            if status is not CookieStatus.REJECTED:
                logger.debug("Using cached cookies for %s (%s)", self.name, status.value)
                return cached
            logger.info("Cached cookies rejected for %s, refreshing via browser", self.name)

        try:
            # keepalive_and_extract validates cookies before returning
            return keepalive_and_extract(self._site_url, self.name, secret_ref)
        except SessionExpiredError:
            raise
        except Exception as e:
            logger.warning("Keep-alive failed for %s: %s", self.name, e, exc_info=True)
            raise SessionExpiredError(
                f"Could not refresh the SharePoint session for '{self.name}' automatically "
                f"({type(e).__name__}: {e}). If the login really has expired, run: "
                f"{relogin_hint(self.name)}"
            ) from e

    def _get_client(self) -> httpx.Client:
        with self._auth_lock:
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

    def _handle_auth_failure(self, failed_client: httpx.Client) -> None:
        """React to SharePoint rejecting our cookies mid-run.

        Rebuilds the client from refreshed cookies (silent SSO in the browser profile).
        Safe to call from many worker threads: only the first one that still holds the
        failed client does the refresh. Raises ``SessionExpiredError`` once the refresh
        budget is spent or a human login is genuinely required.
        """
        with self._auth_lock:
            if self._client is not failed_client:
                return  # another thread already replaced it
            if self._session_refreshes >= MAX_SESSION_REFRESHES:
                raise SessionExpiredError(
                    f"SharePoint kept rejecting the session for '{self.name}' "
                    f"after {self._session_refreshes} refreshes. "
                    f"Run: {relogin_hint(self.name)}"
                )
            self._session_refreshes += 1
            logger.warning(
                "SharePoint rejected the session for %s mid-run — refreshing (%d/%d)",
                self.name,
                self._session_refreshes,
                MAX_SESSION_REFRESHES,
            )
            with contextlib.suppress(Exception):
                failed_client.close()
            self._client = None
            self._get_client()  # raises SessionExpiredError if a human is needed

    @staticmethod
    def _looks_unauthenticated(resp: httpx.Response) -> bool:
        return resp.status_code in (401, 403) or 300 <= resp.status_code < 400

    def _session_is_rejected(self) -> bool:
        """Ask SharePoint itself whether the stored login still works.

        A single 401/403/redirect from one endpoint proves nothing (throttling, a
        flaky edge node and per-folder permission quirks all look like that), so the
        session is only declared dead when the lightweight probe is rejected too.
        """
        secret_ref = self.config.auth.secret_ref if self.config.auth else ""
        cookies = load_cookies(secret_ref) if secret_ref else None
        if not cookies:
            return True
        return cookie_status(self._site_url, cookies) is CookieStatus.REJECTED

    # -------------------------------------------------------------- requests

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """HTTP call that rides out transient failures.

        Retries connection errors/timeouts and 429/5xx with exponential backoff
        (honouring ``Retry-After``), refreshes the session when SharePoint rejects the
        cookies, and returns the last response (possibly non-200) for the caller to judge.
        Raises ``httpx.TransportError`` only after every attempt failed on the network.
        """
        last_exc: httpx.TransportError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            client = self._get_client()
            try:
                resp = client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt == MAX_ATTEMPTS:
                    break
                delay = _backoff(attempt)
                logger.warning(
                    "SharePoint %s %s failed (%s), retry %d/%d in %.0fs",
                    method,
                    _short(url),
                    type(exc).__name__,
                    attempt,
                    MAX_ATTEMPTS - 1,
                    delay,
                )
                time.sleep(delay)
                continue

            if self._looks_unauthenticated(resp):
                if self._session_is_rejected():
                    self._handle_auth_failure(client)
                    continue
                # Login is fine; this endpoint just said no. Treat like a transient fault.
                if attempt < MAX_ATTEMPTS:
                    delay = _backoff(attempt, resp.headers.get("Retry-After"))
                    logger.warning(
                        "SharePoint %s %s returned HTTP %d although the session is valid, "
                        "retry %d/%d in %.0fs",
                        method,
                        _short(url),
                        resp.status_code,
                        attempt,
                        MAX_ATTEMPTS - 1,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                return resp
            if resp.status_code in _TRANSIENT_STATUSES and attempt < MAX_ATTEMPTS:
                delay = _backoff(attempt, resp.headers.get("Retry-After"))
                logger.warning(
                    "SharePoint %s %s returned HTTP %d, retry %d/%d in %.0fs",
                    method,
                    _short(url),
                    resp.status_code,
                    attempt,
                    MAX_ATTEMPTS - 1,
                    delay,
                )
                time.sleep(delay)
                continue
            return resp

        if last_exc is not None:
            raise last_exc
        raise SessionExpiredError(
            f"SharePoint kept rejecting the session for '{self.name}'. "
            f"Run: {relogin_hint(self.name)}"
        )

    def _sp_get_by_path(
        self,
        client: httpx.Client | None,
        api_method: str,
        server_relative_path: str,
        *,
        suffix: str = "",
        params: dict[str, str] | None = None,
    ) -> httpx.Response | None:
        """Call a SP REST endpoint that takes a server-relative path.

        Tries inline encoding first, falls back to @path parameter
        notation if the server returns 400 (common with & and other
        special chars in folder/file names). Returns ``None`` if the network
        failed on every attempt. (``client`` is accepted for backwards
        compatibility; requests go through the retrying ``_request``.)
        """
        escaped = server_relative_path.replace("'", "''")
        encoded = quote(escaped, safe="/")
        url = f"/_api/web/{api_method}('{encoded}'){suffix}"
        try:
            resp = self._request("GET", url, params=params)
            if resp.status_code != 400:
                return resp
        except httpx.TransportError:
            pass

        encoded_full = quote(server_relative_path, safe="")
        encoded_full = encoded_full.replace("'", "%27")
        url_fallback = f"/_api/web/{api_method}(@path){suffix}"
        fallback_params = {"@path": f"'{encoded_full}'"}
        if params:
            fallback_params.update(params)
        try:
            return self._request("GET", url_fallback, params=fallback_params)
        except httpx.TransportError as e:
            logger.warning("SP REST call failed for %s: %s", server_relative_path, e)
            return None

    # ------------------------------------------------------------- discovery

    def discover_changes(
        self,
        db: StateDB,
        checkpoint: SyncCheckpoint | None,
        *,
        full: bool = False,
    ) -> list[DiscoveredChange]:
        listing = self._list_library()
        self._listing = listing

        known = {obj.source_id: obj for obj in db.get_objects_for_source(self.name)}
        self._skipped = Counter()

        changes: list[DiscoveredChange] = []
        for file_data in listing.files.values():
            change = self._file_to_change(file_data, known)
            if change:
                changes.append(change)
        deletions = self._plan_deletions(listing, known, full=full)

        skipped = ", ".join(f"{n} {why}" for why, n in self._skipped.most_common()) or "none"
        logger.info(
            "SharePoint/%s: %d files in %d folders on server; %d new/changed, "
            "%d deleted, %d stored, skipped: %s",
            self.name,
            len(listing.files),
            listing.folders,
            len(changes),
            len(deletions),
            len(known),
            skipped,
        )
        return deletions + changes

    def get_current_ids(self) -> set[str]:
        """Every file id currently on the server. Raises if the listing is incomplete."""
        listing = self._listing or self._list_library()
        if listing.inaccessible:
            raise EnumerationIncompleteError(
                f"{len(listing.inaccessible)} folder(s) could not be read, "
                f"e.g. {listing.inaccessible[0]}"
            )
        return set(listing.files)

    def _list_library(self) -> _Listing:
        """Walk the configured folder concurrently and return every file found.

        Transient failures are retried; if any folder still cannot be read because of a
        network/server error the whole walk is abandoned with
        ``EnumerationIncompleteError``. Folders SharePoint explicitly refuses
        (403/404) are recorded in ``inaccessible`` instead, so one locked-down folder
        cannot stop the rest of the library syncing.
        """
        base_path = self._server_relative_path or self._default_server_relative_path()
        listing = _Listing()
        problems: list[str] = []
        lock = Lock()

        def _enum_folder(folder_path: str) -> list[str]:
            logger.debug("Enumerating folder: %s", folder_path)
            child_folders: list[str] = []

            for suffix, select in (
                ("/Files", "Name,ServerRelativeUrl,TimeLastModified,Length,UniqueId"),
                ("/Folders", "Name,ServerRelativeUrl"),
            ):
                resp = self._sp_get_by_path(
                    None,
                    "GetFolderByServerRelativeUrl",
                    folder_path,
                    suffix=suffix,
                    params={"$select": select},
                )
                where = f"{folder_path}{suffix}"
                if resp is None:
                    problems.append(f"{where}: network error")
                    continue
                if resp.status_code in (403, 404):
                    if folder_path == base_path:
                        # The configured root itself is unreadable: misconfiguration or
                        # revoked access, not a quirk of one sub-folder.
                        problems.append(f"{where}: HTTP {resp.status_code} (configured root)")
                    else:
                        with lock:
                            listing.inaccessible.append(f"{where}: HTTP {resp.status_code}")
                    continue
                if resp.status_code != 200:
                    problems.append(f"{where}: HTTP {resp.status_code}")
                    continue

                results = resp.json().get("d", {}).get("results", [])
                if suffix == "/Files":
                    with lock:
                        for f in results:
                            url = f.get("ServerRelativeUrl")
                            if url:
                                listing.files[url] = f
                    continue
                for sub in results:
                    name = sub.get("Name", "")
                    if name.startswith("_") or name == "Forms":
                        continue
                    sub_url = sub.get("ServerRelativeUrl", "")
                    if sub_url:
                        child_folders.append(sub_url)
            return child_folders

        pending = [base_path]
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            while pending:
                futures = {pool.submit(_enum_folder, p): p for p in pending}
                listing.folders += len(pending)
                pending = []
                for future in as_completed(futures):
                    try:
                        pending.extend(future.result())
                    except SessionExpiredError:
                        raise
                    except Exception as exc:
                        logger.warning("Failed to enumerate %s", futures[future], exc_info=True)
                        problems.append(f"{futures[future]}: {type(exc).__name__}: {exc}")

        if problems:
            raise EnumerationIncompleteError(
                f"SharePoint listing for '{self.name}' is incomplete — "
                f"{len(problems)} folder request(s) failed after retries "
                f"(e.g. {problems[0]}). Nothing was changed; the next run starts again."
            )
        if listing.inaccessible:
            logger.warning(
                "SharePoint/%s: %d folder(s) are not readable (no deletions this run): %s",
                self.name,
                len(listing.inaccessible),
                "; ".join(listing.inaccessible[:5]),
            )
        logger.info(
            "SharePoint/%s: walked %d folders, %d files in %.0fs",
            self.name,
            listing.folders,
            len(listing.files),
            time.monotonic() - started,
        )
        return listing

    def _plan_deletions(
        self,
        listing: _Listing,
        known: dict[str, SourceObject],
        *,
        full: bool,
    ) -> list[DiscoveredChange]:
        """Stored files that are no longer on the server — only when the walk is trustworthy."""
        gone = [sid for sid in known if sid not in listing.files]
        # Stored files that are still on the server but are no longer wanted (now excluded
        # by config, or a picture/media type we never convert): stubs for those are noise.
        unwanted = [
            sid for sid in known if sid in listing.files and self._is_unwanted(listing.files[sid])
        ]
        if unwanted:
            logger.info(
                "SharePoint/%s: %d stored file(s) are excluded or not convertible — removing",
                self.name,
                len(unwanted),
            )
        gone = gone + unwanted
        if not gone:
            return []
        if listing.inaccessible:
            logger.warning(
                "SharePoint/%s: not deleting %d missing file(s) — some folders were unreadable",
                self.name,
                len(gone),
            )
            return []
        if not listing.files:
            raise EnumerationIncompleteError(
                f"SharePoint returned no files for '{self.name}' but {len(known)} are stored — "
                "refusing to treat that as a mass deletion."
            )
        if not full and len(gone) >= MASS_DELETE_MIN and len(gone) > MASS_DELETE_RATIO * len(known):
            logger.warning(
                "SharePoint/%s: %d of %d stored files look deleted — refusing to remove them "
                "automatically. Re-run with --full if that is genuine.",
                self.name,
                len(gone),
                len(known),
            )
            return []
        return [
            DiscoveredChange(
                source_id=sid,
                title=known[sid].title,
                action=ChangeAction.DELETE,
            )
            for sid in gone
        ]

    def _is_unwanted(self, file_data: dict[str, Any]) -> bool:
        """True for files we deliberately do not mirror (excluded, or pictures/media)."""
        from workctx.normalise.convertibility import should_skip_download

        name = file_data.get("Name", "")
        if self._should_exclude(name, file_data.get("ServerRelativeUrl", "")):
            return True
        skip, reason = should_skip_download(name, None)  # size limits never delete content
        return skip and reason.startswith("unconvertible format")

    def _file_to_change(
        self,
        file_data: dict[str, Any],
        known: dict[str, SourceObject],
    ) -> DiscoveredChange | None:
        from workctx.normalise.convertibility import should_skip_download

        server_url = file_data.get("ServerRelativeUrl", "")
        name = file_data.get("Name", "")
        modified = file_data.get("TimeLastModified", "")
        size = file_data.get("Length")
        unique_id = file_data.get("UniqueId", server_url)

        if self._should_exclude(name, server_url):
            self._skipped["excluded"] += 1
            return None

        file_size = int(size) if size else None
        skip, reason = should_skip_download(name, file_size)
        if skip:
            logger.debug("Skipping %s: %s", name, reason)
            self._skipped[reason.split(" (")[0]] += 1
            return None

        existing = known.get(server_url)
        if existing and existing.source_version == modified:
            if not existing.last_error:
                return None
            if (existing.retry_count or 0) >= MAX_STUB_RETRIES:
                self._skipped["unconvertible (gave up)"] += 1
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
            metadata={
                "library": self._doc_library,
                "relative_path": self.relative_path(server_url),
            },
        )

    # --------------------------------------------------------------- paths

    def relative_path(self, server_url: str) -> str:
        """Path of ``server_url`` below the configured folder (no leading slash).

        This is what the corpus uses for the output location, so documents appear
        directly under ``sharepoint/<source>/`` instead of behind the site, library and
        root-folder names that every file shares.
        """
        base = (self._server_relative_path or self._default_server_relative_path()).rstrip("/")
        if server_url.lower().startswith(f"{base.lower()}/"):
            return server_url[len(base) + 1 :]
        return server_url.lstrip("/")

    def _default_server_relative_path(self) -> str:
        """Derive server-relative path from site URL."""
        from urllib.parse import urlparse

        parsed = urlparse(self._site_url)
        site_path = parsed.path.rstrip("/")
        return f"{site_path}/{self._doc_library}"

    # ------------------------------------------------------------ download

    def download_file(self, server_relative_url: str) -> Path | None:
        """Download a file to a temp path for conversion, streaming to disk.

        Tries the standard URL-encoded path first, falls back to the @path
        parameter for files with problematic characters, and retries transient
        failures (resets, 429/5xx) with backoff.
        """
        suffix = Path(server_relative_url).suffix

        escaped = server_relative_url.replace("'", "''")
        encoded = quote(escaped, safe="/")
        urls = [
            f"/_api/web/GetFileByServerRelativeUrl('{encoded}')/$value",
            f"/_api/web/GetFileByServerRelativeUrl(@path)/$value?@path='{encoded}'",
        ]

        last_status: int | None = None
        for url in urls:
            path, status = self._download_with_retries(url, suffix)
            if path is not None:
                return path
            last_status = status
            if status != 400:
                break

        logger.warning(
            "Failed to download %s (last status: %s)", server_relative_url, last_status or "network"
        )
        return None

    def _download_with_retries(self, url: str, suffix: str) -> tuple[Path | None, int | None]:
        status: int | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            client = self._get_client()
            fd, tmp_name = tempfile.mkstemp(suffix=suffix)
            retry_after: str | None = None
            try:
                with os.fdopen(fd, "wb") as tmp, client.stream("GET", url) as resp:
                    status = resp.status_code
                    retry_after = resp.headers.get("Retry-After")
                    if status == 200:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            tmp.write(chunk)
                        return Path(tmp_name), 200
            except httpx.TransportError as exc:
                status = None
                logger.warning(
                    "Download error %s (%s), attempt %d/%d",
                    _short(url),
                    type(exc).__name__,
                    attempt,
                    MAX_ATTEMPTS,
                )
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)

            if status is not None and (status == 401 or 300 <= status < 400):
                self._handle_auth_failure(client)
                continue
            if (status is None or status in _TRANSIENT_STATUSES) and attempt < MAX_ATTEMPTS:
                time.sleep(_backoff(attempt, retry_after))
                continue
            return None, status
        return None, status

    # ----------------------------------------------------------- housekeeping

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


def _backoff(attempt: int, retry_after: str | None = None) -> float:
    """Seconds to wait before retry number ``attempt`` (``Retry-After`` wins if sane)."""
    if retry_after:
        try:
            return min(MAX_BACKOFF_SECONDS, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return min(MAX_BACKOFF_SECONDS, 2.0**attempt)


def _short(url: str) -> str:
    """Request path without the query string (which can carry file names/tokens)."""
    path = url.split("?", 1)[0]
    return path if len(path) <= 120 else f"{path[:117]}..."


def _parse_sp_datetime(val: str) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None
