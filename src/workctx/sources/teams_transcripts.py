"""Teams meeting transcripts source adapter.

Teams saves every recorded (or transcribed) meeting as an ``.mp4`` in the
organiser's OneDrive ``Recordings`` folder (or a team site for channel
meetings), then shares it with the attendees. The transcript lives on that
media item as a Stream *media transcript*.

This adapter reuses a browser-mode SharePoint login (``rtFa``/``FedAuth``
cookies - no Microsoft app registration needed) to:

1. **Discover** recordings through SharePoint Search (``ProgId:Media.Meeting``),
   scoped to the user's own OneDrive, items shared directly with them, and any
   extra team sites listed in the config.
2. **List** each item's transcripts via the OneDrive/SharePoint ``v2.1`` API.
3. **Download** the JSON transcript (carries speaker names) and render Markdown.

Cookies are per host, so two sessions are managed: the team-sites host
(``<tenant>.sharepoint.com``) and the OneDrive host (``<tenant>-my.sharepoint.com``).
Credentials are only ever attached to those two hosts.
"""

from __future__ import annotations

import base64
import fnmatch
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from workctx.auth.sharepoint import (
    SessionExpiredError,
    get_valid_cookies,
    onedrive_secret_ref,
    relogin_hint,
    tenant_hosts,
)
from workctx.config import SharePointSource, TranscriptsSource
from workctx.models import (
    ChangeAction,
    DiscoveredChange,
    SourceObject,
    SourceType,
    SyncCheckpoint,
)
from workctx.normalise.transcript import (
    ParsedTranscript,
    parse_recording_stamp,
    parse_transcript_json,
    parse_transcript_vtt,
    render_transcript_markdown,
)
from workctx.sources.base import Source
from workctx.state import StateDB

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 120.0
SEARCH_PAGE_SIZE = 500
MAX_HITS_PER_QUERY = 20_000
MAX_RETRIES = 4
MAX_RETRY_WAIT_SECONDS = 30.0
# Recordings modified more recently than this are always re-checked: the
# transcript is generated (and sometimes edited) after the meeting ends.
RECENT_RECHECK_DAYS = 14
# Item permanently unavailable to us: bad request for this drive form, no access,
# deleted, gone, or the owner's OneDrive is locked/blocked (e.g. a leaver's account).
_INACCESSIBLE_STATUSES = frozenset({400, 403, 404, 410, 423})
# Teams' own placeholder identity for service-created media items.
_SERVICE_ACCOUNT_NAMES = {"sharepoint app", "system account"}

_CHROMIUM_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_SELECT_PROPS = "Title,LastModifiedTime,UniqueId,SPWebUrl,SiteId,WebId,ListId,Size,ParentLink"
_FILENAME_SUFFIX_RE = re.compile(
    r"-\d{8}_\d{6}(?:UTC)?-Meeting (?:Recording|Transcript)$", re.IGNORECASE
)


@dataclass(frozen=True)
class RecordingHit:
    """A Teams recording found by SharePoint Search."""

    uid: str
    title: str
    web_url: str
    site_id: str
    web_id: str
    list_id: str
    modified: datetime | None
    size: int | None

    def api_bases(self) -> list[str]:
        """Candidate ``v2.1`` item URLs, most precise first."""
        bases: list[str] = []
        drive_id = _drive_id(self.site_id, self.web_id, self.list_id)
        if drive_id:
            bases.append(f"{self.web_url}/_api/v2.1/drives/{drive_id}/items/{self.uid}")
        bases.append(f"{self.web_url}/_api/v2.1/drive/items/{self.uid}")
        return bases


def _drive_id(site_id: str, web_id: str, list_id: str) -> str | None:
    """Build the Graph/OneDrive drive id (``b!…``) from site/web/list GUIDs."""
    try:
        raw = uuid.UUID(site_id).bytes_le + uuid.UUID(web_id).bytes_le + uuid.UUID(list_id).bytes_le
    except (ValueError, AttributeError, TypeError):
        return None
    return "b!" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _kql_quote(value: str) -> str:
    """Escape a value for use inside a double-quoted KQL literal in an OData string."""
    return value.replace('"', " ").replace("'", "''")


class TeamsTranscriptSource(Source):
    """Teams meeting transcripts via SharePoint/OneDrive browser cookies."""

    def __init__(
        self,
        config: TranscriptsSource,
        *,
        sharepoint_sources: dict[str, SharePointSource] | None = None,
        max_workers: int = 8,
    ) -> None:
        self.config = config
        self._max_workers = max(1, max_workers)
        self._config_error: str | None = None
        self._profile_name = config.name
        self._team_root = ""
        self._my_root = ""
        self._refs: dict[str, str] = {}
        self._clients: dict[str, httpx.Client] = {}
        self._client_lock = threading.Lock()

        login_url: str | None = config.site_url
        secret_ref: str | None = config.auth.secret_ref if config.auth else None
        if config.sharepoint_source:
            sp = (sharepoint_sources or {}).get(config.sharepoint_source)
            if sp is None:
                self._config_error = (
                    f"{config.name}: sharepoint_source '{config.sharepoint_source}' not found"
                )
            else:
                login_url = sp.site_url
                secret_ref = sp.auth.secret_ref if sp.auth else None
                self._profile_name = sp.name
        if not self._config_error and (not login_url or not secret_ref):
            self._config_error = (
                f"{config.name}: no SharePoint site_url / auth.secret_ref available for login"
            )
        if not self._config_error:
            assert login_url and secret_ref
            try:
                self._team_root, self._my_root = tenant_hosts(login_url)
            except ValueError as exc:
                self._config_error = f"{config.name}: {exc}"
            else:
                self._refs = {
                    self._team_root: secret_ref,
                    self._my_root: onedrive_secret_ref(secret_ref),
                }

    # ------------------------------------------------------------------ Source API

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def source_type(self) -> SourceType:
        return SourceType.TRANSCRIPT

    def validate(self) -> list[str]:
        issues = [self._config_error] if self._config_error else []
        for site in self.config.sites:
            if self._host_root(site) not in self._refs:
                issues.append(f"{self.name}: site '{site}' is not on this SharePoint tenant")
        if not (self.config.include_own or self.config.include_shared or self.config.sites):
            issues.append(f"{self.name}: nothing to sync (own, shared and sites all disabled)")
        return issues

    def discover_changes(
        self,
        db: StateDB,
        checkpoint: SyncCheckpoint | None,
        *,
        full: bool = False,
    ) -> list[DiscoveredChange]:
        self._require_valid_config()
        hits = self._collect_hits()
        known_all = {o.source_id: o for o in db.get_objects_for_source(self.name)}
        known = {} if full else known_all

        changes: list[DiscoveredChange] = []
        failures = 0
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(self._probe, hit, known, known_all): hit for hit in hits}
            for future in as_completed(futures):
                try:
                    change = future.result()
                except SessionExpiredError:
                    raise
                except Exception as exc:
                    failures += 1
                    logger.warning(
                        "Transcripts/%s: probing %s failed: %s",
                        self.name,
                        futures[future].uid,
                        exc,
                    )
                    continue
                if change:
                    changes.append(change)

        if hits and failures == len(hits):
            raise RuntimeError(
                f"Transcripts/{self.name}: all {failures} recording checks failed — see log"
            )
        logger.info(
            "Transcripts/%s: %d recordings visible, %d new/changed transcripts, %d check failures",
            self.name,
            len(hits),
            len(changes),
            failures,
        )
        return changes

    def get_current_ids(self) -> set[str]:
        """All recording ids currently visible. Raises on any failure (never partial)."""
        self._require_valid_config()
        return {hit.uid for hit in self._collect_hits()}

    def render_content(self, change: DiscoveredChange) -> str | None:
        """Download the transcript for ``change`` and render it as Markdown."""
        meta = change.metadata
        base = meta["item_api"]
        transcript_id = meta["transcript_id"]
        stream = f"{base}/media/transcripts/{quote(transcript_id)}/streamContent"

        parsed: ParsedTranscript | None = None
        resp = self._get(stream, params={"format": "json"}, accept="*/*")
        if resp.status_code == 200:
            try:
                parsed = parse_transcript_json(resp.json())
            except ValueError:
                logger.warning(
                    "Transcripts/%s: %s: invalid JSON, trying VTT", self.name, change.source_id
                )
        if parsed is None:
            resp = self._get(stream, params={"format": "vtt"}, accept="*/*")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"transcript download failed (HTTP {resp.status_code}) for {change.source_id}"
                )
            parsed = parse_transcript_vtt(resp.text)

        fm = change.metadata.setdefault("front_matter", {})
        if parsed.speakers:
            fm["participants"] = parsed.speakers
        if parsed.duration_seconds:
            fm["duration_minutes"] = max(1, round(parsed.duration_seconds / 60))

        meeting_date = _parse_date(meta.get("meeting_date"))
        return render_transcript_markdown(
            change.title or change.source_id,
            parsed,
            meeting_date=meeting_date,
            start_time=meta.get("start_time"),
            start_time_is_utc=bool(meta.get("start_time_is_utc")),
            recorded_by=meta.get("recorded_by"),
            recording_name=meta.get("recording_name"),
            recording_url=change.source_url,
        )

    def close(self) -> None:
        with self._client_lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()

    # --------------------------------------------------------------- Discovery

    def _require_valid_config(self) -> None:
        if self._config_error:
            raise RuntimeError(self._config_error)

    def _collect_hits(self) -> list[RecordingHit]:
        """Run every configured Search query and return de-duplicated, filtered hits."""
        personal_url, identities = self._identity()
        queries: list[str] = []
        if self.config.include_own and personal_url:
            queries.append(f'ProgId:Media.Meeting path:"{_kql_quote(personal_url)}"')
        if self.config.include_shared and identities:
            clause = " OR ".join(f'SharedWithUsersOWSUSER:"{_kql_quote(i)}"' for i in identities)
            queries.append(f"ProgId:Media.Meeting ({clause})")
        for site in self.config.sites:
            queries.append(f'ProgId:Media.Meeting path:"{_kql_quote(site.rstrip("/"))}"')

        by_uid: dict[str, RecordingHit] = {}
        for query in queries:
            for row in self._search(query):
                hit = self._row_to_hit(row)
                if hit:
                    by_uid.setdefault(hit.uid, hit)

        cutoff = (
            datetime.now(UTC) - timedelta(days=self.config.since_days)
            if self.config.since_days
            else None
        )
        patterns = [p.lower() for p in self.config.exclude_titles]
        hits: list[RecordingHit] = []
        for hit in by_uid.values():
            if cutoff and hit.modified and hit.modified < cutoff:
                continue
            title = hit.title.lower()
            if any(fnmatch.fnmatch(title, p) for p in patterns):
                logger.debug("Transcripts/%s: excluded by title pattern: %s", self.name, hit.uid)
                continue
            hits.append(hit)

        hits.sort(key=lambda h: h.modified or datetime.min.replace(tzinfo=UTC), reverse=True)
        return hits

    def _identity(self) -> tuple[str | None, list[str]]:
        """Return ``(personal_onedrive_url, [identities used for 'shared with me'])``."""
        resp = self._get(
            f"{self._my_root}/_api/web/currentuser",
            params={"$select": "Email,LoginName"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Could not identify logged-in user (HTTP {resp.status_code})")
        user = resp.json()
        email = (user.get("Email") or "").strip()
        upn = (user.get("LoginName") or "").split("|")[-1].strip()
        identities = [i for i in dict.fromkeys([email, upn]) if i and "@" in i]

        personal: str | None = None
        resp = self._get(
            f"{self._my_root}/_api/SP.UserProfiles.PeopleManager/GetMyProperties",
            params={"$select": "PersonalUrl"},
        )
        if resp.status_code == 200:
            personal = (resp.json().get("PersonalUrl") or "").rstrip("/") or None
        if not personal and identities:
            personal = (
                f"{self._my_root}/personal/{re.sub(r'[^A-Za-z0-9]', '_', identities[0]).lower()}"
            )
        logger.info(
            "Transcripts/%s: logged-in identities resolved (%d)", self.name, len(identities)
        )
        return personal, identities

    def _search(self, query: str) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        start = 0
        while len(rows) < MAX_HITS_PER_QUERY:
            resp = self._get(
                f"{self._my_root}/_api/search/query",
                params={
                    "querytext": f"'{query}'",
                    "selectproperties": f"'{_SELECT_PROPS}'",
                    "rowlimit": str(SEARCH_PAGE_SIZE),
                    "startrow": str(start),
                    "sortlist": "'LastModifiedTime:descending'",
                    "trimduplicates": "false",
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"SharePoint search failed (HTTP {resp.status_code}): {resp.text[:200]}"
                )
            results = resp.json()["PrimaryQueryResult"]["RelevantResults"]
            batch = results["Table"]["Rows"]
            for row in batch:
                rows.append({c["Key"]: c["Value"] for c in row["Cells"] if c.get("Value")})
            start += len(batch)
            if len(batch) < SEARCH_PAGE_SIZE or start >= int(results.get("TotalRows") or 0):
                break
        logger.debug("Transcripts/%s: query returned %d rows", self.name, len(rows))
        return rows

    def _row_to_hit(self, row: dict[str, str]) -> RecordingHit | None:
        uid = (row.get("UniqueId") or "").strip("{}").lower()
        web_url = (row.get("SPWebUrl") or "").rstrip("/")
        if not uid or not web_url or self._host_root(web_url) not in self._refs:
            return None
        try:
            uuid.UUID(uid)
        except ValueError:
            return None
        size = row.get("Size")
        return RecordingHit(
            uid=uid,
            title=(row.get("Title") or "").strip(),
            web_url=web_url,
            site_id=row.get("SiteId", ""),
            web_id=row.get("WebId", ""),
            list_id=row.get("ListId", ""),
            modified=_parse_dt(row.get("LastModifiedTime")),
            size=int(size) if size and size.isdigit() else None,
        )

    def _probe(
        self,
        hit: RecordingHit,
        known: dict[str, SourceObject],
        known_all: dict[str, SourceObject],
    ) -> DiscoveredChange | None:
        """Return a change if ``hit`` has a new/updated transcript, else ``None``."""
        existing = known.get(hit.uid)
        if (
            existing
            and not existing.last_error
            and hit.modified
            and existing.source_updated_at
            and hit.modified <= existing.source_updated_at
            and hit.modified < datetime.now(UTC) - timedelta(days=RECENT_RECHECK_DAYS)
        ):
            return None

        found = self._list_transcripts(hit)
        if not found:
            return None
        base, transcripts = found
        transcript = _pick_transcript(transcripts)
        version = str(transcript.get("cTag") or (hit.modified.isoformat() if hit.modified else ""))
        if existing and existing.source_version == version and not existing.last_error:
            return None

        item = self._get_item(base)
        file_name = str(item.get("name") or "")
        stamp = parse_recording_stamp(file_name)
        created = _parse_dt(item.get("createdDateTime"))
        meeting_date = stamp[0] if stamp else (created.date() if created else None)
        title = hit.title or _FILENAME_SUFFIX_RE.sub(
            "", re.sub(r"\.mp4$", "", file_name, flags=re.I)
        )
        recorded_by = ((item.get("createdBy") or {}).get("user") or {}).get("displayName")
        if recorded_by and recorded_by.strip().lower() in _SERVICE_ACCOUNT_NAMES:
            recorded_by = None

        front_matter: dict[str, Any] = {}
        if meeting_date:
            front_matter["meeting_date"] = meeting_date.isoformat()

        action = ChangeAction.UPDATE if hit.uid in known_all else ChangeAction.ADD
        size = transcript.get("size")
        return DiscoveredChange(
            source_id=hit.uid,
            source_key=hit.uid,
            title=title or hit.uid,
            source_url=item.get("webUrl"),
            source_version=version,
            source_updated_at=hit.modified,
            action=action,
            file_size=int(size) if isinstance(size, int) else None,
            metadata={
                "item_api": base,
                "transcript_id": transcript["id"],
                "recording_name": file_name or None,
                "recorded_by": recorded_by,
                "meeting_date": meeting_date.isoformat() if meeting_date else None,
                "start_time": stamp[1] if stamp else None,
                "start_time_is_utc": stamp[2] if stamp else False,
                "front_matter": front_matter,
            },
        )

    def _list_transcripts(self, hit: RecordingHit) -> tuple[str, list[dict[str, Any]]] | None:
        """Return ``(working_item_api_base, transcripts)`` or ``None`` if there are none."""
        for base in hit.api_bases():
            resp = self._get(f"{base}/media/transcripts")
            if resp.status_code == 200:
                items = [t for t in resp.json().get("value", []) if t.get("id")]
                return (base, items) if items else None
            if resp.status_code in _INACCESSIBLE_STATUSES:
                continue
            raise RuntimeError(f"listing transcripts failed (HTTP {resp.status_code})")
        logger.debug("Transcripts/%s: no accessible media item for %s", self.name, hit.uid)
        return None

    def _get_item(self, base: str) -> dict[str, Any]:
        resp = self._get(
            base,
            params={"select": "name,size,createdDateTime,lastModifiedDateTime,webUrl,createdBy"},
        )
        if resp.status_code != 200:
            logger.debug("Transcripts/%s: item metadata HTTP %d", self.name, resp.status_code)
            return {}
        data = resp.json()
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------- HTTP

    @staticmethod
    def _host_root(url: str) -> str:
        """``https://host`` for plain-https URLs; ``""`` for anything unusual.

        Rejects non-https schemes, embedded credentials and non-default ports so
        session cookies can never be steered to an unexpected endpoint.
        """
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return ""
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in (None, 443)
        ):
            return ""
        return f"https://{parsed.hostname.lower()}"

    def _client_for(self, url: str) -> httpx.Client:
        """Return the authenticated client for ``url``'s host (refuses unknown hosts)."""
        root = self._host_root(url)
        if root not in self._refs:
            raise ValueError("refusing to send SharePoint credentials to an unrelated host")
        with self._client_lock:
            client = self._clients.get(root)
            if client is None:
                cookies = get_valid_cookies(root, self._profile_name, self._refs[root])
                client = httpx.Client(
                    timeout=REQUEST_TIMEOUT,
                    follow_redirects=False,
                    headers={
                        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
                        "User-Agent": _CHROMIUM_UA,
                    },
                )
                self._clients[root] = client
            return client

    def _get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        accept: str = "application/json",
    ) -> httpx.Response:
        """GET with throttling back-off. Raises ``SessionExpiredError`` on auth loss."""
        client = self._client_for(url)
        resp: httpx.Response | None = None
        for attempt in range(MAX_RETRIES):
            resp = client.get(url, params=params, headers={"Accept": accept})
            if resp.status_code in (429, 503, 504):
                wait = _retry_after(resp, default=2.0 ** (attempt + 1))
                logger.warning(
                    "Transcripts/%s: throttled (HTTP %d), waiting %.0fs",
                    self.name,
                    resp.status_code,
                    wait,
                )
                time.sleep(wait)
                continue
            break
        assert resp is not None
        if resp.status_code in (301, 302, 303, 307, 308, 401):
            raise SessionExpiredError(
                f"SharePoint session for '{self._profile_name}' was rejected. "
                f"Run: {relogin_hint(self._profile_name)}"
            )
        return resp


def _retry_after(resp: httpx.Response, *, default: float) -> float:
    try:
        wait = float(resp.headers.get("Retry-After", default))
    except ValueError:
        wait = default
    return max(0.0, min(wait, MAX_RETRY_WAIT_SECONDS))


def _pick_transcript(transcripts: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer the default, visible transcript; otherwise the first one."""
    for transcript in transcripts:
        if transcript.get("isDefault") and transcript.get("isVisible", True):
            return transcript
    for transcript in transcripts:
        if transcript.get("isDefault"):
            return transcript
    for transcript in transcripts:
        if transcript.get("isVisible", True):
            return transcript
    return transcripts[0]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
